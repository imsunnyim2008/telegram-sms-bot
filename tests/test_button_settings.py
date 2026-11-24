from decimal import Decimal
from datetime import timedelta

import pytest

import app.handlers as handlers
from app.db import Activation, Setting, User, utcnow
import test_inbot_pricing_topup as fixtures

setup_database = fixtures.setup_database


class Click:
    def __init__(self, target, data):
        self.message = target
        self.from_user = target.from_user
        self.data = data

    async def answer(self, *args, **kwargs):
        pass


def buttons(markup):
    return {b.callback_data for row in markup.inline_keyboard for b in row}


@pytest.mark.asyncio
async def test_maintenance_command_displays_both_buttons_and_toggles(setup_database):
    session, _, _ = setup_database
    admin = fixtures.Target(uid=7031009238, text='/maintenance')
    await handlers.admin_maintenance(admin)
    assert {'ui:maint:on', 'ui:maint:off'} <= buttons(admin.answers[-1][1]['reply_markup'])
    await handlers.callbacks(Click(admin, 'ui:maint:on'))
    assert 'ON' in admin.edits[-1][0]
    async with session() as db:
        assert (await db.get(Setting, 'purchase_maintenance')).value == 'on'
    guest = fixtures.Target(uid=99)
    await handlers.callbacks(Click(guest, 'ui:maint:off'))
    assert 'Admin only' in guest.answers[-1][0]
    async with session() as db:
        assert (await db.get(Setting, 'purchase_maintenance')).value == 'on'
    await handlers.callbacks(Click(admin, 'ui:maint:off'))
    assert 'OFF' in admin.edits[-1][0]
    async with session() as db:
        assert (await db.get(Setting, 'purchase_maintenance')).value == 'off'


@pytest.mark.asyncio
async def test_adjust_requires_single_use_confirmation(setup_database):
    session, _, _ = setup_database
    admin = fixtures.Target(uid=7031009238)
    await handlers.callbacks(Click(admin, 'ui:adjust:99'))
    admin.text = '1'
    await handlers.handle_pending_text(admin)
    admin.text = 'Test adjustment'
    await handlers.handle_pending_text(admin)
    confirm = next(b for b in buttons(admin.answers[-1][1]['reply_markup']) if b.startswith('ui:confirm:'))
    async with session() as db:
        before = (await db.get(User, 99)).balance
    guest = fixtures.Target(uid=99)
    await handlers.callbacks(Click(guest, confirm))
    await handlers.callbacks(Click(admin, confirm))
    await handlers.callbacks(Click(admin, confirm))
    async with session() as db:
        assert (await db.get(User, 99)).balance == before + Decimal('1')


@pytest.mark.asyncio
async def test_order_picker_lists_only_own_orders(setup_database):
    session, _, _ = setup_database
    async with session.begin() as db:
        for uid in [99, 7031009238]:
            db.add(Activation(user_id=uid, service='telegram', country='us',
                              status='completed', sms_notified=True,
                              expires_at=utcnow()+timedelta(minutes=15),
                              cost_provider=Decimal('1'), price_charged=Decimal('2')))
    guest = fixtures.Target(uid=99, text='/receipt')
    await handlers.order_receipt(guest)
    choices = buttons(guest.answers[-1][1]['reply_markup'])
    assert 'ui:receipt:1' in choices
    assert 'ui:receipt:2' not in choices
    await handlers.callbacks(Click(guest, 'ui:order:2'))
    assert 'Order not found' in guest.answers[-1][0]
