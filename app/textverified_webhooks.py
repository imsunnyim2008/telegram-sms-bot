"""Hardened receiver for the *textverified.com* public v2 webhook contract.

Official Westbold/PythonClient swagger:
- HMAC-SHA512=BASE64_DIGEST in X-Webhook-Signature
- event v2.sms.received, idempotencyKey, data.to and optional reservationId
This is NOT the unrelated textverified.org SHA256 webhook protocol.

The event only accelerates normal provider polling after an exact, single-owner
order match. No OTP from inbound payload can directly modify wallet/receipt.
"""
import base64
import hashlib
import hmac
import json
import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from .db import Activation, Session, TextVerifiedWebhookReceipt, utcnow

_HMAC_PREFIX = "HMAC-SHA512="
_PHONE = re.compile(r"^\+?[0-9]{9,20}$")


def valid_signature(body: bytes, signature: str, secret: str) -> bool:
    if not secret.startswith("whsec_") or not signature.startswith(_HMAC_PREFIX):
        return False
    expected = base64.b64encode(
        hmac.new(secret.encode(), body, hashlib.sha512).digest()
    ).decode("ascii")
    return hmac.compare_digest(signature[len(_HMAC_PREFIX):], expected)


def parse_event(body: bytes) -> dict:
    try:
        event = json.loads(body)
    except (ValueError, TypeError) as exc:
        raise ValueError("Invalid webhook JSON") from exc
    if not isinstance(event, dict):
        raise ValueError("Invalid webhook JSON")
    if event.get("event") != "v2.sms.received":
        return {"event": "other"}
    key = event.get("idempotencyKey")
    data = event.get("data")
    if not isinstance(key, str) or not 1 <= len(key) <= 160:
        raise ValueError("Invalid webhook idempotency key")
    if not isinstance(data, dict):
        raise ValueError("Invalid webhook data")
    phone = data.get("to")
    occurred = data.get("createdAt")
    if not isinstance(phone, str) or not _PHONE.fullmatch(phone):
        raise ValueError("Invalid SMS destination")
    try:
        timestamp = datetime.fromisoformat(str(occurred).replace("Z", "+00:00"))
        if timestamp.tzinfo is None:
            raise ValueError()
        timestamp = timestamp.astimezone(timezone.utc)
    except (TypeError, ValueError) as exc:
        raise ValueError("Invalid webhook timestamp") from exc
    # Provider has a unique identifier for an owned reservation if supplied.
    reservation_id = data.get("reservationId")
    if reservation_id is not None and (
        not isinstance(reservation_id, str) or len(reservation_id) > 90
    ):
        raise ValueError("Invalid provider order reference")
    return {
        "event": "v2.sms.received",
        "key": hashlib.sha256(key.encode()).hexdigest(),
        "phone": phone.lstrip("+"),
        "reservation_id": reservation_id,
        "sms_time": timestamp,
    }


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


async def wake_matching_orders(event: dict) -> bool:
    """Deduplicate and reschedule, never trust webhook data as an OTP."""
    if event["event"] != "v2.sms.received":
        return False
    try:
        async with Session.begin() as s:
            s.add(TextVerifiedWebhookReceipt(key=event["key"]))
            await s.flush()
            # An active order must match exact TextVerified number and, if
            # present, the provider-owned order ID. Never forward to a
            # different user's reissued number.
            rows = (await s.scalars(select(Activation).where(
                Activation.provider == "textverified",
                Activation.status == "pending",
                Activation.phone == event["phone"],
            ).limit(3).with_for_update())).all()
            matched = [
                row for row in rows
                if (event["reservation_id"] is None or
                    row.provider_order_id == event["reservation_id"])
                and _aware(row.created_at) - timedelta(seconds=120)
                <= event["sms_time"]
                <= _aware(row.expires_at) + timedelta(seconds=120)
            ]
            if len(matched) != 1:
                return False
            matched[0].next_poll_at = utcnow()
            return True
    except IntegrityError:
        # Retried same signed event, already scheduled; never double-trigger.
        return False


async def prune_old_receipts():
    """Reduce event receipt storage without retaining messages or codes."""
    async with Session.begin() as s:
        await s.execute(delete(TextVerifiedWebhookReceipt).where(
            TextVerifiedWebhookReceipt.processed_at <
            utcnow() - timedelta(days=30)
        ))
