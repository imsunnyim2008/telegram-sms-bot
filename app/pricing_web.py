"""Admin-only pricing UI backed by PostgreSQL; opened as a Telegram Mini App.

The HTML page contains no secrets. API calls require a correctly signed and
fresh Telegram WebApp.initData value whose user ID is in ADMIN_IDS.
"""
import hashlib
import hmac
import json
import re
import time
from decimal import Decimal, InvalidOperation, ROUND_UP
from pathlib import Path
from urllib.parse import parse_qsl

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import select

from .business import Shop, Q
from .config import settings
from .country_ui import country_flag
from .db import Session, Setting
from .provider import ProviderError


router = APIRouter()
_PRICE_KEY = re.compile(r'^price:([a-z0-9_]{1,35}):([0-9]{1,6})$')
_NO_CACHE = {'Cache-Control': 'no-store'}


def admin_from_init_data(payload: str, *, now: int | None = None) -> int:
    """Verify Telegram's HMAC data-check-string, not client-provided user ID."""
    if not isinstance(payload, str) or not payload or len(payload) > 8192:
        raise HTTPException(status_code=401, detail='Open Price Manager from Telegram /pricing')
    pairs = parse_qsl(payload, keep_blank_values=True)
    if len(pairs) != len(set(key for key, _ in pairs)):
        raise HTTPException(status_code=401, detail='Invalid Telegram session')
    data = dict(pairs)
    claim = data.get('hash', '')
    if not re.fullmatch(r'[0-9a-fA-F]{64}', claim):
        raise HTTPException(status_code=401, detail='Invalid Telegram session')
    # Telegram's optional signature is for third-party public-key validation.
    check = '\n'.join(f'{key}={value}' for key, value in sorted(data.items())
                      if key not in ('hash', 'signature'))
    key = hmac.new(b'WebAppData', settings.bot_token.encode(), hashlib.sha256).digest()
    expected = hmac.new(key, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, claim.lower()):
        raise HTTPException(status_code=401, detail='Invalid Telegram session')
    try:
        auth_at = int(data['auth_date'])
        clock = now if now is not None else int(time.time())
        user = json.loads(data['user'])
        uid = int(user['id'])
    except (ValueError, TypeError, KeyError, json.JSONDecodeError):
        raise HTTPException(status_code=401, detail='Invalid Telegram user')
    if auth_at > clock + 60 or clock - auth_at > 12 * 3600:
        raise HTTPException(status_code=401, detail='Telegram session expired; reopen /pricing')
    if uid not in settings.admins:
        raise HTTPException(status_code=403, detail='Admin only')
    return uid


def require_admin(request: Request) -> int:
    return admin_from_init_data(request.headers.get('x-telegram-init-data', ''))


@router.get('/admin/pricing', response_class=HTMLResponse)
async def admin_pricing_page() -> HTMLResponse:
    # Public shell, no user data or secret embedded. Prices API is protected.
    page = Path(__file__).with_name('pricing_page.html').read_text(encoding='utf-8')
    return HTMLResponse(content=page, headers={
        **_NO_CACHE,
        'X-Content-Type-Options': 'nosniff',
        'Referrer-Policy': 'no-referrer',
    })


