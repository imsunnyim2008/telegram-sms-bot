"""Paystack live deposits, separate from crypto and disabled by default."""
import re
import uuid
from decimal import Decimal, ROUND_DOWN

import httpx
from sqlalchemy import select

from .config import settings
from .db import Payment, User, Session, utcnow
from .ledger import apply
from .paystack_test import PaystackTest, PaystackError, REF


def ready():
    rate = settings.paystack_ngn_per_usd
    return (settings.paystack_enabled and settings.paystack_secret_key.startswith('sk_live_')
            and settings.wallet_currency == 'USD' and rate.is_finite()
            and rate > 0 and settings.public_base_url.startswith('https://'))


def eligible(uid):
    return ready() and (not settings.paystack_admin_only or uid in settings.admins)


def quote(raw):
    if not re.fullmatch(r'[0-9]{1,7}(?:\.[0-9]{1,2})?', raw):
        raise PaystackError('Enter a naira amount with up to two decimal places.')
    ngn = Decimal(raw)
    if not ready() or ngn < 500 or ngn > 1000000:
        raise PaystackError('Deposit unavailable or amount outside ₦500–₦1,000,000.')
    credit = (ngn / settings.paystack_ngn_per_usd).quantize(Decimal('.0001'), rounding=ROUND_DOWN)
    if credit <= 0 or credit > settings.max_topup:
        raise PaystackError('Amount outside the configured wallet limits.')
    return ngn, credit


class PaystackLive(PaystackTest):
    def __init__(self, key, client=None):
        if not key.startswith('sk_live_'):
            raise PaystackError('Live secret key required')
        self._key = key
        self.client = client or httpx.AsyncClient(timeout=15, follow_redirects=False)

    async def verify(self, reference, expected_kobo):
        if not REF.fullmatch(reference):
            raise PaystackError('Invalid reference')
        data = await self._request('GET', '/transaction/verify/' + reference)
        return (data.get('domain') == 'live' and data.get('status') == 'success'
                and data.get('reference') == reference and data.get('currency') == 'NGN'
                and type(data.get('amount')) is int and data['amount'] == expected_kobo)


async def create_deposit(uid, raw, email, *, gateway=None):
    if not eligible(uid):
        raise PaystackError('Paystack deposits are not enabled for this account.')
    if len(email) > 254 or not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', email):
        raise PaystackError('Enter your valid payment email.')
    ngn, credit = quote(raw)
    reference = 'friday-' + uuid.uuid4().hex
    async with Session.begin() as s:
        user = await s.get(User, uid, with_for_update=True)
        if not user or user.is_banned or not user.accepted_terms:
            raise PaystackError('Account unavailable.')
        row = Payment(user_id=uid, gateway='paystack', gateway_invoice_id=reference,
                      amount_fiat=ngn, amount_credit=credit, status='created')
        s.add(row)
        await s.flush()
        pid = row.id
    # Persist reference and exact quote first: an ambiguous initialization is
    # reconcilable. Do not create another invoice automatically.
    owned = gateway is None
    gateway = gateway or PaystackLive(settings.paystack_secret_key)
    try:
        url = await gateway.initialize(email, int(ngn * 100), reference)
    finally:
        if owned:
            await gateway.aclose()
    return pid, url, ngn, credit


async def settle(reference, *, gateway=None, notifier=None):
    # Existing payments may be reconciled even when new checkout is paused.
    if not settings.paystack_secret_key.startswith('sk_live_') or not re.fullmatch(r'friday-[0-9a-f]{32}', reference):
        return False
    async with Session() as s:
        row = await s.scalar(select(Payment).where(Payment.gateway == 'paystack',
                             Payment.gateway_invoice_id == reference))
        if not row or row.status != 'created':
            return False
        expected = int(row.amount_fiat * 100)
        pid = row.id
    owned = gateway is None
    gateway = gateway or PaystackLive(settings.paystack_secret_key)
    try:
        if not await gateway.verify(reference, expected):
            return False
    finally:
        if owned:
            await gateway.aclose()
    async with Session.begin() as s:
        row = await s.get(Payment, pid, with_for_update=True)
        if (not row or row.gateway != 'paystack' or row.gateway_invoice_id != reference
                or row.status != 'created' or row.amount_fiat * 100 != expected):
            return False
        await apply(s, row.user_id, row.amount_credit, 'topup', 'payment', str(pid))
        row.status = 'paid'
        row.paid_at = utcnow()
        uid, pid, ngn, credit = row.user_id, row.id, row.amount_fiat, row.amount_credit
    if notifier is not None:
        try:
            await notifier.send(uid, f'✅ Top up successful!\n\nDeposit #{pid}\nAmount paid: ₦{ngn:.2f}\nWallet credited: ${credit:.4f}\nPayment method: Paystack\nThank you for using FRIDAY!')
        except Exception:
            # Settlement is already committed. Never repeat wallet credit for a notification failure.
            import logging
            logging.getLogger(__name__).warning('Paystack deposit credited; notification unavailable')
    return True
