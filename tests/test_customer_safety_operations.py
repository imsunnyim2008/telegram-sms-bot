from datetime import timedelta
from decimal import Decimal

import pytest

import app.handlers as handlers
from app.db import Activation, LedgerEntry, Setting, utcnow
import test_inbot_pricing_topup as fixtures

setup_database = fixtures.setup_database


@pytest.mark.asyncio
async def test_confirmation_is_owner_only_and_idempotent(setup_database):
    session, _, _ = setup_database
    async with session.begin() as db:
        order = Activation(user_id=99, service='telegram', country='us',
                           status='completed', sms_notified=True,
                           expires_at=utcnow() + timedelta(minutes=15),
                           cost_provider=Decimal('1'), price_charged=Decimal('2'))
        db.add(order)
        await db.flush()
        aid = order.id
    other = fixtures.Target(uid=7031009238, text=f'/done {aid}')
    await handlers.customer_done(other)
    assert 'Order not found' in other.answers[-1][0]
    owner = fixtures.Target(uid=99, text=f'/done {aid}')
    await handlers.customer_done(owner)
    await handlers.customer_done(owner)
    assert 'marked done by you' in owner.answers[-1][0]
    async with session() as db:
        assert await db.get(Setting, f'customer_done:{aid}') is not None


@pytest.mark.asyncio
async def test_history_only_contains_own_transactions(setup_database):
    session, _, _ = setup_database
    async with session.begin() as db:
        for uid, amount in [(99, '1.2345'), (7031009238, '9.8765')]:
            db.add(LedgerEntry(user_id=uid, type='refund', ref_type='activation',
                               ref_id=str(uid), amount=Decimal(amount),
                               balance_after=Decimal('10')))
    msg = fixtures.Target(uid=99, text='/refunds')
    await handlers.transaction_history(msg)
    assert '1.2345' in msg.answers[-1][0]
    assert '9.8765' not in msg.answers[-1][0]
    group = fixtures.Target(uid=99, text='/history', chat_type='group')
    await handlers.transaction_history(group)
    assert not group.answers


@pytest.mark.asyncio
async def test_maintenance_requires_private_admin(setup_database):
    session, _, _ = setup_database
    guest = fixtures.Target(uid=99, text='/maintenance on')
    await handlers.admin_maintenance(guest)
    assert 'Admin only' in guest.answers[-1][0]
    admin = fixtures.Target(uid=7031009238, text='/maintenance on')
    await handlers.admin_maintenance(admin)
    async with session() as db:
        assert (await db.get(Setting, 'purchase_maintenance')).value == 'on'
    admin.text = '/maintenance off'
    await handlers.admin_maintenance(admin)
    async with session() as db:
        assert (await db.get(Setting, 'purchase_maintenance')).value == 'off'
