import httpx
import pytest

from app.textverified import TextVerifiedSms
from app.handlers import catalogue_matches


def test_search_includes_unpriced_catalogue_names():
    assert catalogue_matches(['applewallet', 'apple', 'instagram'], 'APPLE') == ['apple', 'applewallet']
    assert catalogue_matches(['apple', 'instagram'], 'missing') == []


@pytest.mark.asyncio
async def test_full_catalogue_is_cached_and_read_only(monkeypatch):
    calls = []
    def respond(request):
        calls.append((request.method, request.url.path))
        return httpx.Response(200, json=[
            {'serviceName':'Apple','capability':'sms'},
            {'serviceName':'Instagram','capability':'smsandvoicecombo'},
            {'serviceName':'VoiceOnly','capability':'voice'}])
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = TextVerifiedSms(client)
        async def token():
            return 'test-token'
        monkeypatch.setattr(provider, '_auth', token)
        assert await provider.catalogue_names() == ['apple', 'instagram']
        assert await provider.catalogue_names() == ['apple', 'instagram']
        assert calls == [('GET','/api/pub/v2/services')]
