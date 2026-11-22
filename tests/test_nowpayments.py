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



def test_signature_top_level_sorted_with_nested_fee_order(monkeypatch):
    """Support both HMAC formats referenced by the NOWPayments documentation."""
    monkeypatch.setattr(settings, 'nowpayments_ipn_secret', 'mock-ipn')
    event = {'payment_status': 'finished', 'fee': {'withdrawalFee': 0, 'depositFee': 0.1},
             'payment_id': 777, 'invoice_id': 222}
    top_sorted = json.dumps(dict(sorted(event.items())), separators=(',', ':')).encode()
    signature = hmac.new(b'mock-ipn', top_sorted, hashlib.sha512).hexdigest()
    assert NowPayments.verify_webhook(json.dumps(event).encode(), signature)


def test_signature_bad_ipn_secret_is_denied(monkeypatch):
    monkeypatch.setattr(settings, 'nowpayments_ipn_secret', 'different-secret')
    event = {'payment_id': 333, 'invoice_id': 111}
    body = json.dumps(event, sort_keys=True, separators=(',', ':')).encode()
    signature = hmac.new(b'actual-provider-secret', body, hashlib.sha512).hexdigest()
    assert not NowPayments.verify_webhook(json.dumps(event).encode(), signature)



def test_signature_matches_javascript_float_normalization(monkeypatch):
    """JS signs 0.0 as 0; Python normally serializes float as 0.0."""
    monkeypatch.setattr(settings, 'nowpayments_ipn_secret', 'mock-ipn')
    from app.nowpayments import canonical_json, js_normalize_numbers
    event = {'payment_id': 123456, 'fee': {'serviceFee': 0.0,
             'withdrawalFee': 1.0}, 'price_amount': 5.0}
    raw = json.dumps(event).encode()
    canonical = json.dumps(js_normalize_numbers(canonical_json(event)),
                           separators=(',', ':'), ensure_ascii=False).encode()
    signature = hmac.new(b'mock-ipn', canonical, hashlib.sha512).hexdigest()
    assert NowPayments.verify_webhook(raw, signature)


def test_signature_can_verify_exact_signed_body(monkeypatch):
    monkeypatch.setattr(settings, 'nowpayments_ipn_secret', 'mock-ipn')
    raw = b'{"payment_id":123,"invoice_id":234}'
    signature = hmac.new(b'mock-ipn', raw, hashlib.sha512).hexdigest()
    assert NowPayments.verify_webhook(raw, signature)


@pytest.mark.asyncio
async def test_hosted_invoice_null_invoice_id_can_credit_safely(monkeypatch):
    """NOWPayments webhook can have invoice_id=null even for paid invoices."""
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
        s.add(User(id=999, balance=Decimal('0'), accepted_terms=True))
        s.add(Payment(id=99, user_id=999, gateway='nowpayments',
                      gateway_invoice_id='111', status='created',
                      amount_fiat=Decimal('5'), amount_credit=Decimal('5')))
    class Gateway:
        async def get_payment_status(self, payment_id):
            assert payment_id == '333'
            return {'payment_id': 333, 'invoice_id': None,
                    'order_id': 'friday-topup-99', 'price_amount': 5,
                    'price_currency': 'usd', 'actually_paid': '4.99',
                    'payment_status': 'finished'}
    class Notifier:
        def __init__(self): self.sent = []
        async def send(self, user_id, message): self.sent.append(user_id)
    notifier = Notifier()
    shop = Shop(None, Gateway(), notifier)
    assert await shop.verify_and_credit_nowpayments(
        None, '333', order_id='friday-topup-99')
    assert not await shop.verify_and_credit_nowpayments(
        None, '333', order_id='friday-topup-99')
    async with sess() as s:
        user = await s.get(User, 999)
        entries = (await s.scalars(select(LedgerEntry))).all()
        assert user.balance == Decimal('5')
        assert len(entries) == 1
    assert notifier.sent == [999]
    await engine.dispose()


