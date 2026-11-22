"""NOWPayments payment verification regression tests; no live API credentials."""
import hashlib
import hmac
import json
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.db import Base, LedgerEntry, Payment, User
from app.nowpayments import NowPayments
from app.business import Shop


@pytest.mark.asyncio
async def test_api_creates_hosted_invoice(monkeypatch):
    monkeypatch.setattr(settings, 'nowpayments_api_key', 'mock-key')
    monkeypatch.setattr(settings, 'nowpayments_ipn_secret', 'mock-ipn')
    monkeypatch.setattr(settings, 'public_base_url', 'https://bot.example.com')
    calls = []

    def handle(request):
        calls.append(request)
        assert request.headers['x-api-key'] == 'mock-key'
        body = json.loads(request.content)
        assert body['price_currency'] == 'usd'
        assert body['price_amount'] == 5
        assert body['order_id'] == 'friday-topup-9'
        assert body['ipn_callback_url'] == 'https://bot.example.com/webhook/nowpayments'
        return httpx.Response(200, json={
            'id': '111', 'invoice_url': 'https://nowpayments.io/payment/?iid=111',
            'order_id': body['order_id'],
        })

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        gateway = NowPayments(client)
        inv = await gateway.create_invoice(Decimal('5.00'), '9')
    assert inv['invoice_id'] == '111'
    assert len(calls) == 1


def test_signature_and_tampering(monkeypatch):
    monkeypatch.setattr(settings, 'nowpayments_ipn_secret', 'mock-ipn')
    event = {'payment_id': 333, 'payment_status': 'finished', 'invoice_id': 111}
    raw = json.dumps(event).encode()
    canonical = json.dumps({k: event[k] for k in sorted(event)},
                           separators=(',', ':')).encode()
    signature = hmac.new(b'mock-ipn', canonical, hashlib.sha512).hexdigest()
    assert NowPayments.verify_webhook(raw, signature)
    assert not NowPayments.verify_webhook(raw, 'bad')
    assert not NowPayments.verify_webhook(raw.replace(b'finished', b'waiting'), signature)


@pytest.mark.asyncio
async def test_payment_verified_and_credited_once(monkeypatch):
    import app.business as business
    engine = create_async_engine('sqlite+aiosqlite:///:memory:')
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sess = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(business, 'Session', sess)
    monkeypatch.setattr(settings, 'payment_provider', 'nowpayments')
    monkeypatch.setattr(settings, 'payments_enabled', True)
    monkeypatch.setattr(settings, 'nowpayments_api_key', 'mock-key')
    monkeypatch.setattr(settings, 'nowpayments_ipn_secret', 'mock-ipn')
    monkeypatch.setattr(settings, 'public_base_url', 'https://bot.example.com')
    async with sess.begin() as s:
        s.add(User(id=456, balance=Decimal('0'), accepted_terms=True))
        s.add(Payment(id=7, user_id=456, gateway='nowpayments',
                      gateway_invoice_id='111', status='created',
                      amount_fiat=Decimal('5'), amount_credit=Decimal('5')))

    class Gateway:
        async def get_payment_status(self, _):
            return {'payment_id': 333, 'invoice_id': 111,
                    'order_id': 'friday-topup-7', 'price_amount': 5,
                    'price_currency': 'usd', 'actually_paid': 5,
                    'payment_status': 'finished'}

    class Notifier:
        def __init__(self):
            self.sent = []

        async def send(self, user_id, message):
            self.sent.append((user_id, message))

    notifier = Notifier()
    shop = Shop(None, Gateway(), notifier)
    assert await shop.verify_and_credit_nowpayments('111', '333')
    assert not await shop.verify_and_credit_nowpayments('111', '333')
    async with sess() as s:
        user = await s.get(User, 456)
        entries = (await s.scalars(select(LedgerEntry))).all()
        assert user.balance == Decimal('5')
        assert len(entries) == 1
    await engine.dispose()


@pytest.mark.asyncio
async def test_no_credit_for_wrong_order_or_unfinished(monkeypatch):
    import app.business as business
    engine = create_async_engine('sqlite+aiosqlite:///:memory:')
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sess = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(business, 'Session', sess)
    monkeypatch.setattr(settings, 'payment_provider', 'nowpayments')
    monkeypatch.setattr(settings, 'payments_enabled', True)
    monkeypatch.setattr(settings, 'nowpayments_api_key', 'mock-key')
    monkeypatch.setattr(settings, 'nowpayments_ipn_secret', 'mock-ipn')
    monkeypatch.setattr(settings, 'public_base_url', 'https://bot.example.com')
    async with sess.begin() as s:
        s.add(User(id=456, balance=Decimal('0'), accepted_terms=True))
        s.add(Payment(id=7, user_id=456, gateway='nowpayments',
                      gateway_invoice_id='111', status='created',
                      amount_fiat=Decimal('5'), amount_credit=Decimal('5')))

    class Gateway:
        async def get_payment_status(self, _):
            return {'payment_id': 333, 'invoice_id': 111,
                    'order_id': 'friday-topup-WRONG', 'price_amount': 5,
                    'price_currency': 'usd', 'actually_paid': 5,
                    'payment_status': 'finished'}

    class Notifier:
        async def send(self, user_id, message):
            raise AssertionError('No notification expected')

    shop = Shop(None, Gateway(), Notifier())
    assert not await shop.verify_and_credit_nowpayments('111', '333')
    async with sess() as s:
        assert (await s.get(User, 456)).balance == Decimal('0')
    await engine.dispose()
