"""Real-money pilot is restricted to owners until provider webhooks are verified."""
from decimal import Decimal

import pytest

from app.business import BotError, Shop
from app.config import settings


@pytest.mark.asyncio
async def test_non_admin_cannot_create_live_payment_invoice(monkeypatch):
    monkeypatch.setattr(settings, 'payment_provider', 'nowpayments')
    monkeypatch.setattr(settings, 'payments_enabled', True)
    monkeypatch.setattr(settings, 'nowpayments_api_key', 'test-key')
    monkeypatch.setattr(settings, 'nowpayments_ipn_secret', 'test-ipn')
    monkeypatch.setattr(settings, 'public_base_url', 'https://bot.example')
    monkeypatch.setattr(settings, 'payment_test_admin_only', True)
    shop = Shop(None, None, None)
    with pytest.raises(BotError, match='admin-only testing'):
        await shop.start_payment(-5678943, Decimal('5'))


@pytest.mark.asyncio
async def test_disabled_payment_rejects_even_admin(monkeypatch):
    monkeypatch.setattr(settings, 'payments_enabled', False)
    monkeypatch.setattr(settings, 'payment_test_admin_only', True)
    shop = Shop(None, None, None)
    with pytest.raises(BotError, match='not enabled'):
        await shop.start_payment(-5678943, Decimal('5'))



@pytest.mark.asyncio
async def test_provider_verified_finished_payment_manual_recovery(monkeypatch):
    import app.business as business
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy import select
    from app.db import Base, LedgerEntry, Payment, User
    engine = create_async_engine('sqlite+aiosqlite:///:memory:')
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sess = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(business, 'Session', sess)
    monkeypatch.setattr(settings, 'payment_provider', 'nowpayments')
    monkeypatch.setattr(settings, 'payments_enabled', True)
    monkeypatch.setattr(settings, 'nowpayments_api_key', 'mock')
    monkeypatch.setattr(settings, 'nowpayments_ipn_secret', 'mock')
    monkeypatch.setattr(settings, 'public_base_url', 'https://bot.example.com')
    async with sess.begin() as db:
        db.add(User(id=999, balance=Decimal('0'), accepted_terms=True))
        db.add(Payment(id=10, user_id=999, gateway='nowpayments',
                       gateway_invoice_id='1234', status='created',
                       amount_fiat=Decimal('5'), amount_credit=Decimal('5')))
    class Gateway:
        async def get_payment_status(self, payment_id):
            return {'payment_id': int(payment_id), 'invoice_id': None,
                    'order_id': 'friday-topup-10', 'price_amount': 5,
                    'price_currency': 'usd', 'actually_paid': 5,
                    'payment_status': 'finished'}
    class Notifier:
        async def send(self, *_): pass
    shop = Shop(None, Gateway(), Notifier())
    assert await shop.verify_and_credit_nowpayments(None, '777', order_id='friday-topup-10')
    assert not await shop.verify_and_credit_nowpayments(None, '777', order_id='friday-topup-10')
    async with sess() as db:
        assert (await db.get(User, 999)).balance == Decimal('5')
        assert len((await db.scalars(select(LedgerEntry))).all()) == 1
    await engine.dispose()
