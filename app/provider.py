"""5SIM provider interface. Buying is NEVER retried automatically."""
import asyncio
import re
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

import httpx

from .config import settings

API = 'https://5sim.net/v1'
SLUG = re.compile(r'^[a-z0-9_-]{1,24}$')


class ProviderError(Exception):
    pass


class AmbiguousPurchase(ProviderError):
    """A purchase request could have reached the provider; reconciliation is required."""


@dataclass(frozen=True)
class PriceItem:
    service: str
    country: str
    cost: Decimal
    stock: int


class ProviderClient(Protocol):
    async def get_prices(self) -> list[PriceItem]: ...
    async def buy_number(self, service: str, country: str, raw_max_price: Decimal) -> dict: ...
    async def check(self, order_id: str) -> dict: ...
    async def finish(self, order_id: str) -> dict: ...
    async def cancel(self, order_id: str) -> dict: ...
    async def get_balance(self) -> Decimal: ...


class FiveSim:
    def __init__(self, client: httpx.AsyncClient | None = None):
        self.client = client or httpx.AsyncClient(timeout=15)
        self._cache: list[PriceItem] = []
        self._cached_at = 0.0
        self._cache_lock = asyncio.Lock()

    def _headers(self, auth: bool = True) -> dict:
        h = {'Accept': 'application/json'}
        if auth:
            h['Authorization'] = f'Bearer {settings.provider_api_key}'
        return h

    async def _get(self, path: str, *, auth: bool = True, params: dict | None = None,
                   retry: bool = True) -> dict:
        for attempt in range(3 if retry else 1):
            try:
                response = await self.client.get(API + path, headers=self._headers(auth), params=params)
                if retry and response.status_code >= 500 and attempt < 2:
                    await asyncio.sleep(2 ** attempt)
                    continue
                if response.status_code >= 400:
                    raise ProviderError(f'Provider HTTP {response.status_code}')
                if 'no free phones' in response.text.lower():
                    raise ProviderError('No free phones available')
                data = response.json()
                if not isinstance(data, dict):
                    raise ProviderError('Provider returned unexpected response')
                return data
            except (httpx.TransportError, ValueError) as exc:
                if not retry:
                    raise AmbiguousPurchase('Purchase result cannot be confirmed') from exc
                if attempt == 2:
                    raise ProviderError('Provider network unavailable') from exc
                await asyncio.sleep(2 ** attempt)
        raise ProviderError('Provider request failed')

    async def get_prices(self) -> list[PriceItem]:
        if self._cache and time.monotonic() - self._cached_at < 60:
            return self._cache
        async with self._cache_lock:
            if self._cache and time.monotonic() - self._cached_at < 60:
                return self._cache
            data = await self._get('/guest/prices', auth=False)
            # Response: {country: {service: {operator: {cost, count}}}}
            found: dict[tuple[str, str], PriceItem] = {}
            for country, products in data.items():
                if not isinstance(products, dict) or not SLUG.fullmatch(country):
                    continue
                if not settings.country_allowed(country):
                    continue
                for service, operators in products.items():
                    if not isinstance(operators, dict) or not SLUG.fullmatch(service):
                        continue
                    if not settings.service_allowed(service):
                        continue
                    offers = []
                    for offer in operators.values():
                        if not isinstance(offer, dict):
                            continue
                        try:
                            count = int(offer.get('count', 0))
                            cost = Decimal(str(offer.get('cost', 0)))
                        except (ValueError, TypeError, ArithmeticError):
                            continue
                        if count > 0 and cost > 0:
                            offers.append((cost, count))
                    if offers:
                        lowest = min(c for c, _ in offers)
                        stock = sum(q for c, q in offers if c == lowest)
                        found[(service, country)] = PriceItem(service, country, lowest, stock)
            self._cache = list(found.values())
            self._cached_at = time.monotonic()
            return self._cache

    async def buy_number(self, service: str, country: str, raw_max_price: Decimal) -> dict:
        if not (SLUG.fullmatch(service) and SLUG.fullmatch(country)):
            raise ProviderError('Invalid service or country')
        # A non-idempotent GET; a failed/unknown result must NOT be retried.
        path = f'/user/buy/activation/{country}/any/{service}'
        try:
            result = await self._get(path, params={'maxPrice': str(raw_max_price)}, retry=False)
        except ProviderError as exc:
            if isinstance(exc, AmbiguousPurchase):
                raise
            # A 5xx may happen AFTER the provider purchased; never refund blindly.
            if 'HTTP 5' in str(exc):
                raise AmbiguousPurchase('Provider may have allocated a number') from exc
            raise
        if not result.get('id') or not result.get('phone'):
            raise AmbiguousPurchase('Provider returned no order identifier')
        return result

    async def check(self, order_id: str) -> dict:
        return await self._get(f'/user/check/{int(order_id)}')

    async def finish(self, order_id: str) -> dict:
        return await self._get(f'/user/finish/{int(order_id)}')

    async def cancel(self, order_id: str) -> dict:
        return await self._get(f'/user/cancel/{int(order_id)}')

    async def get_balance(self) -> Decimal:
        data = await self._get('/user/profile')
        return Decimal(str(data['balance']))

    async def recent_orders(self) -> list[dict]:
        data = await self._get('/user/orders', params={
            'category': 'activation', 'limit': '50', 'offset': '0',
            'order': 'id', 'reverse': 'true',
        })
        return data.get('Data', []) if isinstance(data.get('Data'), list) else []

    async def aclose(self) -> None:
        await self.client.aclose()
