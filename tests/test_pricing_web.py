"""Live price manager: Telegram HMAC admin auth, persistent settings and price floors."""
import hashlib
import hmac
import json
import time
from decimal import Decimal
from urllib.parse import urlencode

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.business as business
import app.pricing_web as pricing
from app.business import Shop
from app.config import settings
from app.db import Base
from app.main import web
from app.provider import PriceItem


def signed_webapp_data(uid=42, age=0):
    now = int(time.time())
    values = {'auth_date': str(now - age),
              'query_id': 'AAE',
              'user': json.dumps({'id': uid, 'first_name': 'Owner'}, separators=(',', ':'))}
    check = '\n'.join(f'{k}={v}' for k, v in sorted(values.items()))
    key = hmac.new(b'WebAppData', settings.bot_token.encode(), hashlib.sha256).digest()
    values['hash'] = hmac.new(key, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(values)


@pytest.fixture
def admin_config(monkeypatch):
    monkeypatch.setattr(settings, 'bot_token', '12345:DEMO_ONLY')
    monkeypatch.setattr(settings, 'admins', {42}, raising=False)
    monkeypatch.setattr(settings, 'provider_price_to_wallet', Decimal('1'))


def test_miniapp_admin_hmac_and_expiry(admin_config):
    assert pricing.admin_from_init_data(signed_webapp_data(42)) == 42
    with pytest.raises(HTTPException) as wrong:
        pricing.admin_from_init_data(signed_webapp_data(43))
    assert wrong.value.status_code == 403
    with pytest.raises(HTTPException) as stale:
        pricing.admin_from_init_data(signed_webapp_data(42, age=13 * 3600))
    assert stale.value.status_code == 401
    altered = signed_webapp_data(42).replace('Owner', 'Other')
    with pytest.raises(HTTPException) as tampered:
        pricing.admin_from_init_data(altered)
    assert tampered.value.status_code == 401
    with pytest.raises(HTTPException) as absent:
        pricing.admin_from_init_data('')
    assert absent.value.status_code == 401


def test_shop_manual_price_respects_cost_floor(admin_config):
    item = PriceItem('telegram', '12', Decimal('0.45'), 10)
    assert Shop.price_from_settings(item, Decimal('50')) == Decimal('0.6750')
    assert Shop.price_from_settings(item, Decimal('50'), Decimal('0.8500')) == Decimal('0.8500')
    assert Shop.price_from_settings(item, Decimal('50'), Decimal('0.1000')) == Decimal('0.4500')


@pytest.mark.asyncio
async def test_web_dashboard_saves_exact_price_and_global_markup(monkeypatch, admin_config):
    engine = create_async_engine('sqlite+aiosqlite:///:memory:')
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(pricing, 'Session', sessions)
    monkeypatch.setattr(business, 'Session', sessions)

    class FakeProvider:
        async def get_prices(self):
            return [PriceItem('telegram', '12', Decimal('.30'), 100),
                    PriceItem('whatsapp', '2', Decimal('.40'), 50)]
        async def get_country_names(self):
            return {'12': 'USA (2)', '2': 'UK'}

    class FakeShop(Shop):
        def __init__(self): self.provider = FakeProvider()
    from app.main import runtime
    monkeypatch.setitem(runtime, 'shop', FakeShop())
    auth = {'X-Telegram-Init-Data': signed_webapp_data()}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=web),
                                 base_url='https://test.example') as client:
        page = await client.get('/admin/pricing')
        assert page.status_code == 200
        assert 'Price Manager' in page.text
        denied = await client.get('/admin/api/pricing')
        assert denied.status_code == 401
        nonadmin = await client.get('/admin/api/pricing',
                                    headers={'X-Telegram-Init-Data':signed_webapp_data(51)})
        assert nonadmin.status_code == 403
        status = await client.get('/admin/api/pricing', headers=auth)
        assert status.status_code == 200
        assert status.json()['count'] == 2
        assert status.json()['offers'][0]['flag'] == '🇺🇸'

        under_cost = await client.post('/admin/api/pricing',headers=auth,
            json={'action':'set','service':'telegram','country_id':'12','value':'0.01'})
        assert under_cost.status_code == 400
        saved = await client.post('/admin/api/pricing', headers=auth,
            json={'action':'set','service':'telegram','country_id':'12','value':'0.9000'})
        assert saved.status_code == 200
        quote = await FakeShop().price(PriceItem('telegram','12',Decimal('.30'),100))
        assert quote == Decimal('0.9000')
        updated = await client.get('/admin/api/pricing',headers=auth)
        assert updated.json()['offers'][0]['custom'] == '0.9000'

        markup = await client.post('/admin/api/pricing',headers=auth,
            json={'action':'markup','value':'80'})
        assert markup.status_code == 200
        default_quote = await FakeShop().price(PriceItem('whatsapp','2',Decimal('.40'),50))
        assert default_quote == Decimal('0.7200')
        assert await FakeShop().price(PriceItem('telegram','12',Decimal('.30'),100)) == Decimal('0.9000')

        reset = await client.post('/admin/api/pricing', headers=auth,
            json={'action':'reset','service':'telegram','country_id':'12'})
        assert reset.status_code == 200
        assert await FakeShop().price(PriceItem('telegram','12',Decimal('.30'),100)) == Decimal('0.5400')
        assert (await client.get('/admin/api/pricing',headers=auth)).json()['offers'][0]['custom'] is None

        invalid = await client.post('/admin/api/pricing', headers=auth,
            json={'action':'set','service':'bank','country_id':'12','value':'50'})
        assert invalid.status_code == 400
        hacked = await client.post('/admin/api/pricing', headers={'X-Telegram-Init-Data':'bad'},
            json={'action':'set','service':'telegram','country_id':'12','value':'50'})
        assert hacked.status_code == 401

    await engine.dispose()


@pytest.mark.asyncio
async def test_case_insensitive_telegram_start_command(admin_config):
    from aiogram.filters import CommandStart
    from aiogram.types import Chat, Message, User
    from datetime import datetime, timezone

    # Filter must recognize /Start as well as /start, including chat screenshots.
    message = Message(message_id=1, date=datetime.now(timezone.utc),
                      chat=Chat(id=42,type='private'),
                      from_user=User(id=42,is_bot=False,first_name='Test'),
                      text='/Start')
    filt = CommandStart(ignore_case=True)
    result = await filt(message, bot=None)
    assert result
