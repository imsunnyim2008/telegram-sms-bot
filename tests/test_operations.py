from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

import app.handlers as handlers
from app.config import settings
from app.db import Activation, LedgerEntry, OpsNotice, Payment, PaymentInbox, utcnow
from app.ledger import apply
from app.operations import accept_finished_ipn, claim_purchase, reconcile_inbox, report, schedule_daily_report
import test_inbot_pricing_topup as fixtures

setup_database = fixtures.setup_database


@pytest.mark.asyncio
async def test_purchase_confirmation_claim_is_one_use(setup_database):
    session, _, _ = setup_database
    assert await claim_purchase(session, 99, 42)
    assert not await claim_purchase(session, 99, 42)
    assert await claim_purchase(session, 99, 43)
    assert await claim_purchase(session, 7031009238, 42)


@pytest.mark.asyncio
async def test_ledger_notices_atomic_and_deduplicated(setup_database):
    session, _, _ = setup_database
    async with session.begin() as s:
        assert await apply(s, 99, Decimal('5'), 'topup', 'payment', '123')
        assert not await apply(s, 99, Decimal('5'), 'topup', 'payment', '123')
        assert await apply(s, 99, Decimal('1'), 'refund', 'activation', '12')
    async with session() as s:
        rows = (await s.scalars(select(OpsNotice))).all()
        assert len(rows) == 2
        assert {row.recipient for row in rows} == {99, 7031009238}
        assert all(row.status == 'pending' for row in rows)


@pytest.mark.asyncio
async def test_signed_inbox_retries_verified_credit_without_duplicates(setup_database, monkeypatch):
    session, shop, _ = setup_database
    async with session.begin() as s:
        s.add(Payment(id=123, user_id=99, gateway='nowpayments', gateway_invoice_id='456',
                      status='created', amount_fiat=Decimal('5'), amount_credit=Decimal('5')))
    attempts = []

    async def verify(invoice, payment, *, order_id):
        attempts.append(payment)
        if len(attempts) == 1:
            raise RuntimeError('temporary unavailable')
        assert (invoice, payment, order_id) == ('456', '789', 'friday-topup-123')
        async with session.begin() as s:
            p = await s.get(Payment, 123)
            p.status = 'paid'
            await apply(s, p.user_id, p.amount_credit, 'topup', 'payment', str(p.id))
        return True

    monkeypatch.setattr(shop, 'verify_and_credit_nowpayments', verify)
    await accept_finished_ipn(session, '789', '456', 'friday-topup-123')
    await accept_finished_ipn(session, '789', '456', 'friday-topup-123')
    await reconcile_inbox(session, shop)
    async with session.begin() as s:
        row = await s.get(PaymentInbox, '789')
        assert row.status == 'pending'
        assert row.attempts == 1
        row.next_attempt_at = utcnow()-timedelta(seconds=1)
    await reconcile_inbox(session, shop)
    await reconcile_inbox(session, shop)
    async with session() as s:
        assert (await s.get(PaymentInbox, '789')).status == 'credited'
        rows = (await s.scalars(select(LedgerEntry).where(LedgerEntry.ref_id == '123'))).all()
        assert len(rows) == 1
    assert attempts == ['789', '789']


@pytest.mark.asyncio
async def test_business_report_distinguishes_deposit_and_gross_profit(setup_database, monkeypatch):
    session, _, _ = setup_database
    monkeypatch.setattr(settings, 'wallet_currency', 'USD')
    now = utcnow()
    async with session.begin() as s:
        await apply(s, 99, Decimal('5'), 'topup', 'payment', '42')
        s.add(Activation(user_id=99, service='telegram', country='us', provider='textverified',
                         status='completed', completed_at=now, created_at=now,
                         expires_at=now+timedelta(minutes=15), cost_provider=Decimal('1'),
                         price_charged=Decimal('2')))
    text = await report(session, now-timedelta(days=1), now+timedelta(seconds=30))
    assert 'Deposits: 5.0000 USD' in text
    assert 'Estimated gross profit: 1.0000 USD' in text
    await schedule_daily_report(session)
    await schedule_daily_report(session)
    async with session() as s:
        rows = (await s.scalars(select(OpsNotice).where(OpsNotice.key.startswith('daily:')))).all()
        assert len(rows) == 1


@pytest.mark.asyncio
async def test_deposit_receipt_checks_owner_before_showing_details(setup_database):
    session, _, _ = setup_database
    async with session.begin() as s:
        s.add(Payment(id=777, user_id=7031009238, gateway='nowpayments',
                      gateway_invoice_id='987', status='created',
                      amount_fiat=Decimal('5'), amount_credit=Decimal('5')))
    customer = fixtures.Target(uid=99)
    await handlers.deposit_receipt(customer, 99, 777)
    assert 'Deposit not found' in customer.answers[-1][0]


@pytest.mark.asyncio
async def test_support_thread_checks_owner(setup_database):
    session, _, _ = setup_database
    async with session.begin() as s:
        s.add(Activation(id=321, user_id=7031009238, service='telegram', country='us',
                         status='completed', expires_at=utcnow()+timedelta(minutes=15),
                         cost_provider=Decimal('1'), price_charged=Decimal('2')))
    customer = fixtures.Target(uid=99)
    await handlers.support_thread(customer, 99, 321)
    assert 'Order not found' in customer.answers[-1][0]
