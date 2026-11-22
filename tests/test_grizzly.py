"""Offline integration checks for Grizzly's published handler API protocol."""
from decimal import Decimal

import httpx
import pytest

from app.config import settings
from app.grizzly import GrizzlySms
from app.provider import AmbiguousPurchase, ProviderError


@pytest.fixture(autouse=True)
def fake_api_key(monkeypatch):
    monkeypatch.setattr(settings, "provider_api_key", "testing-only-not-real")


@pytest.mark.asyncio
async def test_prices_countries_and_balance():
    def reply(request):
        assert request.url.params["api_key"] == "testing-only-not-real"
        action = request.url.params["action"]
        if action == "getPrices":
            return httpx.Response(200, json={
                "0": {"tg": {"cost": 12, "count": 3},
                      "wa": {"cost": "9.75", "count": 1}},
                "73": {"tg": {"cost": 1.3, "count": 0}},
            })
        if action == "getBalance":
            return httpx.Response(200, text="ACCESS_BALANCE:100.25")
        raise AssertionError(action)

    async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
        adapter = GrizzlySms(client)
        items = await adapter.get_prices()
        assert {(i.service, i.country) for i in items} == {
            ("telegram", "0"), ("whatsapp", "0"),
        }
        assert next(i for i in items if i.service == "telegram").cost == Decimal("12")
        assert await adapter.get_balance() == Decimal("100.25")


@pytest.mark.asyncio
async def test_purchase_status_completion_and_cancel():
    calls = []
    def reply(request):
        action = request.url.params["action"]
        calls.append(action)
        if action == "getNumberV2":
            assert request.url.params["service"] == "tg"
            assert request.url.params["country"] == "0"
            assert request.url.params["maxPrice"] == "5"
            return httpx.Response(200, json={
                "activationId": 123,
                "phoneNumber": "447700900123",
                "activationCost": 4.5,
                "currency": 643,
            })
        if action == "getStatus":
            return httpx.Response(200, text="STATUS_OK:123456")
        if action == "setStatus":
            if request.url.params["status"] == "6":
                return httpx.Response(200, text="ACCESS_ACTIVATION")
            return httpx.Response(200, text="ACCESS_CANCEL")
        raise AssertionError(action)

    async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
        adapter = GrizzlySms(client)
        order = await adapter.buy_number("telegram", "0", Decimal("5"))
        assert order["id"] == "123"
        assert order["phone"] == "447700900123"
        status = await adapter.check("123")
        assert status["sms"][0]["code"] == "123456"
        assert (await adapter.finish("123"))["status"] == "FINISHED"
        assert (await adapter.cancel("123"))["status"] == "CANCELLED"
    assert calls.count("getNumberV2") == 1


@pytest.mark.asyncio
async def test_purchase_timeout_is_not_retried():
    attempts = []
    def reply(request):
        attempts.append(request.url.params["action"])
        raise httpx.ConnectError("connection broke")
    async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
        with pytest.raises(AmbiguousPurchase):
            await GrizzlySms(client).buy_number("telegram", "0", Decimal("5"))
    assert attempts == ["getNumberV2"]


@pytest.mark.asyncio
async def test_rejects_wrong_api_key():
    def reply(request):
        return httpx.Response(200, text="BAD_KEY")
    async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
        with pytest.raises(ProviderError):
            await GrizzlySms(client).get_balance()


@pytest.mark.asyncio
async def test_high_actual_cost_kept_for_manual_review():
    def reply(request):
        return httpx.Response(200, json={
            "activationId": 56, "phoneNumber": "15555550123",
            "activationCost": 10, "currency": 643,
        })
    async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
        with pytest.raises(AmbiguousPurchase):
            await GrizzlySms(client).buy_number("telegram", "0", Decimal("5"))
