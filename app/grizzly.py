"""Grizzly SMS activation API adapter. Never retry a purchase request."""
import asyncio
import json
import logging
import re
import time
from decimal import Decimal, InvalidOperation

import httpx

from .config import settings
from .provider import AmbiguousPurchase, PriceItem, ProviderError

API_URL = "https://api.grizzlysms.com/stubs/handler_api.php"

# Grizzly uses short service codes, while the bot displays friendly names.
SERVICES = {
    "telegram": "tg", "whatsapp": "wa", "google": "go",
    "facebook": "fb", "instagram": "ig", "discord": "ds",
    "amazon": "am", "microsoft": "mm",
}
COUNTRY_ID = re.compile(r"^\d{1,6}$")
log = logging.getLogger(__name__)


class GrizzlySms:
    # The active-activations API does not prove there were no historical orders.
    # Uncertain purchases must be investigated by the operator, not auto-refunded.
    supports_complete_order_history = False

    def __init__(self, client: httpx.AsyncClient | None = None):
        self.client = client or httpx.AsyncClient(timeout=16)
        self._cache: list[PriceItem] = []
        self._cached_at = 0.0
        self._lock = asyncio.Lock()
        self.country_names: dict[str, str] = {}

    async def _request(self, action: str, params: dict | None = None,
                       *, purchase: bool = False):
        if not settings.provider_api_key:
            raise ProviderError("Grizzly API key not configured")
        # The provider requires query-string authentication. Do not log request
        # URLs: they contain credentials, even when a request fails.
        values = {"api_key": settings.provider_api_key, "action": action}
        if params:
            values.update(params)
        attempts = 1 if purchase else 3
        for attempt in range(attempts):
            try:
                response = await self.client.get(API_URL, params=values)
                if response.status_code >= 500:
                    if purchase:
                        raise AmbiguousPurchase("Grizzly purchase response uncertain")
                    if attempt + 1 < attempts:
                        await asyncio.sleep(2 ** attempt)
                        continue
                    raise ProviderError("Grizzly server temporarily unavailable")
                if response.status_code >= 400:
                    raise ProviderError(f"Grizzly API rejected request (HTTP {response.status_code})")
                raw = response.text.strip()
                if not raw:
                    if purchase:
                        raise AmbiguousPurchase("Grizzly purchase response empty")
                    raise ProviderError("Grizzly returned empty response")
                if raw in {"BAD_KEY", "NO_BALANCE", "NO_NUMBERS", "BAD_ACTION",
                           "NO_ACTIVATION", "ERROR_SQL", "SERVICE_UNAVAILABLE_REGION"} \
                        or raw.startswith("ERROR"):
                    raise ProviderError(f"Grizzly returned {raw[:80]}")
                try:
                    return json.loads(raw)
                except json.JSONDecodeError:
                    return raw
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if purchase:
                    raise AmbiguousPurchase("Grizzly purchase may have completed") from exc
                if attempt + 1 == attempts:
                    raise ProviderError("Grizzly API network unavailable") from exc
                await asyncio.sleep(2 ** attempt)
        raise ProviderError("Grizzly request unsuccessful")

    async def get_prices(self) -> list[PriceItem]:
        if self._cache and time.monotonic() - self._cached_at < 60:
            return self._cache
        async with self._lock:
            if self._cache and time.monotonic() - self._cached_at < 60:
                return self._cache
            data = await self._request("getPrices")
            if not isinstance(data, dict):
                raise ProviderError("Unexpected Grizzly pricing response")
            # API: country id -> service code -> {"cost": 1.2, "count": 4}
            choices: list[PriceItem] = []
            for country, services in data.items():
                country = str(country)
                if not COUNTRY_ID.fullmatch(country) or not settings.country_allowed(country):
                    continue
                if not isinstance(services, dict):
                    continue
                for name, code in SERVICES.items():
                    if not settings.service_allowed(name):
                        continue
                    offer = services.get(code)
                    if not isinstance(offer, dict):
                        continue
                    try:
                        cost = Decimal(str(offer["cost"]))
                        stock = int(offer["count"])
                    except (ValueError, TypeError, KeyError, InvalidOperation):
                        continue
                    if cost.is_finite() and cost > 0 and stock > 0:
                        choices.append(PriceItem(name, country, cost, stock))
            self._cache = choices
            self._cached_at = time.monotonic()
            return choices

    async def get_country_names(self) -> dict[str, str]:
        if self.country_names:
            return self.country_names
        try:
            data = await self._request("getCountries")
            if isinstance(data, list):
                items = data
            elif isinstance(data, dict):
                items = list(data.values())
            else:
                return {}
            self.country_names = {
                str(x["id"]): str(x.get("eng", "")).strip()
                for x in items if isinstance(x, dict) and x.get("id") is not None
                and isinstance(x.get("eng"), str) and x.get("eng")
            }
        except ProviderError:
            pass
        return self.country_names

    async def buy_number(self, service: str, country: str, raw_max_price: Decimal) -> dict:
        if service not in SERVICES or not COUNTRY_ID.fullmatch(country):
            raise ProviderError("Invalid service or country")
        # This API uses GET for a chargeable operation. Do not ever retry it.
        result = await self._request("getNumberV2", {
            "service": SERVICES[service],
            "country": country,
            "maxPrice": str(raw_max_price),
        }, purchase=True)
        if not isinstance(result, dict) or not result.get("activationId") or not result.get("phoneNumber"):
            raise AmbiguousPurchase("Grizzly returned an unexpected purchase confirmation")
        try:
            actual = Decimal(str(result["activationCost"]))
            if not actual.is_finite() or actual <= 0 or actual > raw_max_price:
                raise ValueError("Unexpected provider cost")
        except (ValueError, TypeError, KeyError, InvalidOperation) as exc:
            # Chargeable order may already exist; manual reconciliation is required.
            raise AmbiguousPurchase("Grizzly purchase cost requires review") from exc
        expiry = result.get("activationEnd")
        if isinstance(expiry, str) and expiry:
            # Provider returns a naive wall-clock timestamp. Without an explicit
            # timezone, use our conservative local 15-minute expiry instead.
            expiry = None
        return {
            "id": str(result["activationId"]),
            "phone": str(result["phoneNumber"]),
            "price": actual,
            "expires": expiry,
        }

    async def check(self, order_id: str) -> dict:
        result = await self._request("getStatus", {"id": str(int(order_id))})
        if not isinstance(result, str):
            raise ProviderError("Unexpected Grizzly activation status")
        if result.startswith("STATUS_OK:"):
            code = result.split(":", 1)[1].strip()
            return {"status": "RECEIVED", "sms": [{"code": code}]} if code else {"status": "PENDING", "sms": []}
        if result in {"STATUS_CANCEL", "STATUS_CANCELED"}:
            return {"status": "CANCELLED", "sms": []}
        if result in {"STATUS_WAIT_CODE", "STATUS_WAIT_RESEND"} or result.startswith("STATUS_WAIT_RETRY"):
            return {"status": "PENDING", "sms": []}
        raise ProviderError("Unexpected Grizzly activation status")

    async def finish(self, order_id: str) -> dict:
        result = await self._request("setStatus", {"id": str(int(order_id)), "status": 6})
        if result != "ACCESS_ACTIVATION":
            raise ProviderError("Grizzly did not confirm completion")
        return {"status": "FINISHED"}

    async def cancel(self, order_id: str) -> dict:
        result = await self._request("setStatus", {"id": str(int(order_id)), "status": 8})
        if result != "ACCESS_CANCEL":
            raise ProviderError("Grizzly did not confirm cancellation")
        return {"status": "CANCELLED"}

    async def get_balance(self) -> Decimal:
        result = await self._request("getBalance")
        if not isinstance(result, str) or not result.startswith("ACCESS_BALANCE:"):
            raise ProviderError("Unexpected Grizzly balance response")
        try:
            balance = Decimal(result.split(":", 1)[1])
            if not balance.is_finite():
                raise ValueError()
            return balance
        except (ValueError, InvalidOperation) as exc:
            raise ProviderError("Invalid Grizzly balance") from exc

    async def recent_orders(self) -> list[dict]:
        result = await self._request("getActiveActivations")
        return result if isinstance(result, list) else []

    async def aclose(self) -> None:
        await self.client.aclose()
