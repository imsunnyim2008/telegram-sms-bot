"""NOWPayments hosted invoices and verified payment-status API.

The two secrets are configured in Render, never in repository files.
Payment-credit decisions are made by the app's ledger logic after a fresh
authenticated GET /payment/{id}, not from a webhook payload alone.
"""
import hashlib
import hmac
import json
import re
from decimal import Decimal
from urllib.parse import urlparse

import httpx

from .config import settings
from .gateway import PaymentError

PAYMENT_ID = re.compile(r"^[0-9]{1,40}$")


def canonical_json(obj):
    """Follow NOWPayments' recursively sorted JSON for HMAC-SHA512."""
    if isinstance(obj, dict):
        return {key: canonical_json(obj[key]) for key in sorted(obj)}
    if isinstance(obj, list):
        return [canonical_json(value) for value in obj]
    return obj


class NowPayments:
    def __init__(self, client: httpx.AsyncClient | None = None):
        self.client = client or httpx.AsyncClient(timeout=20)

    async def request(self, method: str, path: str, *, json_body=None):
        # The key travels in a header, not a URL.
        if not settings.nowpayments_api_key:
            raise PaymentError("NOWPayments API key is missing")
        response = await self.client.request(
            method, settings.nowpayments_api_base.rstrip("/") + path,
            headers={"x-api-key": settings.nowpayments_api_key,
                     "Content-Type": "application/json"},
            json=json_body,
        )
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, (dict, list)):
            raise PaymentError("Unexpected NOWPayments response")
        return data

    async def check_credentials(self) -> None:
        await self.request("GET", "/merchant/coins")

    async def create_invoice(self, amount: Decimal, payment_id: str) -> dict:
        if not settings.nowpayments_ipn_secret or not settings.public_base_url:
            raise PaymentError("IPN secret and PUBLIC_BASE_URL are required")
        if not PAYMENT_ID.fullmatch(payment_id):
            raise PaymentError("Invalid local payment ID")
        order_id = "friday-topup-" + payment_id
        payload = {
            "price_amount": float(amount),
            "price_currency": settings.wallet_currency.lower(),
            "order_id": order_id,
            "order_description": "Friday SMS bot prepaid wallet top-up",
            "ipn_callback_url": settings.public_base_url.rstrip("/") + "/webhook/nowpayments",
        }
        result = await self.request("POST", "/invoice", json_body=payload)
        invoice_id = str(result.get("id", ""))
        invoice_url = str(result.get("invoice_url", ""))
        parsed = urlparse(invoice_url)
        if not PAYMENT_ID.fullmatch(invoice_id) or parsed.scheme != "https":
            raise PaymentError("NOWPayments returned invalid invoice data")
        if parsed.hostname not in {"nowpayments.io"} and not (
            parsed.hostname and parsed.hostname.endswith(".nowpayments.io")
        ):
            raise PaymentError("NOWPayments checkout URL is not trusted")
        if result.get("order_id") and result["order_id"] != order_id:
            raise PaymentError("NOWPayments order reference mismatch")
        return {"invoice_id": invoice_id, "pay_url": invoice_url}

    async def get_payment_status(self, payment_id: str) -> dict:
        if not PAYMENT_ID.fullmatch(payment_id):
            raise PaymentError("Invalid payment ID")
        data = await self.request("GET", "/payment/" + payment_id)
        if not isinstance(data, dict):
            raise PaymentError("Payment status response malformed")
        return data

    @staticmethod
    def verify_webhook(raw_body: bytes, signature: str) -> bool:
        if (not settings.nowpayments_ipn_secret or not signature
                or len(signature) != 128):
            return False
        try:
            obj = json.loads(raw_body)
            if not isinstance(obj, dict):
                return False
            # NOWPayments docs illustrate sorting the top-level parameters,
            # while SDKs also support deep-sorted nested fee objects. Both
            # representations MUST have a matching HMAC-SHA512 with the
            # merchant's IPN secret. Never accept unsigned payloads.
            candidates = [
                json.dumps(canonical_json(obj), separators=(",", ":"),
                           ensure_ascii=False).encode("utf-8"),
                json.dumps(dict(sorted(obj.items())), separators=(",", ":"),
                           ensure_ascii=False).encode("utf-8"),
            ]
        except (ValueError, UnicodeError, TypeError):
            return False
        key = settings.nowpayments_ipn_secret.strip().encode("utf-8")
        claimed = signature.lower()
        return any(hmac.compare_digest(
            hmac.new(key, body, hashlib.sha512).hexdigest(), claimed)
            for body in candidates)

    async def aclose(self) -> None:
        await self.client.aclose()
