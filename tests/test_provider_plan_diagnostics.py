from decimal import Decimal

import pytest

from app.textverified import TextVerifiedSms
from app.provider import ProviderError


@pytest.mark.asyncio
async def test_catalogue_capabilities_distinguish_unavailable(monkeypatch):
    client = TextVerifiedSms()
    async def request(method, path, *, params=None):
        assert method == 'GET'
        assert path == '/api/pub/v2/services'
        assert params['numberType'] == 'mobile'
        if params['reservationType'] == 'renewable':
            raise ProviderError('not available')
        return [{'serviceName': 'example', 'capability': 'sms'},
                {'serviceName': 'example', 'capability': 'smsAndVoiceCombo'},
                {'serviceName': 'voice', 'capability': 'voice'}]
    monkeypatch.setattr(client, '_request', request)
    try:
        assert await client.catalogue_capabilities() == {
            'verification': 1, 'nonrenewable': 1, 'renewable': None}
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_confirmed_refund_does_not_require_sms_history(monkeypatch):
    client = TextVerifiedSms()
    async def details(order_id):
        return {'id': order_id, 'state': 'verificationRefunded', 'totalCost': str(Decimal('1'))}
    async def no_history(*args, **kwargs):
        raise AssertionError('No SMS history required for confirmed refund')
    monkeypatch.setattr(client, '_details', details)
    monkeypatch.setattr(client, '_request', no_history)
    try:
        assert await client.check('test-order') == {'status': 'CANCELLED', 'sms': []}
    finally:
        await client.aclose()
