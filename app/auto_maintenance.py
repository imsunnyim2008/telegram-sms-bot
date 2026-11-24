"""Read-only health decisions; never auto-resume an operator-owned pause."""
import json
from secrets import token_hex

from sqlalchemy import insert, literal, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from .db import MaintenanceNotice, Setting, User, utcnow
from .operations import audit

KEY = 'textverified_auto_state'


async def locked_state(s):
    # Serialize manual decisions and monitor transitions, including first use.
    dialect = s.bind.dialect.name
    stmt = (pg_insert if dialect == 'postgresql' else sqlite_insert)(Setting)
    await s.execute(stmt.values(key=KEY, value='{}').on_conflict_do_nothing(index_elements=['key']))
    row = await s.get(Setting, KEY, with_for_update=True)
    return row, json.loads(row.value)


async def set_mode(s, value):
    row = await s.get(Setting, 'purchase_maintenance', with_for_update=True)
    previous = row.value if row else 'off'
    if previous == value:
        return False
    if row:
        row.value = value
    else:
        s.add(Setting(key='purchase_maintenance', value=value))
    now = utcnow()
    await s.execute(insert(MaintenanceNotice).from_select(
        ['user_id', 'state', 'status', 'attempts', 'created_at', 'next_attempt_at'],
        select(User.id, literal(value), literal('pending'), literal(0),
               literal(now), literal(now)).where(
            User.accepted_terms.is_(True), User.is_banned.is_(False))))
    return True


async def manual_mode(session, value, *, actor=None):
    if value not in ('on', 'off'):
        raise ValueError('Invalid maintenance mode')
    async with session.begin() as s:
        row, state = await locked_state(s)
        # An explicit operator decision takes precedence even if mode is unchanged.
        state['auto_owned'] = False
        if state.get('incident'):
            state['ack'] = True
        row.value = json.dumps(state)
        if actor is not None:
            audit(s, actor, 'maintenance', value)
        return await set_mode(s, value)


async def acknowledge(session, incident=None):
    async with session.begin() as s:
        row, state = await locked_state(s)
        if not state.get('incident') or (incident and incident != state['incident']):
            return False
        state['ack'] = True
        row.value = json.dumps(state)
        return True


async def observe(session, healthy, now):
    """Record one actual observation. Gaps reset the continuous recovery clock."""
    async with session.begin() as s:
        row, state = await locked_state(s)
        event = None
        if not healthy:
            state.pop('healthy_since', None)
            if not state.get('incident'):
                state.update(incident=token_hex(6), started=now, ack=False)
                event = 'outage'
            if (not state.get('ack') and not state.get('auto_owned')
                    and now - state['started'] >= 1800):
                if await set_mode(s, 'on'):
                    state['auto_owned'] = True
                    event = 'paused'
        elif state.get('incident'):
            last = state.get('last_sample')
            if 'healthy_since' not in state or last is None or now - last > 120:
                state['healthy_since'] = now
                event = 'recovering' if state.get('auto_owned') else 'recovered'
            if not state.get('auto_owned'):
                state.pop('incident', None)
                state.pop('started', None)
                state.pop('ack', None)
                state.pop('healthy_since', None)
            elif now - state['healthy_since'] >= 600:
                if await set_mode(s, 'off'):
                    event = 'resumed'
                state.pop('incident', None)
                state.pop('started', None)
                state.pop('ack', None)
                state.pop('healthy_since', None)
                state['auto_owned'] = False
        state['last_sample'] = now
        if event:
            state.setdefault('notices', []).append({
                'id': token_hex(8), 'event': event, 'incident': state.get('incident'),
                'created': now})
        row.value = json.dumps(state)
        return state


async def pending_notices(session):
    async with session() as s:
        row = await s.get(Setting, KEY)
        return json.loads(row.value).get('notices', []) if row else []


async def mark_notice_sent(session, notice_id, admin_id, admins):
    async with session.begin() as s:
        row, state = await locked_state(s)
        for notice in state.get('notices', []):
            if notice['id'] == notice_id:
                sent = notice.setdefault('sent', [])
                if admin_id not in sent:
                    sent.append(admin_id)
        state['notices'] = [n for n in state.get('notices', [])
                            if not set(admins).issubset(n.get('sent', []))]
        row.value = json.dumps(state)
