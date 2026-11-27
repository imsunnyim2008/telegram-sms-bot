import httpx
import pytest

from app.textverified import TextVerifiedSms


@pytest.mark.asyncio
async def test_capabilities_are_read_only(monkeypatch):
    calls = []
    def respond(request):
        calls.append((request.method, request.url.path))
        kind = request.url.params.get('reservationType')
        if kind == 'renewable':
            return httpx.Response(403)
        return httpx.Response(200, json=[{'serviceName':'example','capability':'sms'},
                                         {'serviceName':'example','capability':'sms'}])
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = TextVerifiedSms(client)
        async def token():
            return 'test-token'
        monkeypatch.setattr(provider, '_auth', token)
        assert await provider.catalogue_capabilities() == {
            'verification':1, 'nonrenewable':1, 'renewable':None}
        assert calls == [('GET','/api/pub/v2/services')]*3


@pytest.mark.asyncio
async def test_refund_requires_exact_provider_id_and_state(monkeypatch):
    provider = TextVerifiedSms()
    async def details(oid):
        return {'id': 'different', 'state':'verificationRefunded'}
    monkeypatch.setattr(provider, '_details', details)
    assert not await provider.confirmed_refund('abc')
    async def confirmed(oid):
        return {'id':oid, 'state':'verificationRefunded'}
    monkeypatch.setattr(provider, '_details', confirmed)
    assert await provider.confirmed_refund('abc')
    await provider.aclose()
