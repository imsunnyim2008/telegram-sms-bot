"""Crypto Bot payment invoices. Never credit on user-supplied invoice status."""
import hashlib
import hmac
from decimal import Decimal

import httpx

from .config import settings


class PaymentError(Exception):
    pass


class CryptoBot:
    def __init__(self, client: httpx.AsyncClient | None = None):
        self.client = client or httpx.AsyncClient(timeout=20)

    async def _call(self, method: str, body: dict) -> dict:
        response = await self.client.post(
            f'{settings.payment_base_url}/{method}',
            headers={'Crypto-Pay-API-Token': settings.payment_api_key}, json=body,
        )
        response.raise_for_status()
        data = response.json()
        if not data.get('ok'):
            raise PaymentError('Payment gateway rejected request')
        return data['result']

    async def create_invoice(self, amount: Decimal, payment_id: str) -> dict:
        return await self._call('createInvoice', {
            'currency_type': 'fiat', 'fiat': settings.wallet_currency.upper(),
            'amount': str(amount), 'description': 'SMS bot wallet top-up',
            'payload': payment_id, 'expires_in': 3600,
        })

    async def get_invoice(self, invoice_id: str) -> dict | None:
        data = await self._call('getInvoices', {'invoice_ids': invoice_id, 'count': 1})
        if isinstance(data, dict):
            items = data.get('items', [])
        else:
            items = data if isinstance(data, list) else []
        return next((x for x in items if str(x.get('invoice_id')) == invoice_id), None)

    @staticmethod
    def verify_webhook(raw_body: bytes, signature: str) -> bool:
        if not settings.payment_api_key or not signature:
            return False
        key = hashlib.sha256(settings.payment_api_key.encode()).digest()
        expected = hmac.new(key, raw_body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, signature)

    async def aclose(self) -> None:
        await self.client.aclose()