@pytest.mark.asyncio
async def test_wrong_remote_invoice_id_must_never_credit(monkeypatch):
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
        s.add(User(id=999, balance=Decimal('0'), accepted_terms=True))
        s.add(Payment(id=99, user_id=999, gateway='nowpayments',
                      gateway_invoice_id='111', status='created',
                      amount_fiat=Decimal('5'), amount_credit=Decimal('5')))
    class Gateway:
        async def get_payment_status(self, payment_id):
            return {'payment_id': 333, 'invoice_id': 222,
                    'order_id': 'friday-topup-99', 'price_amount': 5,
                    'price_currency': 'usd', 'actually_paid': 5,
                    'payment_status': 'finished'}
    class Notifier:
        async def send(self, *_): raise AssertionError('No credit notification')
    shop = Shop(None, Gateway(), Notifier())
    assert not await shop.verify_and_credit_nowpayments(None, '333',
                                                        order_id='friday-topup-99')
    async with sess() as s:
        assert (await s.get(User, 999)).balance == Decimal('0')
    await engine.dispose()


@pytest.mark.asyncio
async def test_ipn_webhook_accepts_signed_event_without_invoice_id(monkeypatch):
    """Webhooks containing a valid signed order_id are processed asynchronously."""
    import app.main as main
    monkeypatch.setattr(settings, 'payment_provider', 'nowpayments')
    monkeypatch.setattr(settings, 'payments_enabled', True)
    monkeypatch.setattr(settings, 'nowpayments_api_key', 'mock-key')
    monkeypatch.setattr(settings, 'nowpayments_ipn_secret', 'mock-ipn')
    monkeypatch.setattr(settings, 'public_base_url', 'https://bot.example.com')
    calls = []
    async def processor(order_id, invoice_id, payment_id):
        calls.append((order_id, invoice_id, payment_id))
    monkeypatch.setattr(main, 'process_nowpayments_payment', processor)
    event = {'payment_id': 333, 'invoice_id': None, 'order_id': 'friday-topup-99',
             'payment_status': 'finished'}
    raw = json.dumps(event).encode()
    sorted_payload = json.dumps(event, sort_keys=True, separators=(',', ':')).encode()
    signature = hmac.new(b'mock-ipn', sorted_payload, hashlib.sha512).hexdigest()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.web),
                                 base_url='https://bot.example.com') as client:
        response = await client.post('/webhook/nowpayments', content=raw,
                headers={'x-nowpayments-sig':signature,'content-type':'application/json'})
        assert response.status_code == 200
        assert calls == [('friday-topup-99', None, '333')]
        invalid = await client.post('/webhook/nowpayments', content=raw,
                headers={'x-nowpayments-sig':'invalid','content-type':'application/json'})
        assert invalid.status_code == 401



def test_official_nowpayments_python_hmac_recipe_with_unicode(monkeypatch):
    # NOWPayments explicitly uses json.dumps(..., sort_keys=True) with default
    # ensure_ascii=True in its official Python webhook verification example.
    monkeypatch.setattr(settings, 'nowpayments_ipn_secret', 'demo-only-ipn-key')
    webhook = {'payment_id': 222, 'order_id': 'friday-topup-1',
               'order_description': 'Test café', 'payment_status': 'finished',
               'fee': {'withdrawalFee': 0.01, 'depositFee': 0}}
    sorted_msg = json.dumps(webhook, separators=(',', ':'), sort_keys=True)
    signature = hmac.new(b'demo-only-ipn-key', sorted_msg.encode(),
                         hashlib.sha512).hexdigest()
    assert NowPayments.verify_webhook(json.dumps(webhook).encode(), signature)
    changed = dict(webhook, payment_status='waiting')
    assert not NowPayments.verify_webhook(json.dumps(changed).encode(), signature)


def test_official_nowpayments_hmac_rejects_wrong_key(monkeypatch):
    webhook = {'payment_id': 222, 'payment_status': 'finished'}
    signed = hmac.new(b'real-key-demo', json.dumps(webhook, separators=(',', ':'),
                      sort_keys=True).encode(), hashlib.sha512).hexdigest()
    monkeypatch.setattr(settings, 'nowpayments_ipn_secret', 'incorrect-key-demo')
    assert not NowPayments.verify_webhook(json.dumps(webhook).encode(), signed)
