"""Isolated Paystack test-mode client. No production wallet mutations."""
import hashlib
import hmac
import re
import time
from urllib.parse import urlparse

import httpx

BASE = 'https://api.paystack.co'
REF = re.compile(r'^[a-zA-Z0-9.=-]{1,90}$')


class PaystackError(Exception):
    pass


class PaystackTest:
    def __init__(self, key: str, client=None):
        if not key.startswith('sk_test_'):
            raise PaystackError('A Paystack test secret key is required')
        self._key = key
        self.client = client or httpx.AsyncClient(timeout=15, follow_redirects=False)

    async def _request(self, method, path, **kwargs):
        try:
            response = await self.client.request(method, BASE + path,
                headers={'Authorization': 'Bearer ' + self._key, 'Accept': 'application/json'},
                **kwargs)
            response.raise_for_status()
            data = response.json()
            if data.get('status') is not True or not isinstance(data.get('data'), dict):
                raise ValueError()
            return data['data']
        except (httpx.HTTPError, ValueError, TypeError, AttributeError) as exc:
            raise PaystackError('Paystack request failed; do not credit or retry initialization blindly') from exc

    async def initialize(self, email: str, kobo: int, reference: str, *, channel: str | None = None):
        if channel not in (None, 'card', 'bank_transfer'):
            raise PaystackError('Unsupported payment method')
        if (not isinstance(kobo, int) or isinstance(kobo, bool) or not 50000 <= kobo <= 100000000
                or not REF.fullmatch(reference)
                or not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', email)
                or len(email) > 254):
            raise PaystackError('Invalid test checkout details')
        data = await self._request('POST', '/transaction/initialize', json={
            'email': email, 'amount': kobo, 'currency': 'NGN', 'reference': reference,
            'channels': [channel] if channel else ['card', 'bank_transfer'],
        })
        url = data.get('authorization_url', '')
        parsed = urlparse(url)
        if (data.get('reference') != reference or parsed.scheme != 'https'
                or parsed.netloc != 'checkout.paystack.com'):
            raise PaystackError('Unconfirmed checkout link')
        return url

    async def verify(self, reference: str, expected_kobo: int) -> bool:
        if not REF.fullmatch(reference):
            raise PaystackError('Invalid reference')
        data = await self._request('GET', '/transaction/verify/' + reference)
        return (data.get('domain') == 'test' and data.get('status') == 'success'
                and data.get('reference') == reference and data.get('currency') == 'NGN'
                and type(data.get('amount')) is int and data['amount'] == expected_kobo)

    async def aclose(self):
        await self.client.aclose()


def verify_signature(raw: bytes, legacy: str, modern: str, *, test_key: str,
                     signing_secret: str = '', now: int | None = None,
                     mode: str = 'test') -> bool:
    if len(raw) > 65536 or mode not in ('test', 'live') or not test_key.startswith(f'sk_{mode}_'):
        return False
    if modern:
        if not signing_secret.startswith('whsec_'):
            return False
        parts = [p.strip().split('=', 1) for p in modern.split(',') if '=' in p]
        timestamps = [v for k, v in parts if k == 't']
        if len(timestamps) != 1 or not re.fullmatch(r'[0-9]{1,12}', timestamps[0]):
            return False
        stamp = timestamps[0]
        if abs((int(time.time()) if now is None else now) - int(stamp)) > 300:
            return False
        expected = hmac.new(signing_secret.encode(), stamp.encode() + b'.' + raw,
                            hashlib.sha512).hexdigest()
        return any(re.fullmatch(r'whk_[a-zA-Z0-9_-]+:[0-9a-f]{128}', v)
                   and hmac.compare_digest(v.split(':', 1)[1], expected)
                   for k, v in parts if k == 'v1')
    if not re.fullmatch(r'[0-9a-f]{128}', legacy):
        return False
    expected = hmac.new(test_key.encode(), raw, hashlib.sha512).hexdigest()
    return hmac.compare_digest(expected, legacy)
