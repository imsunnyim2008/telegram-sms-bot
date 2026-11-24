"""HTTP-level NOWPayments callback checks without real transactions."""
import hashlib
import hmac
import json

import httpx
import pytest

from app.config import settings
from app.main import web
from app.nowpayments import NowPayments, canonical_json


@pytest.fixture(autouse=True)
def sandbox_credentials(monkeypatch):
    monkeypatch.setattr(settings, 'payment_provider', 'nowpayments')
    monkeypatch.setattr(settings, 'payments_enabled', True)
    monkeypatch.setattr(settings, 'nowpayments_api_key', 'test-key')
    monkeypatch.setattr(settings, 'nowpayments_ipn_secret', 'test-ipn-only')
    monkeypatch.setattr(settings, 'public_base_url', 'https://test.example')


def signature_for(event, *, deep=True):
    payload = canonical_json(event) if deep else dict(sorted(event.items()))
    text = json.dumps(payload, separators=(',', ':'), ensure_ascii=False).encode()
    return hmac.new(b'test-ipn-only', text, hashlib.sha512).hexdigest()


def sample_event(status='finished'):
    return {
        'invoice_id': 123, 'payment_id': 456, 'price_amount': 5,
        'payment_status': status,
        'fee': {'withdrawalFee': '0.01', 'depositFee': '0', 'serviceFee': '0.02'},
    }


def test_ipn_accepts_documented_shallow_sorted_signature():
    event = sample_event()
    assert NowPayments.verify_webhook(json.dumps(event).encode(),
                                     signature_for(event, deep=False))


def test_ipn_accepts_deep_sorted_signed_payload():
    event = sample_event()
    assert NowPayments.verify_webhook(json.dumps(event).encode(),
                                     signature_for(event, deep=True))


def test_ipn_rejects_signature_for_tampered_payment():
    event = sample_event()
    sig = signature_for(event)
    event['price_amount'] = 5000
    assert not NowPayments.verify_webhook(json.dumps(event).encode(), sig)


@pytest.mark.asyncio
async def test_webhook_route_rejects_unsigned_and_modified_payload(monkeypatch):
    called = []

    async def fake_process(*args):
        called.append(args)

    monkeypatch.setattr('app.main.process_nowpayments_payment', fake_process)
    queued = []
    async def fake_enqueue(session, payment_id, invoice_id, order_id):
        queued.append((order_id, invoice_id, payment_id))
    monkeypatch.setattr('app.main.accept_finished_ipn', fake_enqueue)
    event = sample_event()
    sig = signature_for(event)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=web),
                                 base_url='http://testserver') as client:
        unsigned = await client.post('/webhook/nowpayments', json=event)
        assert unsigned.status_code == 401
        altered = event | {'payment_status': 'waiting'}
        bad = await client.post('/webhook/nowpayments', json=altered,
                                headers={'x-nowpayments-sig': sig})
        assert bad.status_code == 401
    assert called == []


@pytest.mark.asyncio
async def test_webhook_route_processes_finished_only(monkeypatch):
    called = []

    async def fake_process(*args):
        called.append(args)

    monkeypatch.setattr('app.main.process_nowpayments_payment', fake_process)
    queued = []
    async def fake_enqueue(session, payment_id, invoice_id, order_id):
        queued.append((order_id, invoice_id, payment_id))
    monkeypatch.setattr('app.main.accept_finished_ipn', fake_enqueue)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=web),
                                 base_url='http://testserver') as client:
        waiting = sample_event('waiting')
        r = await client.post('/webhook/nowpayments', json=waiting,
                              headers={'x-nowpayments-sig': signature_for(waiting)})
        assert r.status_code == 200
        assert called == []
        finished = sample_event()
        r = await client.post('/webhook/nowpayments', json=finished,
                              headers={'x-nowpayments-sig': signature_for(finished)})
        assert r.status_code == 200
    assert called == [(None, '123', '456')]
    assert queued == called


@pytest.mark.asyncio
async def test_webhook_route_rejects_forged_invoice_id(monkeypatch):
    called = []

    async def fake_process(*args):
        called.append(args)

    monkeypatch.setattr('app.main.process_nowpayments_payment', fake_process)
    event = sample_event()
    event['invoice_id'] = '../not-a-payment'
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=web),
                                 base_url='http://testserver') as client:
        r = await client.post('/webhook/nowpayments', json=event,
                              headers={'x-nowpayments-sig': signature_for(event)})
    assert r.status_code == 400
    assert called == []
