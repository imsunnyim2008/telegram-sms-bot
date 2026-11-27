import hashlib
import hmac

import httpx
import pytest
from app.paystack_test import PaystackTest, PaystackError, verify_signature


@pytest.mark.asyncio
async def test_checkout_and_verify():
    def respond(request):
        if request.method == 'POST':
            return httpx.Response(200, json={'status': True, 'data': {
                'reference': 'test-123', 'authorization_url': 'https://checkout.paystack.com/demo'}})
        return httpx.Response(200, json={'status': True, 'data': {
            'domain': 'test', 'status': 'success', 'amount': 50000,
            'currency': 'NGN', 'reference': 'test-123'}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        gateway = PaystackTest('sk_test_fake', client)
        assert await gateway.initialize('tester@example.com', 50000, 'test-123')
        assert await gateway.verify('test-123', 50000)
        assert not await gateway.verify('test-123', 60000)
        assert not await gateway.verify('wrong-reference', 50000)


def test_reject_live_key():
    with pytest.raises(PaystackError):
        PaystackTest('sk_live_fake')


def test_signatures():
    raw = b'{"event":"charge.success"}'
    key = 'sk_test_fake'
    sig = hmac.new(key.encode(), raw, hashlib.sha512).hexdigest()
    assert verify_signature(raw, sig, '', test_key=key)
    assert not verify_signature(raw+b' ', sig, '', test_key=key)
    secret = 'whsec_fake'
    v1 = hmac.new(secret.encode(), b'1000.'+raw, hashlib.sha512).hexdigest()
    modern = 't=1000,v1=whk_example:'+v1
    assert verify_signature(raw, '', modern, test_key=key, signing_secret=secret, now=1000)
    assert not verify_signature(raw, '', modern, test_key=key, signing_secret=secret, now=1400)
    assert not verify_signature(raw, sig, 'malformed', test_key=key)
