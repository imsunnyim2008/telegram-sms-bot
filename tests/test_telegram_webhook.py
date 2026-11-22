"""Webhook auth, deduplication, and SMS recovery without live external APIs."""
import hashlib
from decimal import Decimal

import httpx
import pytest
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from app.config import settings
from app.db import Base, Activation, User
from app.main import web


@pytest.mark.asyncio
async def test_signed_webhook_routes_start_and_blocks_duplicate_paid_action(monkeypatch):
    import app.main as main
    engine = create_async_engine('sqlite+aiosqlite:///:memory:')
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(main, 'Session', session)
    monkeypatch.setattr(settings, 'bot_token', 'demo-only:bot-token')
    calls = []

    class Dispatcher:
        async def feed_update(self, bot, event):
            calls.append((event.update_id, event.message.text))

    monkeypatch.setitem(main.runtime, 'bot', object())
    monkeypatch.setitem(main.runtime, 'dp', Dispatcher())
    update = {
        'update_id': 99991,
        'message': {
            'message_id': 1, 'date': 1763790000,
            'chat': {'id': 221, 'type': 'private'},
            'from': {'id': 221, 'is_bot': False, 'first_name': 'Test'},
            'text': '/start',
        },
    }
    secret = hashlib.sha256(b'Friday-webhook-v1:demo-only:bot-token').hexdigest()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=web),
                                 base_url='https://test.example') as client:
        invalid = await client.post('/webhook/telegram', json=update)
        assert invalid.status_code == 403
        good = await client.post('/webhook/telegram', json=update,
                                 headers={'x-telegram-bot-api-secret-token': secret})
        assert good.status_code == 200
        repeated = await client.post('/webhook/telegram', json=update,
                                     headers={'x-telegram-bot-api-secret-token': secret})
        assert repeated.status_code == 200
        assert repeated.json()['duplicate'] is True
    assert calls == [(99991, '/start')]
    main.runtime.pop('bot', None)
    main.runtime.pop('dp', None)
    await engine.dispose()


@pytest.mark.asyncio
async def test_sms_check_replays_saved_code_only_to_order_owner(monkeypatch):
    import app.handlers as handlers
    engine = create_async_engine('sqlite+aiosqlite:///:memory:')
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(handlers, 'Session', session)
    async with session.begin() as s:
        s.add(User(id=221, balance=Decimal('0'), accepted_terms=True))
        s.add(User(id=222, balance=Decimal('0'), accepted_terms=True))
        s.add(Activation(id=10, user_id=221, provider='grizzly',
                         provider_order_id='321',
                         service='telegram', country='0', phone='15555550123',
                         cost_provider=Decimal('1'), price_charged=Decimal('1.5'),
                         status='completed', code='654321', sms_notified=True,
                         expires_at=__import__('app.db', fromlist=['utcnow']).utcnow()))

    class Target:
        def __init__(self): self.answers = []
        async def answer(self, msg): self.answers.append(msg)
    owner, stranger = Target(), Target()
    await handlers.check_sms(stranger, 222, 10)
    await handlers.check_sms(owner, 221, 10)
    assert stranger.answers == ['Order not found.']
    assert len(owner.answers) == 1
    assert '654321' in owner.answers[0]
    assert '654321' not in stranger.answers[0]
    await engine.dispose()
