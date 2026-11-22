"""MockTransport contract tests for TextVerified.com API (no live purchases)."""
from decimal import Decimal

import httpx
import pytest

from app.config import settings
from app.provider import AmbiguousPurchase, ProviderError
from app.textverified import TextVerifiedSms


@pytest.fixture(autouse=True)
def config(monkeypatch):
    monkeypatch.setattr(settings, "textverified_api_username", "test@example.com")
    monkeypatch.setattr(settings, "textverified_api_key", "TEST_SECRET_ONLY")
    monkeypatch.setattr(settings, "allowed_services", "telegram,whatsapp,facebook")
    monkeypatch.setattr(settings, "allowed_countries", "")
    monkeypatch.setattr(settings, "blocked_services", "")


@pytest.mark.asyncio
async def test_auth_service_inventory_pricing_and_us_only():
    calls = []
    def reply(req):
        path = req.url.path
        calls.append(path)
        if path.endswith("/auth"):
            assert req.headers["X-API-USERNAME"] == "test@example.com"
            assert req.headers["X-API-KEY"] == "TEST_SECRET_ONLY"
            return httpx.Response(200, json={"token": "BEARER", "expiresIn": 899})
        assert req.headers["Authorization"] == "Bearer BEARER"
        if path.endswith("/services"):
            assert req.url.params["reservationType"] == "verification"
            return httpx.Response(200, json=[
                {"serviceName": "telegram", "capability": "sms"},
                {"serviceName": "whatsapp", "capability": "sms"},
                {"serviceName": "notallowed", "capability": "sms"},
            ])
        if path.endswith("/inventory/verifications"):
            return httpx.Response(200, json={"availableQuantity": 14})
        if path.endswith("/pricing/verifications"):
            return httpx.Response(200, json={"serviceName": "telegram", "price": 2.50})
        if path.endswith("/account/me"):
            return httpx.Response(200, json={"currentBalance": 8.25})
        raise AssertionError(path)

    async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
        p = TextVerifiedSms(client)
        items = await p.get_prices()
        assert len(items) == 2
        assert all(i.country == "us" for i in items)
        assert {i.service for i in items} == {"telegram", "whatsapp"}
        assert all(i.cost == Decimal("2.5") and i.stock == 14 for i in items)
        assert await p.get_country_names() == {"us": "USA"}
        assert await p.get_balance() == Decimal("8.25")
        assert len(await p.get_prices()) == 2
    assert calls.count("/api/pub/v2/services") == 1
    assert calls.count("/api/pub/v2/auth") == 1


@pytest.mark.asyncio
async def test_purchase_code_finish_and_confirmed_refund():
    state = {"sms": False, "state": "verificationPending", "posts": 0}
    def reply(req):
        path = req.url.path
        if path.endswith("/auth"):
            return httpx.Response(200, json={"token": "BEARER", "expiresIn": 899})
        if path.endswith("/services"):
            return httpx.Response(200, json=[{"serviceName": "telegram", "capability": "sms"}])
        if path.endswith("/inventory/verifications"):
            return httpx.Response(200, json={"availableQuantity": 6})
        if path.endswith("/pricing/verifications"):
            return httpx.Response(200, json={"price": 1.00})
        if path == "/api/pub/v2/verifications" and req.method == "POST":
            state["posts"] += 1
            assert req.headers.get("Idempotency-Key")
            assert req.content and b'"serviceName":"telegram"' in req.content
            return httpx.Response(200, json={
                "method": "GET", "href": "https://www.textverified.com/api/pub/v2/verifications/abc-123"
            })
        if path == "/api/pub/v2/verifications/abc-123" and req.method == "GET":
            return httpx.Response(200, json={
                "id": "abc-123", "number": "15557778888", "totalCost": 1.00,
                "serviceName": "telegram", "state": state["state"],
                "createdAt": "2026-11-22T00:00:00Z",
                "endsAt": "2026-11-22T00:15:00Z",
                "cancel": {"canCancel": True},
            })
        if path == "/api/pub/v2/sms":
            return httpx.Response(200, json={"data": [{
                "id": "m1", "to": "15557778888", "createdAt": "2026-11-22T00:03:00Z",
                "parsedCode": "654321",
            }]} if state["sms"] else {"data": []})
        if path == "/api/pub/v2/verifications/abc-123/cancel":
            state["state"] = "verificationRefunded"
            return httpx.Response(204)
        raise AssertionError((req.method, path))

    async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
        p = TextVerifiedSms(client)
        items = await p.get_prices()
        assert len(items) == 1
        order = await p.buy_number("telegram", "us", Decimal("1.0"))
        assert order["id"] == "abc-123"
        assert order["phone"] == "15557778888"
        assert state["posts"] == 1
        assert (await p.check("abc-123"))["status"] == "PENDING"
        state["sms"] = True
        state["state"] = "verificationCompleted"
        assert (await p.check("abc-123"))["sms"][0]["code"] == "654321"
        assert (await p.finish("abc-123"))["status"] == "FINISHED"
        state["sms"] = False
        state["state"] = "verificationPending"
        assert (await p.cancel("abc-123"))["status"] == "CANCELLED"


@pytest.mark.asyncio
async def test_purchase_timeout_not_retried_and_refund_not_faked():
    calls = []
    def reply(req):
        if req.url.path.endswith("/auth"):
            return httpx.Response(200, json={"token": "x", "expiresIn": 899})
        if req.url.path.endswith("/services"):
            return httpx.Response(200, json=[{"serviceName": "telegram", "capability": "sms"}])
        if req.url.path.endswith("/inventory/verifications"):
            return httpx.Response(200, json={"availableQuantity": 3})
        if req.url.path.endswith("/pricing/verifications"):
            return httpx.Response(200, json={"price": 0.90})
        if req.url.path == "/api/pub/v2/verifications":
            calls.append("purchase")
            raise httpx.ReadTimeout("unknown outcome")
        raise AssertionError(str(req.url))

    async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
        p = TextVerifiedSms(client)
        with pytest.raises(AmbiguousPurchase):
            await p.buy_number("telegram", "us", Decimal("1"))
        assert calls == ["purchase"]


@pytest.mark.asyncio
async def test_rejects_untrusted_provider_action_link():
    def reply(req):
        if req.url.path.endswith("/auth"):
            return httpx.Response(200, json={"token": "x", "expiresIn": 899})
        if req.url.path.endswith("/services"):
            return httpx.Response(200, json=[{"serviceName": "telegram", "capability": "sms"}])
        if req.url.path.endswith("/inventory/verifications"):
            return httpx.Response(200, json={"availableQuantity": 1})
        if req.url.path.endswith("/pricing/verifications"):
            return httpx.Response(200, json={"price": 1})
        if req.url.path.endswith("/verifications"):
            return httpx.Response(200, json={
                "method": "GET", "href": "https://attacker.example/api/pub/v2/verifications/x"
            })
        raise AssertionError(str(req.url))

    async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
        with pytest.raises(AmbiguousPurchase):
            await TextVerifiedSms(client).buy_number("telegram", "us", Decimal("1"))


@pytest.mark.asyncio
async def test_cancel_requires_actual_refund():
    def reply(req):
        p = req.url.path
        if p.endswith("/auth"):
            return httpx.Response(200, json={"token": "x", "expiresIn": 899})
        if p.endswith("/verifications/test123"):
            return httpx.Response(200, json={"state": "verificationCanceled", "cancel": {"canCancel": False}})
        raise AssertionError(p)

    async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
        with pytest.raises(ProviderError, match="refund not confirmed"):
            await TextVerifiedSms(client).cancel("test123")
