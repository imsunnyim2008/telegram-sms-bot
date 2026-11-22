"""TextVerified.com HMAC-SHA512 webhook receiver: auth, dedup, ownership."""
import base64
import hashlib
import hmac
import json
from datetime import timedelta, timezone

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.db import (Activation, Base, TextVerifiedWebhookReceipt, utcnow)
from app.main import web
import app.textverified_webhooks as tv


@pytest.fixture
def credentials(monkeypatch):
    monkeypatch.setattr(settings, 'textverified_webhook_secret', 'whsec_test-secret')


@pytest.fixture
def signed_event():
    now = utcnow()
    return {
        'attempt': 1,
        'event': 'v2.sms.received',
        'id': 'evt-1',
        'idempotencyKey': 'key123',
        'occurredAt': now.isoformat(),
        'data': {
            'from': 'verified sender',
            'to': '15557778888',
            'createdAt': now.isoformat(),
            'smsContent': 'Example code 999000',
            'parsedCode': '999000',
            'encrypted': False,
            'reservationId': None,
        },
    }


def signed_headers(body, secret='whsec_test-secret'):
    sig = base64.b64encode(
        hmac.new(secret.encode(), body, hashlib.sha512).digest()
    ).decode()
    return {'x-webhook-signature': 'HMAC-SHA512=' + sig}


@pytest.mark.asyncio
async def test_receiver_rejects_unsigned_tampered_and_unconfigured(credentials, signed_event, monkeypatch):
    raw = json.dumps(signed_event).encode()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=web),
                                 base_url='http://test') as client:
        res = await client.post('/webhook/textverified', content=raw)
        assert res.status_code == 401
        altered = raw.replace(b'999000', b'888000')
        res = await client.post('/webhook/textverified', content=altered,
                                headers=signed_headers(raw))
        assert res.status_code == 401
        monkeypatch.setattr(settings, 'textverified_webhook_secret', '')
        res = await client.post('/webhook/textverified', content=raw,
                                headers=signed_headers(raw))
        assert res.status_code == 503


@pytest.mark.asyncio
async def test_signed_event_wakes_only_order_owned_by_textverified(credentials, signed_event, monkeypatch):
    engine = create_async_engine('sqlite+aiosqlite:///:memory:')
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(tv, 'Session', sessions)
    now = utcnow()
    async with sessions.begin() as s:
        s.add_all([
            Activation(id=1, provider='textverified', provider_order_id='abc123',
                       user_id=7, service='telegram', country='us',
                       phone='15557778888', status='pending',
                       next_poll_at=now+timedelta(hours=1),
                       created_at=now-timedelta(minutes=1),
                       expires_at=now+timedelta(minutes=10),
                       cost_provider=1, price_charged=2),
            Activation(id=2, provider='grizzly', provider_order_id='123456',
                       user_id=8, service='telegram', country='us',
                       phone='15557778888', status='pending',
                       next_poll_at=now+timedelta(hours=1),
                       created_at=now-timedelta(minutes=1),
                       expires_at=now+timedelta(minutes=10),
                       cost_provider=1, price_charged=2),
        ])
    raw = json.dumps(signed_event).encode()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=web),
                                 base_url='http://test') as client:
        first = await client.post('/webhook/textverified', content=raw,
                                  headers=signed_headers(raw))
        second = await client.post('/webhook/textverified', content=raw,
                                   headers=signed_headers(raw))
    assert first.status_code == 200 and second.status_code == 200
    async with sessions() as s:
        one, two = await s.get(Activation, 1), await s.get(Activation, 2)
        assert one.next_poll_at.replace(tzinfo=timezone.utc) <= utcnow()
        assert two.next_poll_at.replace(tzinfo=timezone.utc) > utcnow()
        # The incoming code is never trusted as proof of received SMS. Worker
        # must fetch actual receipt from the authenticated TextVerified API.
        assert one.code is None and one.status == 'pending'
        assert await s.scalar(select(func.count()).select_from(TextVerifiedWebhookReceipt)) == 1
    await engine.dispose()


@pytest.mark.asyncio
async def test_mismatching_order_id_never_wakes_another_customer(credentials, signed_event, monkeypatch):
    engine = create_async_engine('sqlite+aiosqlite:///:memory:')
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(tv, 'Session', sessions)
    now = utcnow()
    async with sessions.begin() as s:
        s.add(Activation(id=1, provider='textverified', provider_order_id='abc123',
                         user_id=7, service='telegram', country='us',
                         phone='15557778888', status='pending',
                         next_poll_at=now+timedelta(hours=1),
                         created_at=now-timedelta(minutes=1),
                         expires_at=now+timedelta(minutes=10),
                         cost_provider=1, price_charged=2))
    signed_event['data']['reservationId'] = 'not-abc123'
    raw = json.dumps(signed_event).encode()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=web),
                                 base_url='http://test') as client:
        res = await client.post('/webhook/textverified', content=raw,
                                headers=signed_headers(raw))
    assert res.status_code == 200
    async with sessions() as s:
        row = await s.get(Activation, 1)
        assert row.next_poll_at.replace(tzinfo=timezone.utc) > utcnow()
        assert row.code is None
    await engine.dispose()
