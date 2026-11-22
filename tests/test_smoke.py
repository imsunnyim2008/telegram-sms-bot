from decimal import Decimal

import pytest

from app.business import Shop
from app.config import Config
from app.gateway import CryptoBot
from app.ledger import InsufficientFunds
from app.provider import FiveSim, PriceItem


def test_config_live_is_fail_closed():
    c = Config(_env_file=None)
    assert not c.live_ready if not c.live_purchases_enabled else c.provider_price_to_wallet > 0
    assert not c.service_allowed('bank')
    assert c.service_allowed('telegram')


def test_crypto_signature_refuses_empty_header():
    assert not CryptoBot.verify_webhook(b'{"test":true}', '')


def test_price_item():
    price = PriceItem('telegram', 'england', Decimal('0.25'), 100)
    assert price.stock == 100


@pytest.mark.asyncio
async def test_provider_parses_mock_prices():
    import httpx
    import respx
    from app.provider import API
    with respx.mock(assert_all_called=True) as mock:
        mock.get(API + '/guest/prices').mock(return_value=httpx.Response(200, json={
            'england': {'telegram': {
                'vodafone': {'cost': 3, 'count': 5},
                'virtual': {'cost': 2, 'count': 3},
            }, 'bank': {'any': {'cost': 1, 'count': 3}}},
        }))
        provider = FiveSim()
        data = await provider.get_prices()
        await provider.aclose()
        assert len(data) == 1
        assert data[0].cost == Decimal('2')
        assert data[0].stock == 3


@pytest.mark.asyncio
async def test_ledger_atomic_and_duplicate_guard():
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
    from app.db import Base, User, LedgerEntry
    from app.ledger import apply
    from sqlalchemy import select
    engine = create_async_engine('sqlite+aiosqlite:///:memory:')
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sess = async_sessionmaker(engine, expire_on_commit=False)
    async with sess.begin() as s:
        s.add(User(id=999, balance=Decimal('0'), accepted_terms=True))
    async with sess.begin() as s:
        assert await apply(s, 999, Decimal('5'), 'topup', 'payment', 'a')
    async with sess.begin() as s:
        assert not await apply(s, 999, Decimal('5'), 'topup', 'payment', 'a')
        assert await apply(s, 999, Decimal('-3'), 'purchase', 'activation', 'b')
        with pytest.raises(InsufficientFunds):
            await apply(s, 999, Decimal('-3'), 'purchase', 'activation', 'c')
    async with sess() as s:
        user = await s.get(User, 999)
        entries = (await s.scalars(select(LedgerEntry))).all()
        assert user.balance == Decimal('2')
        assert sum(e.amount for e in entries) == user.balance
    await engine.dispose()


@pytest.mark.asyncio
async def test_payment_replay_is_idempotent(monkeypatch):
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
    import app.business as business
    from app.db import Base, User, Payment, LedgerEntry
    from sqlalchemy import select

    engine = create_async_engine('sqlite+aiosqlite:///:memory:')
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sess = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(business, 'Session', sess)
    async with sess.begin() as s:
        s.add(User(id=888, balance=Decimal('0'), accepted_terms=True))
        s.add(Payment(id=1, user_id=888, gateway_invoice_id='456',
                      amount_fiat=Decimal('5'), amount_credit=Decimal('5'), status='created'))

    class FakeGateway:
        async def get_invoice(self, invoice_id):
            return {'invoice_id': 456, 'status': 'paid', 'payload': '1',
                    'amount': '5', 'fiat': business.settings.wallet_currency, 'currency_type': 'fiat'}

    class FakeNotifier:
        def __init__(self):
            self.sent = []

        async def send(self, uid, msg):
            self.sent.append((uid, msg))

    notifier = FakeNotifier()
    shop = Shop(None, FakeGateway(), notifier)
    assert await shop.verify_and_credit('456')
    assert not await shop.verify_and_credit('456')
    async with sess() as s:
        user = await s.get(User, 888)
        entries = (await s.scalars(select(LedgerEntry))).all()
        assert user.balance == Decimal('5')
        assert len(entries) == 1
    assert len(notifier.sent) == 1
    await engine.dispose()


@pytest.mark.asyncio
async def test_uncertain_provider_buy_not_retried():
    import httpx
    import respx
    from app.provider import API, AmbiguousPurchase
    with respx.mock(assert_all_called=True) as mock:
        route = mock.get(API + '/user/buy/activation/england/any/telegram').mock(
            return_value=httpx.Response(503, json={'error': 'unavailable'}))
        client = FiveSim()
        with pytest.raises(AmbiguousPurchase):
            await client.buy_number('telegram', 'england', Decimal('1'))
        assert route.call_count == 1
        await client.aclose()