@router.get('/admin/api/pricing')
async def admin_get_prices(request: Request) -> JSONResponse:
    require_admin(request)
    from .main import runtime  # late import avoids circular startup references
    shop = runtime.get('shop')
    if not shop:
        raise HTTPException(status_code=503, detail='Bot is starting')
    try:
        items = await shop.provider.get_prices()
        names = (await shop.provider.get_country_names()
                 if hasattr(shop.provider, 'get_country_names') else {})
    except ProviderError:
        raise HTTPException(status_code=503, detail='Unable to fetch live provider prices')
    async with Session() as s:
        markup_row = await s.get(Setting, 'markup_percent')
        override_rows = (await s.scalars(select(Setting).where(
            Setting.key.like('price:%')))).all()
    markup = Decimal(markup_row.value) if markup_row else settings.markup_percent
    overrides = {entry.key: Decimal(entry.value) for entry in override_rows
                 if _PRICE_KEY.fullmatch(entry.key)}
    rows = []
    for item in items:
        name = names.get(item.country, f'Country #{item.country}')
        code = f'price:{item.service}:{item.country}'
        override = overrides.get(code)
        rows.append({
            'service': item.service,
            'country_id': item.country,
            'country': name,
            'flag': country_flag(name),
            'provider_cost': str(item.cost),
            'base_cost': str((item.cost * settings.provider_price_to_wallet).quantize(
                Q, rounding=ROUND_UP)),
            'stock': item.stock,
            'price': str(Shop.price_from_settings(item, markup, override)),
            'custom': str(override) if override is not None else None,
        })
    return JSONResponse(content={
        'currency': settings.wallet_currency,
        'markup': str(markup),
        'count': len(rows),
        'offers': rows,
    }, headers=_NO_CACHE)


@router.post('/admin/api/pricing')
async def admin_save_prices(request: Request) -> JSONResponse:
    require_admin(request)
    if int(request.headers.get('content-length', '0') or 0) > 2048:
        raise HTTPException(status_code=413, detail='Request too large')
    try:
        payload = await request.json()
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail='Invalid request')
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail='Invalid request')
    action = payload.get('action')
    if action == 'markup':
        try:
            value = Decimal(str(payload['value']))
            if (not value.is_finite() or value < 0 or value > 500 or
                    value.as_tuple().exponent < -2):
                raise ValueError()
        except (ValueError, KeyError, InvalidOperation):
            raise HTTPException(status_code=400, detail='Markup must be 0–500% (2 decimals)')
        async with Session.begin() as s:
            row = await s.get(Setting, 'markup_percent')
            if row:
                row.value = str(value)
            else:
                s.add(Setting(key='markup_percent', value=str(value)))
        return JSONResponse({'ok': True, 'markup': str(value)}, headers=_NO_CACHE)

    if action not in {'set', 'reset'}:
        raise HTTPException(status_code=400, detail='Unsupported pricing action')
    service = payload.get('service')
    country = payload.get('country_id')
    if (not isinstance(service, str) or not isinstance(country, str) or
            not _PRICE_KEY.fullmatch(f'price:{service}:{country}') or
            not settings.service_allowed(service) or
            not settings.country_allowed(country)):
        raise HTTPException(status_code=400, detail='Invalid service or country')
    # Only real currently available provider offers can be adjusted.
    from .main import runtime
    shop = runtime.get('shop')
    if not shop:
        raise HTTPException(status_code=503, detail='Bot is starting')
    try:
        offers = await shop.provider.get_prices()
    except ProviderError:
        raise HTTPException(status_code=503, detail='Provider temporarily unavailable')
    offer = next((x for x in offers if x.service == service and x.country == country), None)
    if offer is None:
        raise HTTPException(status_code=404, detail='No live offer for this selection')
    key = f'price:{service}:{country}'
    if action == 'set':
        try:
            value = Decimal(str(payload['value']))
            if (not value.is_finite() or value <= 0 or value > 1000 or
                    value.as_tuple().exponent < -4):
                raise ValueError()
            floor = (offer.cost * settings.provider_price_to_wallet).quantize(
                Q, rounding=ROUND_UP)
            if value < floor:
                raise HTTPException(
                    status_code=400,
                    detail=f'Price must cover the provider cost ({floor} {settings.wallet_currency})')
        except (ValueError, KeyError, InvalidOperation):
            raise HTTPException(status_code=400, detail='Enter a positive price with up to 4 decimals')
    async with Session.begin() as s:
        row = await s.get(Setting, key)
        if action == 'reset':
            if row:
                await s.delete(row)
        elif row:
            row.value = str(value)
        else:
            s.add(Setting(key=key, value=str(value)))
    return JSONResponse({'ok': True}, headers=_NO_CACHE)
