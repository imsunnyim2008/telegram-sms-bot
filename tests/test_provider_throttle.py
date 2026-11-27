import httpx
import pytest
from app.textverified import TextVerifiedSms
from app.provider import ProviderError
from app.config import settings


@pytest.mark.asyncio
async def test_rate_limit_honors_retry_after_without_more_requests(monkeypatch):
    monkeypatch.setattr(settings, 'allowed_services', 'example')
    monkeypatch.setattr(settings, 'blocked_services', '')
    monkeypatch.setattr(settings, 'allowed_countries', '')
    calls = []
    def respond(request):
        calls.append(request.url.path)
        if request.url.path.endswith('/services'):
            return httpx.Response(200, json=[{'serviceName':'example','capability':'sms'}])
        return httpx.Response(429, headers={'Retry-After':'120'})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        p = TextVerifiedSms(client)
        async def auth():
            return 'test-token'
        monkeypatch.setattr(p, '_auth', auth)
        for _ in range(2):
            with pytest.raises(ProviderError, match='429'):
                await p.service_prices('example')
        assert calls == ['/api/pub/v2/services','/api/pub/v2/inventory/verifications']
