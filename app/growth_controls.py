"""Conservative business controls: no automatic credits or unsolicited notices."""
import re
from decimal import Decimal
from secrets import token_hex

from sqlalchemy import select

from .db import Setting, User, utcnow
from .operations import audit, notice, insert_once

SERVICE = re.compile(r'^[a-z0-9_]{1,40}$')
PROMO = re.compile(r'^[A-Z0-9]{4,20}$')


async def service_paused(s, service):
    row = await s.get(Setting, 'service_pause:' + service)
    return bool(row and row.value == 'on')


async def set_service_pause(session, actor, service, state):
    if not SERVICE.fullmatch(service) or state not in ('on', 'off'):
        raise ValueError('Invalid service or state')
    async with session.begin() as s:
        key = 'service_pause:' + service
        await insert_once(s, Setting, 'key', {'key': key, 'value': state})
        row = await s.get(Setting, key, with_for_update=True)
        row.value = state
        audit(s, actor, 'service_maintenance', service + ':' + state)


def margin_discount(price, base, percent):
    """Discount only the margin; never sell below provider cost."""
    if not percent.is_finite() or not Decimal('0') <= percent <= Decimal('100'):
        return price
    return max(base, price - max(Decimal('0'), price-base)*percent/100)


async def set_promo(session, actor, code, percent):
    if not PROMO.fullmatch(code) or not percent.is_finite() or not Decimal('0') <= percent <= Decimal('100'):
        raise ValueError('Invalid promo')
    async with session.begin() as s:
        key = 'promo:' + code
        await insert_once(s, Setting, 'key', {'key': key, 'value': str(percent)})
        row = await s.get(Setting, key, with_for_update=True)
        row.value = str(percent)
        audit(s, actor, 'promo_config', code + ':' + str(percent))


async def redeem_promo(session, uid, code):
    if not PROMO.fullmatch(code):
        return False
    async with session.begin() as s:
        user = await s.get(User, uid, with_for_update=True)
        promo = await s.get(Setting, 'promo:' + code)
        if not user or user.is_banned or not user.accepted_terms or not promo or Decimal(promo.value) <= 0:
            return False
        key = 'promo_user:' + str(uid)
        await insert_once(s, Setting, 'key', {'key': key, 'value': code})
        row = await s.get(Setting, key, with_for_update=True)
        row.value = code
        return True


async def watch_stock(session, uid, service, country):
    if not SERVICE.fullmatch(service) or not SERVICE.fullmatch(country):
        raise ValueError('Invalid stock selection')
    async with session.begin() as s:
        user = await s.get(User, uid, with_for_update=True)
        if not user or user.is_banned or not user.accepted_terms:
            return False
        key = f'watch:{uid}:{service}:{country}'
        keys = (await s.scalars(select(Setting.key).where(Setting.key.startswith(f'watch:{uid}:')).limit(6))).all()
        if len(keys) >= 5 and key not in keys:
            return False
        await insert_once(s, Setting, 'key', {'key': key, 'value': token_hex(8)})
        return True


async def stock_notices(session, shop):
    async with session() as s:
        rows = (await s.scalars(select(Setting).where(Setting.key.startswith('watch:')).limit(100))).all()
    if not rows:
        return
    offers = {(p.service, p.country) for p in await shop.provider.get_prices() if p.stock > 0}
    async with session.begin() as s:
        for item in rows:
            _, uid, service, country = item.key.split(':')
            if (service, country) not in offers or await service_paused(s, service):
                continue
            row = await s.get(Setting, item.key, with_for_update=True)
            if row is None:
                continue
            user = await s.get(User, int(uid))
            if user and user.accepted_terms and not user.is_banned:
                await notice(s, f'stock:{row.value}', int(uid),
                             f'Stock update: {service} / {country} is currently available. Availability may change.')
            await s.delete(row)


async def record_referral(session, uid, referrer):
    if uid == referrer or referrer <= 0:
        return False
    async with session.begin() as s:
        user = await s.get(User, uid, with_for_update=True)
        other = await s.get(User, referrer)
        if not user or not other or other.is_banned or not other.accepted_terms or user.accepted_terms:
            return False
        key = f'referral:{uid}'
        if await s.get(Setting, key):
            return False
        await insert_once(s, Setting, 'key', {'key': key, 'value': str(referrer)})
        return True


async def referral_count(session, uid):
    async with session() as s:
        rows = (await s.scalars(select(Setting.key).where(
            Setting.key.startswith('referral:'), Setting.value == str(uid)))).all()
    return len(rows)
