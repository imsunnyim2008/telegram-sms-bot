import pytest
from sqlalchemy import select

from app.auto_maintenance import acknowledge, manual_mode, observe, pending_notices
from app.db import MaintenanceNotice, Setting
import test_inbot_pricing_topup as fixtures

setup_database = fixtures.setup_database


async def mode(session):
    async with session() as s:
        row = await s.get(Setting, 'purchase_maintenance')
        return row.value if row else 'off'


@pytest.mark.asyncio
async def test_outage_auto_pauses_and_recovers_after_ten_continuous_minutes(setup_database):
    session, _, _ = setup_database
    await observe(session, False, 1000)
    for minute in range(1, 30):
        await observe(session, False, 1000 + 60 * minute)
    assert await mode(session) == 'off'
    await observe(session, False, 2800)
    assert await mode(session) == 'on'
    for minute in range(10):
        await observe(session, True, 2860 + 60 * minute)
    assert await mode(session) == 'on'
    await observe(session, True, 3460)
    assert await mode(session) == 'off'
    assert [n['event'] for n in await pending_notices(session)] == [
        'outage', 'paused', 'recovering', 'resumed']
    async with session() as s:
        notices = (await s.scalars(select(MaintenanceNotice))).all()
        assert len(notices) == 4
        assert {n.state for n in notices} == {'on', 'off'}


@pytest.mark.asyncio
async def test_acknowledgement_is_incident_specific(setup_database):
    session, _, _ = setup_database
    state = await observe(session, False, 1000)
    assert not await acknowledge(session, 'wrong-id')
    assert await acknowledge(session, state['incident'])
    await observe(session, False, 3000)
    assert await mode(session) == 'off'
    await observe(session, True, 3060)
    state = await observe(session, False, 3120)
    assert not state['ack']
    await observe(session, False, 4920)
    assert await mode(session) == 'on'


@pytest.mark.asyncio
async def test_manual_pause_never_auto_resumes(setup_database):
    session, _, _ = setup_database
    await manual_mode(session, 'on')
    await observe(session, False, 1000)
    await observe(session, False, 2800)
    for minute in range(11):
        await observe(session, True, 2860 + 60 * minute)
    assert await mode(session) == 'on'


@pytest.mark.asyncio
async def test_manual_override_and_recovery_gap(setup_database):
    session, _, _ = setup_database
    await observe(session, False, 1000)
    await observe(session, False, 2800)
    await observe(session, True, 2860)
    await observe(session, True, 3600)  # missed checks cannot prove continuous recovery
    assert await mode(session) == 'on'
    await observe(session, False, 3660)  # relapse resets recovery
    await observe(session, True, 3720)
    await manual_mode(session, 'on')  # take explicit ownership of an automatic pause
    for minute in range(1, 12):
        await observe(session, True, 3720 + minute * 60)
    assert await mode(session) == 'on'


@pytest.mark.asyncio
async def test_ack_after_auto_pause_does_not_cancel_recovery(setup_database):
    session, _, _ = setup_database
    await observe(session, False, 1000)
    state = await observe(session, False, 2800)
    assert await acknowledge(session, state['incident'])
    for minute in range(11):
        await observe(session, True, 2860 + 60 * minute)
    assert await mode(session) == 'off'
