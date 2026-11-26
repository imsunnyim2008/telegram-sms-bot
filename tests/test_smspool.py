import httpx
import pytest
from types import SimpleNamespace
from decimal import Decimal

from app.config import settings
from app.smspool import SMSPool
from app.provider import AmbiguousPurchase


@pytest.mark.asyncio
async def test_smspool_catalogue_and_refund(monkeypatch):
    monkeypatch.setattr(settings, 'smspool_api_key', 'test-only')
    monkeypatch.setattr('app.smspool.time', SimpleNamespace(monotonic=lambda: 10.0))
    monkeypatch.setattr(settings, 'allowed_services', 'discord')
    monkeypatch.setattr(settings, 'allowed_countries', 'us')
    def respond(request):
        assert request.headers['Authorization'] == 'Bearer test-only'
        if request.url.path == '/service/retrieve_all':
            return httpx.Response(200, json=[{'ID':'8','name':'Discord'}])
        if request.url.path == '/sms/stock':
            return httpx.Response(200, json={'stock': 4, 'price':'0.25'})
        if request.url.path == '/sms/check':
            return httpx.Response(200, json={'status':6})
        raise AssertionError(request.url.path)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as c:
        p = SMSPool(c)
        offers = await p.get_prices()
        assert [(x.service,x.country,x.cost,x.stock) for x in offers] == [('discord','us',Decimal('0.25'),4)]
        assert await p.cancel('testorder') == {'status':'CANCELLED'}


@pytest.mark.asyncio
async def test_smspool_uncertain_purchase_and_expiry(monkeypatch):
    monkeypatch.setattr(settings, 'smspool_api_key', 'test-only')
    monkeypatch.setattr(settings, 'allowed_services', 'discord')
    monkeypatch.setattr(settings, 'allowed_countries', 'us')
    calls = []
    def respond(request):
        calls.append(request.url.path)
        if request.url.path == '/purchase/sms':
            return httpx.Response(503)
        return httpx.Response(200, json={'status':2})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as c:
        p = SMSPool(c)
        with pytest.raises(AmbiguousPurchase):
            await p.buy_number('discord','us',Decimal('0.25'))
        assert calls == ['/purchase/sms']
        assert (await p.check('testorder'))['status'] == 'PENDING'
