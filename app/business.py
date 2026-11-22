"""Business logic independent from Telegram handlers."""
import logging
import uuid
from datetime import timedelta, timezone
from decimal import Decimal, ROUND_UP
from typing import Any

from sqlalchemy import func, select

from .config import settings
from .db import Activation, Payment, Session, Setting, User, utcnow
from .gateway import CryptoBot
from .ledger import InsufficientFunds, apply
from .provider import AmbiguousPurchase, FiveSim, PriceItem, ProviderError

log = logging.getLogger(__name__)
Q = Decimal('.0001')
INFLIGHT = ('ordering', 'pending', 'received', 'manual_review')
FINAL = ('cancelled', 'expired', 'failed', 'completed')


class BotError(Exception):
    pass


class Shop:
    def __init__(self, provider: FiveSim, gateway: CryptoBot, notifier: Any):
        self.provider = provider
        self.gateway = gateway
        self.notifier = notifier

    async def ensure_user(self, user_id: int, username: str = '') -> User:
        async with Session.begin() as s:
            user = await s.get(User, user_id)
            if user is None:
                user = User(id=user_id, username=username)
                s.add(user)
            else:
                user.username = username
                user.last_seen_at = utcnow()
            await s.flush()
            return user

    async def get_user(self, user_id: int) -> User | None:
        async with Session() as s:
            return await s.get(User, user_id)

    async def accept_terms(self, user_id: int) -> None:
        async with Session.begin() as s:
            user = await s.get(User, user_id, with_for_update=True)
            if user is not None and not user.is_banned:
                user.accepted_terms = True

    async def _markup(self) -> Decimal:
        async with Session() as s:
            row = await s.get(Setting, 'markup_percent')
        return Decimal(row.value) if row else settings.markup_percent

    async def price(self, item: PriceItem) -> Decimal:
        if settings.provider_price_to_wallet <= 0:
            raise BotError('Prices are not configured. Ask the admin to verify provider currency.')
        markup = await self._markup()
        return (item.cost * settings.provider_price_to_wallet * (1 + markup / 100)).quantize(Q, rounding=ROUND_UP)

    async def quote(self, service: str, country: str) -> tuple[PriceItem, Decimal]:
        if not settings.service_allowed(service) or not settings.country_allowed(country):
            raise BotError('This selection is not available')
        item = next((i for i in await self.provider.get_prices()
                     if i.service == service and i.country == country), None)
        if not item:
            raise BotError('No numbers in stock for this selection')
        return item, await self.price(item)

    async def buy(self, user_id: int, service: str, country: str) -> Activation:
        if not settings.live_ready:
            raise BotError('Live purchases are disabled until admin setup is complete')
        item, charge = await self.quote(service, country)
        if await self.provider.get_balance() < item.cost:
            raise BotError('Provider balance is insufficient. Please try again later.')
        async with Session.begin() as s:
            user = (await s.execute(select(User).where(User.id == user_id).with_for_update())).scalar_one()
            if user.is_banned or not user.accepted_terms:
                raise BotError('Account is not enabled')
            active = await s.scalar(select(func.count()).select_from(Activation).where(
                Activation.user_id == user_id, Activation.status.in_(INFLIGHT)))
            if active >= 10:
                raise BotError('Maximum of 10 active orders')
            latest = await s.scalar(select(func.max(Activation.created_at)).where(Activation.user_id == user_id))
            if latest and (utcnow().replace(tzinfo=None) - latest.replace(tzinfo=None)).total_seconds() < 3:
                raise BotError('Please wait 3 seconds between purchases')
            activation = Activation(
                user_id=user_id, service=service, country=country,
                provider=settings.provider_name,
                cost_provider=item.cost, price_charged=charge,
                status='ordering', expires_at=utcnow() + timedelta(seconds=settings.activation_timeout_seconds),
                next_poll_at=utcnow() + timedelta(seconds=10),
            )
            s.add(activation)
            await s.flush()
            try:
                await apply(s, user_id, -charge, 'purchase', 'activation', str(activation.id))
            except InsufficientFunds as exc:
                raise BotError(str(exc)) from exc
            aid = activation.id

        # Do not retry buying automatically: it might allocate a second paid number.
        try:
            order = await self.provider.buy_number(service, country, item.cost)
        except AmbiguousPurchase:
            async with Session.begin() as s:
                row = await s.get(Activation, aid, with_for_update=True)
                row.status = 'manual_review'
            await self.alert_admins('Order ' + str(aid) + ' needs provider reconciliation; charge held.')
            raise BotError(f'Purchase #{aid} has an uncertain provider result. Funds are reserved; support is reviewing it.')
        except ProviderError:
            async with Session.begin() as s:
                row = await s.get(Activation, aid, with_for_update=True)
                if row.status == 'ordering':
                    row.status = 'failed'
                    await apply(s, user_id, charge, 'refund', 'activation', str(aid))
            raise BotError('Provider could not allocate a number. Your balance was refunded.')
        async with Session.begin() as s:
            row = await s.get(Activation, aid, with_for_update=True)
            row.provider_order_id = str(order['id'])
            row.phone = str(order['phone'])
            row.cost_provider = Decimal(str(order.get('price', item.cost)))
            row.status = 'pending'
            row.next_poll_at = utcnow()
            row.expires_at = min(row.expires_at, parse_provider_date(order.get('expires'), row.expires_at))
            await s.flush()
            return row

    async def refund(self, activation_id: int, outcome: str) -> bool:
        if outcome not in {'cancelled', 'expired', 'failed'}:
            raise ValueError('Invalid refund outcome')
        async with Session.begin() as s:
            row = await s.get(Activation, activation_id, with_for_update=True)
            if not row or row.status not in {'pending', 'ordering', 'manual_review'}:
                return False
            row.status = outcome
            return await apply(s, row.user_id, row.price_charged, 'refund', 'activation', str(row.id))

    async def cancel(self, user_id: int, activation_id: int) -> None:
        async with Session() as s:
            row = await s.get(Activation, activation_id)
            if not row or row.user_id != user_id:
                raise BotError('Order not found')
            if row.status != 'pending' or not row.provider_order_id:
                raise BotError('Only pending orders can be cancelled')
            created = row.created_at.replace(tzinfo=None)
            if (utcnow().replace(tzinfo=None) - created).total_seconds() < 30:
                raise BotError('Please wait 30 seconds before cancelling')
            oid = row.provider_order_id
        try:
            result = await self.provider.cancel(oid)
            if str(result.get('status', '')).upper() not in {'CANCELED', 'CANCELLED'}:
                raise BotError('Provider did not confirm cancellation; balance unchanged')
        except ProviderError as exc:
            raise BotError('Provider cancellation was not confirmed; balance unchanged') from exc
        if await self.refund(activation_id, 'cancelled'):
            await self.notifier.send(user_id, f'Order #{activation_id} cancelled. Refunded in full.')

    async def reconcile(self, activation_id: int) -> None:
        """Try to recover ambiguous 5SIM orders; NEVER buy a replacement blindly."""
        async with Session() as s:
            row = await s.get(Activation, activation_id)
            if not row or row.status != 'manual_review':
                return
            service, country, created = row.service, row.country, row.created_at
            expiry = row.expires_at.replace(tzinfo=timezone.utc) if row.expires_at.tzinfo is None else row.expires_at
            linked = set((await s.scalars(select(Activation.provider_order_id).where(
                Activation.provider_order_id.is_not(None)))).all())
        if not getattr(self.provider, 'supports_complete_order_history', True):
            # Active-only history cannot prove that a chargeable order was not made.
            # Hold for operator review instead of risking an unverified refund.
            return
        try:
            orders = await self.provider.recent_orders()
        except Exception:
            return
        candidates = []
        for order in orders:
            if order.get('product') != service or order.get('country') != country:
                continue
            if str(order.get('id')) in linked:
                continue
            date = parse_provider_date(order.get('created_at'), None)
            if date and abs((date - created.replace(tzinfo=timezone_utc())).total_seconds()) <= 120:
                candidates.append(order)
        # Same service/country/time is NOT cryptographic proof an order belongs
        # to this user: never forward an unverified third party's SMS.
        if candidates:
            return  # admin must investigate possible matches manually
        if not candidates and utcnow() > expiry + timedelta(minutes=3):
            # Provider history was reachable and reports no candidate. Free held credits.
            if await self.refund(activation_id, 'failed'):
                await self.alert_admins(f'Order #{activation_id} was refunded after no matching 5SIM order was found.')

    async def start_payment(self, user_id: int, amount: Decimal) -> dict:
        if not settings.payment_ready:
            raise BotError('Online top-ups are not enabled yet')
        amount = amount.quantize(Decimal('.01'))
        if not settings.min_topup <= amount <= settings.max_topup:
            raise BotError(f'Top-up must be {settings.min_topup} to {settings.max_topup} {settings.wallet_currency}')
        async with Session.begin() as s:
            user = await s.get(User, user_id)
            if not user or user.is_banned or not user.accepted_terms:
                raise BotError('Account unavailable')
            p = Payment(user_id=user_id, gateway_invoice_id='creating:' + uuid.uuid4().hex,
                        amount_fiat=amount, amount_credit=amount, status='creating')
            s.add(p)
            await s.flush()
            pid = p.id
        try:
            invoice = await self.gateway.create_invoice(amount, str(pid))
            invoice_id = str(invoice['invoice_id'])
            url = invoice.get('bot_invoice_url') or invoice.get('pay_url') or invoice.get('mini_app_invoice_url')
            if not url:
                raise BotError('Payment gateway did not provide payment link')
        except Exception as exc:
            async with Session.begin() as s:
                p = await s.get(Payment, pid, with_for_update=True)
                p.status = 'expired'
            raise BotError('Could not create an invoice. Please try again.') from exc
        async with Session.begin() as s:
            p = await s.get(Payment, pid, with_for_update=True)
            p.gateway_invoice_id = invoice_id
            p.status = 'created'
        return {'invoice_id': invoice_id, 'url': url, 'amount': amount}

    async def verify_and_credit(self, invoice_id: str) -> bool:
        # Authoritative gateway read, not a client-supplied "paid" flag.
        invoice = await self.gateway.get_invoice(invoice_id)
        if not invoice:
            return False
        if invoice.get('status') != 'paid':
            if invoice.get('status') == 'expired':
                async with Session.begin() as s:
                    row = (await s.execute(select(Payment).where(
                        Payment.gateway_invoice_id == invoice_id).with_for_update())).scalar_one_or_none()
                    if row and row.status != 'paid':
                        row.status = 'expired'
            return False
        async with Session.begin() as s:
            row = (await s.execute(select(Payment).where(
                Payment.gateway_invoice_id == invoice_id).with_for_update())).scalar_one_or_none()
            if row is None or row.status != 'created':
                return False
            if (invoice.get('payload') != str(row.id)
                    or Decimal(str(invoice.get('amount'))) != row.amount_fiat
                    or invoice.get('fiat', '').upper() != settings.wallet_currency.upper()
                    or invoice.get('currency_type') != 'fiat'):
                log.warning('Gateway invoice verification mismatch: invoice ID %s', invoice_id)
                return False
            row.status = 'paid'
            row.paid_at = utcnow()
            await apply(s, row.user_id, row.amount_credit, 'topup', 'payment', str(row.id))
            uid, amount = row.user_id, row.amount_credit
        await self.notifier.send(uid, f'Payment confirmed! +{amount} {settings.wallet_currency} wallet credit.')
        return True

    async def alert_admins(self, message: str) -> None:
        for admin in settings.admins:
            try:
                await self.notifier.send(admin, '⚠️ ' + message)
            except Exception:
                log.warning('Could not send admin alert')


def timezone_utc():
    return timezone.utc


def parse_provider_date(value: str | None, fallback):
    from datetime import datetime, timezone
    if not value:
        return fallback
    try:
        out = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return out if out.tzinfo else out.replace(tzinfo=timezone.utc)
    except ValueError:
        return fallback
