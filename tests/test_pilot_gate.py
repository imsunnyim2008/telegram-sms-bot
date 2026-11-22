"""Real-money pilot is restricted to owners until provider webhooks are verified."""
from decimal import Decimal

import pytest

from app.business import BotError, Shop
from app.config import settings


@pytest.mark.asyncio
async def test_non_admin_cannot_create_live_payment_invoice(monkeypatch):
    monkeypatch.setattr(settings, 'payment_provider', 'nowpayments')
    monkeypatch.setattr(settings, 'payments_enabled', True)
    monkeypatch.setattr(settings, 'nowpayments_api_key', 'test-key')
    monkeypatch.setattr(settings, 'nowpayments_ipn_secret', 'test-ipn')
    monkeypatch.setattr(settings, 'public_base_url', 'https://bot.example')
    monkeypatch.setattr(settings, 'payment_test_admin_only', True)
    shop = Shop(None, None, None)
    with pytest.raises(BotError, match='admin-only testing'):
        await shop.start_payment(-5678943, Decimal('5'))


@pytest.mark.asyncio
async def test_disabled_payment_rejects_even_admin(monkeypatch):
    monkeypatch.setattr(settings, 'payments_enabled', False)
    monkeypatch.setattr(settings, 'payment_test_admin_only', True)
    shop = Shop(None, None, None)
    with pytest.raises(BotError, match='not enabled'):
        await shop.start_payment(-5678943, Decimal('5'))
