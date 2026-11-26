"""SMSPool provider, with explicit refund confirmation and no automatic purchase retry."""
import asyncio
import logging
import re
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

import httpx

from .config import settings
from .provider import AmbiguousPurchase, PriceItem, ProviderError

BASE = 'https://api.smspool.net'
log = logging.getLogger(__name__)
COUNTRIES = {'us': 'USA', 'ca': 'Canada', 'gb': 'UK', 'ng': 'Nigeria', 'jp': 'Japan'}
DEFAULT_SERVICES = 'telegram,whatsapp,google,facebook,instagram,discord,amazon,microsoft'
SLUG = re.compile(r'^[a-z0-9_]{1,40}$')
OID = re.compile(r'^[A-Za-z0-9_-]{1,90}$')


def money(value):
    try:
        amount = Decimal(str(value))
    except (ValueError, TypeError, InvalidOperation) as exc:
        raise ProviderError('Invalid SMSPool price') from exc
    if not amount.is_finite() or amount < 0:
        raise ProviderError('Invalid SMSPool price')
    return amount


class SMSPool:
    supports_complete_order_history = False

    def __init__(self, client=None):
        self.client = client or httpx.AsyncClient(timeout=12, follow_redirects=False)
        self._cache = []
        self._cached_at = 0.0
        self._lock = asyncio.Lock()

    async def _request(self, path, *, method='POST', data=None, purchase=False):
        if not settings.smspool_api_key:
            raise ProviderError('SMSPool API key not configured')
        try:
            response = await self.client.request(method, BASE + path, data=data,
                headers={'Authorization': 'Bearer ' + settings.smspool_api_key,
                         'Accept': 'application/json'})
            if not 200 <= response.status_code < 300:
                if purchase:
                    raise AmbiguousPurchase('SMSPool purchase response uncertain')
                raise ProviderError(f'SMSPool HTTP {response.status_code}')
            return response.json()
        except (httpx.RequestError, ValueError) as exc:
            if purchase:
                raise AmbiguousPurchase('SMSPool purchase response uncertain') from exc
            raise ProviderError('SMSPool request unavailable') from exc

    async def get_balance(self):
        data = await self._request('/request/balance')
        if not isinstance(data, dict) or 'balance' not in data:
            raise ProviderError('SMSPool balance unavailable')
        return money(data['balance'])

    async def get_country_names(self):
        return dict(COUNTRIES)

    async def get_prices(self, *, fresh=False):
        if not fresh and time.monotonic() - self._cached_at < 60:
            return self._cache
        async with self._lock:
            if not fresh and time.monotonic() - self._cached_at < 60:
                return self._cache
            catalog = await self._request('/service/retrieve_all', method='GET')
            if not isinstance(catalog, list):
                raise ProviderError('SMSPool catalogue unavailable')
            wanted = {x.strip().lower() for x in (settings.allowed_services or DEFAULT_SERVICES).split(',')}
            services = {}
            for row in catalog:
                if not isinstance(row, dict):
                    continue
                name = str(row.get('name', '')).strip().lower()
                ident = str(row.get('ID', row.get('id', '')))
                if name in wanted and SLUG.fullmatch(name) and ident.isdigit() and settings.service_allowed(name):
                    services[name] = ident
            if not services:
                raise ProviderError('No configured SMSPool services in catalogue')
            sem = asyncio.Semaphore(5)
            async def quote(service, ident, country):
                async with sem:
                    try:
                        data = await self._request('/sms/stock', data={
                            'country': country.upper(), 'service': ident, 'include_price': '1'})
                        if not isinstance(data, dict):
                            return None, False
                        # Do not invent a price or availability. Only accept an explicit
                        # positive stock quantity and quoted monetary amount.
                        stock = data.get('stock', data.get('count'))
                        price = data.get('price', data.get('cost'))
                        if stock is None:
                            return None, False
                        quantity = int(stock)
                        if quantity <= 0:
                            return None, True
                        if price is None:
                            return None, False
                        cost = money(price)
                        if cost <= 0:
                            return None, False
                        return PriceItem(service, country, cost, quantity), True
                    except (ProviderError, TypeError, ValueError, ArithmeticError):
                        return None, False
            results = await asyncio.gather(*(quote(s, ident, c) for s, ident in services.items()
                for c in COUNTRIES if settings.country_allowed(c)))
            if results and not any(ok for _, ok in results):
                raise ProviderError('SMSPool stock/price format not confirmed')
            self._cache = [item for item, _ in results if item is not None]
            self._cached_at = time.monotonic()
            return self._cache

    async def buy_number(self, service, country, raw_max_price):
        if not SLUG.fullmatch(service) or country not in COUNTRIES:
            raise ProviderError('Invalid SMSPool selection')
        if not settings.service_allowed(service) or not settings.country_allowed(country):
            raise ProviderError('SMSPool selection not enabled')
        cap = money(raw_max_price)
        if cap <= 0:
            raise ProviderError('Invalid SMSPool price cap')
        data = await self._request('/purchase/sms', data={
            'country': country.upper(), 'service': service, 'max_price': str(cap),
            'quantity': '1'}, purchase=True)
        if isinstance(data, dict) and str(data.get('success')) == '0':
            if data.get('type') in {'OUT_OF_STOCK', 'BALANCE_ERROR', 'PRICE_NOT_FOUND'}:
                raise ProviderError('SMSPool declined purchase')
            raise AmbiguousPurchase('Unclassified SMSPool purchase response')
        try:
            if str(data['success']) != '1':
                raise ValueError()
            oid, phone = str(data['order_id']), str(data['number'])
            price = money(data['cost'])
            if not OID.fullmatch(oid) or not phone.isdigit() or not 8 <= len(phone) <= 16 or not 0 < price <= cap:
                raise ValueError()
            expires = datetime.fromtimestamp(int(data['expiration']), tz=timezone.utc).isoformat()
            return {'id': oid, 'phone': phone, 'price': price, 'expires': expires}
        except (KeyError, TypeError, ValueError, OverflowError, OSError, ProviderError) as exc:
            raise AmbiguousPurchase('SMSPool allocation requires reconciliation') from exc

    async def check(self, order_id):
        if not OID.fullmatch(str(order_id)):
            raise ProviderError('Invalid SMSPool order ID')
        data = await self._request('/sms/check', data={'orderid': str(order_id)})
        if not isinstance(data, dict):
            raise ProviderError('SMSPool order status unavailable')
        status = str(data.get('status'))
        if status == '6':
            return {'status': 'CANCELLED', 'sms': []}
        if status == '3':
            code = data.get('sms')
            if not isinstance(code, str) or not code.strip():
                raise ProviderError('SMSPool completed order has no confirmed message')
            return {'status': 'RECEIVED', 'sms': [{'code': code}]}
        if status in {'1', '2', '4', '5', '7', '8'}:
            # Expired/cancelled alone is not confirmation of a financial refund.
            return {'status': 'PENDING', 'sms': []}
        raise ProviderError('Unrecognized SMSPool order status')

    async def finish(self, order_id):
        data = await self.check(order_id)
        return {'status': 'FINISHED' if data['status'] == 'RECEIVED' else 'PENDING'}

    async def cancel(self, order_id):
        if not OID.fullmatch(str(order_id)):
            raise ProviderError('Invalid SMSPool order ID')
        if (await self.check(order_id))['status'] == 'CANCELLED':
            return {'status': 'CANCELLED'}
        try:
            data = await self._request('/sms/cancel', data={'orderid': str(order_id)})
            if isinstance(data, dict) and str(data.get('success')) == '1':
                # The provider documents a successful cancellation as refunded.
                return {'status': 'CANCELLED'}
        except ProviderError:
            pass
        if (await self.check(order_id))['status'] == 'CANCELLED':
            return {'status': 'CANCELLED'}
        raise ProviderError('SMSPool refund not confirmed')

    async def recent_orders(self):
        return []

    async def aclose(self):
        await self.client.aclose()
