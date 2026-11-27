"""TextVerified.com /api/pub/v2 SMS verification adapter.

Official service uses account-email + API-key token exchange. It currently
supplies US non-VoIP numbers; do not invent other countries. Purchases are
NOT retried. Paid order IDs are opaque (not Grizzly numeric activation IDs).
"""
import asyncio
import logging
import re
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from urllib.parse import urlparse

import httpx

from .config import settings
from .provider import AmbiguousPurchase, PriceItem, ProviderError

log = logging.getLogger(__name__)
BASE = "https://www.textverified.com"
SLUG = re.compile(r"^[a-z0-9_]{1,50}$")
ORDER_ID = re.compile(r"^[a-zA-Z0-9_-]{1,90}$")
US = "us"


def _decimal(value) -> Decimal:
    try:
        val = Decimal(str(value))
    except (TypeError, ValueError, InvalidOperation) as exc:
        raise ProviderError("Invalid TextVerified price") from exc
    if not val.is_finite() or val < 0:
        raise ProviderError("Invalid TextVerified price")
    return val


def _time(value: str) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        d = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


class TextVerifiedSms:
    # A missing match in history is not proof of a non-purchase.
    supports_complete_order_history = False

    def __init__(self, client: httpx.AsyncClient | None = None):
        self.client = client or httpx.AsyncClient(timeout=16, follow_redirects=False)
        self._token: str | None = None
        self._token_until = 0.0
        self._auth_lock = asyncio.Lock()
        self._price_lock = asyncio.Lock()
        self._catalogue_lock = asyncio.Lock()
        self._catalogue = None
        self._catalogue_at = 0.0
        self._cache: list[PriceItem] = []
        self._cached_at = 0.0
        # Order ID -> exact destination number; recorded after confirmed buy.
        # Check also works after a restart by fetching provider order details.
        self._known_numbers: dict[str, str] = {}

    @staticmethod
    def _credentials() -> tuple[str, str]:
        if not settings.textverified_api_username or not settings.textverified_api_key:
            raise ProviderError("TextVerified API username/key not configured")
        return settings.textverified_api_username, settings.textverified_api_key

    async def _auth(self) -> str:
        if self._token and time.monotonic() < self._token_until:
            return self._token
        async with self._auth_lock:
            if self._token and time.monotonic() < self._token_until:
                return self._token
            username, key = self._credentials()
            try:
                response = await self.client.post(
                    BASE + "/api/pub/v2/auth",
                    headers={"X-API-USERNAME": username, "X-API-KEY": key},
                )
            except httpx.RequestError as exc:
                raise ProviderError("TextVerified authentication network error") from exc
            if response.status_code != 200:
                raise ProviderError(f"TextVerified authentication failed (HTTP {response.status_code})")
            try:
                data = response.json()
                token = data["token"]
                ttl = float(data["expiresIn"])
                if not isinstance(token, str) or not token or ttl <= 0:
                    raise ValueError()
            except (KeyError, TypeError, ValueError) as exc:
                raise ProviderError("Invalid TextVerified authentication response") from exc
            self._token = token
            self._token_until = time.monotonic() + max(1, ttl - 45)
            return token

    @staticmethod
    def _safe_path(path: str) -> str:
        """Follow provider action links only on the known API host."""
        if path.startswith("/api/pub/v2/"):
            return path
        parsed = urlparse(path)
        if (parsed.scheme == "https" and parsed.netloc == "www.textverified.com"
                and parsed.path.startswith("/api/pub/v2/") and not parsed.query
                and not parsed.fragment and not parsed.username):
            return parsed.path
        raise AmbiguousPurchase("TextVerified returned an unsafe verification link")

    async def _request(self, method: str, path: str, *, payload: dict | None = None,
                       params: dict | None = None, purchase: bool = False):
        token = await self._auth()
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        if purchase:
            # A unique key per attempt; never generate a second attempt on timeout.
            headers["Idempotency-Key"] = uuid.uuid4().hex
        safe_path = self._safe_path(path)
        try:
            response = await self.client.request(
                method, BASE + safe_path, headers=headers, json=payload, params=params,
            )
        except httpx.RequestError as exc:
            if purchase:
                raise AmbiguousPurchase("TextVerified purchase outcome uncertain") from exc
            raise ProviderError("TextVerified network unavailable") from exc
        if response.status_code >= 500 and purchase:
            raise AmbiguousPurchase("TextVerified purchase outcome uncertain")
        if not 200 <= response.status_code < 300:
            raise ProviderError(f"TextVerified request failed (HTTP {response.status_code})")
        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError as exc:
            if purchase:
                raise AmbiguousPurchase("TextVerified purchase response unclear") from exc
            raise ProviderError("TextVerified returned invalid JSON") from exc

    async def catalogue_names(self) -> list[str]:
        """Search the complete SMS catalogue without requesting thousands of quotes."""
        async with self._catalogue_lock:
            now = time.monotonic()
            if self._catalogue is not None and now - self._catalogue_at < 300:
                return list(self._catalogue)
            data = await self._request("GET", "/api/pub/v2/services",
                params={"numberType": "mobile", "reservationType": "verification"})
            if not isinstance(data, list):
                raise ProviderError("TextVerified service catalogue unavailable")
            names = {row["serviceName"].strip().lower()
                     for row in data if isinstance(row, dict)
                     and isinstance(row.get("serviceName"), str)
                     and 0 < len(row["serviceName"].strip()) <= 100
                     and row["serviceName"].isprintable()
                     and str(row.get("capability", "")).lower() in
                         {"sms", "smsandvoicecombo"}}
            self._catalogue = sorted(names)
            self._catalogue_at = now
            return list(self._catalogue)

    async def get_prices(self, *, fresh: bool = False) -> list[PriceItem]:
        if not fresh and self._cache and time.monotonic() - self._cached_at < 60:
            return self._cache
        async with self._price_lock:
            if not fresh and self._cache and time.monotonic() - self._cached_at < 60:
                return self._cache
            if not settings.country_allowed(US):
                return []
            data = await self._request(
                "GET", "/api/pub/v2/services",
                params={"numberType": "mobile", "reservationType": "verification"},
            )
            if not isinstance(data, list):
                raise ProviderError("TextVerified service catalogue unavailable")
            allowed = {}
            for row in data:
                if (not isinstance(row, dict) or
                        str(row.get("capability", "")).lower() not in
                        {"sms", "smsandvoicecombo"}):
                    continue
                name = str(row.get("serviceName", "")).lower()
                if SLUG.fullmatch(name) and settings.service_allowed(name):
                    allowed[name] = True

            sem = asyncio.Semaphore(4)
            failures = 0

            async def quote(service):
                nonlocal failures
                async with sem:
                    request = {"serviceName": service, "capability": "sms",
                               "numberType": "mobile"}
                    try:
                        inventory = await self._request(
                            "POST", "/api/pub/v2/inventory/verifications",
                            payload=request,
                        )
                        qty = int(inventory["availableQuantity"])
                        if qty <= 0:
                            return None
                        pricing = await self._request(
                            "POST", "/api/pub/v2/pricing/verifications",
                            payload={**request, "areaCode": False, "carrier": False},
                        )
                        cost = _decimal(pricing["price"])
                        if cost == 0:
                            return None
                        return PriceItem(service, US, cost, qty)
                    except (ProviderError, ValueError, TypeError, KeyError):
                        failures += 1
                        log.warning("TextVerified quote unavailable for a permitted service")
                        return None

            found = await asyncio.gather(*(quote(s) for s in sorted(allowed)))
            if fresh and allowed and failures == len(allowed):
                raise ProviderError('TextVerified inventory/pricing requests all failed')
            offers = [p for p in found if p is not None]
            self._cache = offers
            self._cached_at = time.monotonic()
            return offers

    async def catalogue_capabilities(self) -> dict[str, int | None]:
        """Read-only catalogue availability, not purchase eligibility or stock."""
        async def inspect(kind):
            try:
                data = await self._request("GET", "/api/pub/v2/services",
                    params={"numberType": "mobile", "reservationType": kind})
                if not isinstance(data, list):
                    return kind, None
                names = {str(r.get("serviceName", "")).lower() for r in data
                         if isinstance(r, dict) and r.get("serviceName")
                         and str(r.get("capability", "")).lower() in
                         {"sms", "smsandvoicecombo"}}
                return kind, len(names)
            except ProviderError:
                return kind, None
        return dict(await asyncio.gather(*(inspect(kind) for kind in
                    ("verification", "nonrenewable", "renewable"))))

    async def confirmed_refund(self, order_id: str) -> bool:
        """Check an already-linked order without fetching messages or purchasing."""
        oid = self._oid(order_id)
        details = await self._details(oid)
        return (isinstance(details, dict) and str(details.get("id", "")) == oid
                and str(details.get("state", "")).lower() == "verificationrefunded")

    async def get_country_names(self) -> dict[str, str]:
        return {US: "USA"}

    async def buy_number(self, service: str, country: str,
                         raw_max_price: Decimal) -> dict:
        if (country != US or not SLUG.fullmatch(service) or
                not settings.service_allowed(service)):
            raise ProviderError("Only currently supported US TextVerified services can be purchased")
        if _decimal(raw_max_price) <= 0:
            raise ProviderError("Invalid maximum price")
        # Reconfirm that service is in the catalogue/stock before charging.
        if not any(i.service == service for i in await self.get_prices()):
            raise ProviderError("Service no longer available")
        creation = await self._request(
            "POST", "/api/pub/v2/verifications",
            payload={"serviceName": service, "capability": "sms",
                     "maxPrice": float(raw_max_price)},
            purchase=True,
        )
        if not isinstance(creation, dict) or creation.get("method") != "GET":
            raise AmbiguousPurchase("TextVerified did not return a verification link")
        path = self._safe_path(creation.get("href", ""))
        # Follow the action link to retrieve the purchased number (safe GET).
        # A paid POST may already have created a number. Even if the follow-up
        # GET fails, NEVER tell Shop it was a failed purchase and refund it.
        try:
            details = await self._request("GET", path)
        except ProviderError as exc:
            raise AmbiguousPurchase(
                "TextVerified number allocated but details unavailable; manual review required"
            ) from exc
        try:
            oid = str(details["id"])
            phone = str(details["number"])
            price = _decimal(details["totalCost"])
            expires = details.get("endsAt")
            if (not ORDER_ID.fullmatch(oid) or not phone.isdigit() or
                    len(phone) < 9 or price > raw_max_price or not price):
                raise ValueError()
        except (ValueError, KeyError, TypeError, ProviderError) as exc:
            raise AmbiguousPurchase("TextVerified order details require review") from exc
        self._known_numbers[oid] = phone
        return {"id": oid, "phone": phone, "price": price, "expires": expires}

    @staticmethod
    def _oid(order_id: str) -> str:
        value = str(order_id)
        if not ORDER_ID.fullmatch(value):
            raise ProviderError("Invalid TextVerified verification ID")
        return value

    async def _details(self, order_id: str) -> dict:
        return await self._request("GET",
                                   f"/api/pub/v2/verifications/{self._oid(order_id)}")

    async def check(self, order_id: str) -> dict:
        oid = self._oid(order_id)
        details = await self._details(oid)
        if not isinstance(details, dict):
            raise ProviderError("Invalid TextVerified order response")
        state = str(details.get("state", "")).lower()
        if state == "verificationrefunded":
            # A confirmed refund does not depend on the separate SMS history endpoint.
            return {"status": "CANCELLED", "sms": []}
        phone = str(details.get("number") or self._known_numbers.get(oid) or "")
        if not phone.isdigit():
            raise ProviderError("TextVerified number unavailable")
        self._known_numbers[oid] = phone
        # Only the account's own SMS messages for this number and
        # order creation window can supply a verification code.
        history = await self._request("GET", "/api/pub/v2/sms",
                                      params={"to": phone, "reservationType": "verification"})
        messages = history.get("data", []) if isinstance(history, dict) else []
        began = _time(details.get("createdAt"))
        finished = _time(details.get("endsAt"))
        codes = []
        for msg in messages:
            if not isinstance(msg, dict) or msg.get("to") != phone:
                continue
            arrived = _time(msg.get("createdAt"))
            if not arrived or (began and arrived < began) or (finished and arrived > finished):
                continue
            code = msg.get("parsedCode")
            if code and isinstance(code, str):
                codes.append((arrived, code))
        if codes:
            codes.sort(key=lambda x: x[0])
            return {"status": "RECEIVED", "sms": [{"code": codes[-1][1]}]}
        if state in ("verificationrefunded", "verificationcanceled"):
            # Only a confirmed provider refund can release reserved wallet funds.
            return {"status": "CANCELLED" if state == "verificationrefunded" else "PENDING",
                    "sms": []}
        if state == "verificationtimedout":
            return {"status": "PENDING", "sms": []}
        return {"status": "PENDING", "sms": []}

    async def finish(self, order_id: str) -> dict:
        # TextVerified marks the verification complete when an SMS arrives.
        details = await self._details(order_id)
        if str(details.get("state", "")).lower() == "verificationcompleted":
            return {"status": "FINISHED"}
        # Some SMS can arrive while the verification state is still updating.
        return {"status": "PENDING"}

    async def cancel(self, order_id: str) -> dict:
        oid = self._oid(order_id)
        details = await self._details(oid)
        if str(details.get("state", "")).lower() != "verificationrefunded":
            if details.get("cancel", {}).get("canCancel") is True:
                try:
                    await self._request("POST", f"/api/pub/v2/verifications/{oid}/cancel")
                except ProviderError:
                    # A timed-out cancellation may have been applied. Do not
                    # credit the customer unless the provider confirms refund.
                    pass
                details = await self._details(oid)
        if str(details.get("state", "")).lower() == "verificationrefunded":
            return {"status": "CANCELLED"}
        raise ProviderError("TextVerified refund not confirmed; wallet unchanged")

    async def get_balance(self) -> Decimal:
        data = await self._request("GET", "/api/pub/v2/account/me")
        if not isinstance(data, dict):
            raise ProviderError("TextVerified balance unavailable")
        return _decimal(data["currentBalance"])

    async def recent_orders(self) -> list[dict]:
        # Retain manual review for ambiguous purchase outcomes. Provider
        # history cannot securely bind a match to a specific bot user.
        return []

    async def aclose(self) -> None:
        await self.client.aclose()
