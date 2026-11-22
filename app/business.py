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
from .provider import AmbiguousPurchase, PriceItem, ProviderError, ProviderClient

log = logging.getLogger(__name__)
Q = Decimal('.0001')
INFLIGHT = ('ordering', 'pending', 'received', 'manual_review')
FINAL = ('cancelled', 'expired', 'failed', 'completed')


class BotError(Exception):
    pass


class Shop:
    def __init__(self, provider: ProviderClient, gateway: CryptoBot, notifier: Any):
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

    @staticmethod
    def price_from_settings(item: PriceItem, markup: Decimal,
                            custom: Decimal | None = None) -> Decimal:
        """Compute the identical amount used by Telegram and admin website.

        Custom prices may never fall below the live provider base cost.
        An old custom price that became loss-making rises to the new cost
        automatically, rather than making Friday lose money.
        """
        base = item.cost * settings.provider_price_to_wallet
        minimum = base.quantize(Q, rounding=ROUND_UP)
        if custom is not None:
            return max(minimum, custom.quantize(Q, rounding=ROUND_UP))
        return (base * (1 + markup / 100)).quantize(Q, rounding=ROUND_UP)

    @staticmethod
    def original_price(item: PriceItem) -> Decimal:
        """The provider's live price converted to the wallet currency, with no markup."""
        if settings.provider_price_to_wallet <= 0:
            raise BotError('Prices are not configured. Ask the admin to verify provider currency.')
        return (item.cost * settings.provider_price_to_wallet).quantize(Q, rounding=ROUND_UP)

    async def price(self, item: PriceItem, user_id: int | None = None) -> Decimal:
        # Public overrides and markup are for customers only. Administrators
        # see and pay the original provider cost, including at checkout.
        if user_id is not None and user_id in settings.admins:
            return self.original_price(item)
        self.original_price(item)  # validate currency conversion for all viewers
        async with Session() as s:
            markup = await s.get(Setting, 'markup_percent')
            override = await s.get(Setting, f'price:{item.service}:{item.country}')
        percentage = Decimal(markup.value) if markup else settings.markup_percent
        custom = Decimal(override.value) if override else None
        return self.price_from_settings(item, percentage, custom)

    async def quote(self, service: str, country: str,
                    user_id: int | None = None) -> tuple[PriceItem, Decimal]:
        if not settings.service_allowed(service) or not settings.country_allowed(country):
            raise BotError('This selection is not available')
        item = next((i for i in await self.provider.get_prices()
                     if i.service == service and i.country == country), None)
        if not item:
            raise BotError('No numbers in stock for this selection')
        return item, await self.price(item, user_id=user_id)

    async def buy(self, user_id: int, service: str, country: str) -> Activation:
        if not settings.live_ready:
            raise BotError('Live purchases are disabled until admin setup is complete')
        item, charge = await self.quote(service, country, user_id=user_id)
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
        if settings.payment_test_admin_only and user_id not in settings.admins:
            raise BotError('Payments are in admin-only testing. Public top-ups are not open yet.')
        # Validate BEFORE rounding. A deposit of $4.999 must never round up
        # to the minimum $5 and create a chargeable payment invoice.
        if not amount.is_finite() or amount < settings.min_topup:
            raise BotError(f'Minimum top-up is ${settings.min_topup}. Please enter ${settings.min_topup} or more.')
        if amount > settings.max_topup:
            raise BotError(f'Maximum top-up is ${settings.max_topup}.')
        if amount.as_tuple().exponent < -2:
            raise BotError('Enter an amount with at most 2 decimal places.')
        amount = amount.quantize(Decimal('.01'))
        async with Session.begin() as s:
            user = await s.get(User, user_id)
            if not user or user.is_banned or not user.accepted_terms:
                raise BotError('Account unavailable')
            p = Payment(user_id=user_id, gateway=settings.payment_provider,
                        gateway_invoice_id='creating:' + uuid.uuid4().hex,
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
        if settings.payment_provider != 'cryptobot':
            return False
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

    async def verify_and_credit_nowpayments(self, invoice_id: str | None,
                                            payment_id: str,
                                            *, order_id: str | None = None) -> bool:
        """Validate a NOWPayments hosted checkout and credit once.

        Hosted invoices can send invoice_id=null in their IPNs. Only a
        signed provider event can trigger this method; the order reference
        must still match both the local invoice and authenticated remote
        payment details. No wallet credit from an unverified callback.
        """
        if settings.payment_provider != 'nowpayments' or not settings.payment_ready:
            return False
        if not payment_id.isascii() or not payment_id.isdigit() or len(payment_id) > 40:
            return False
        if invoice_id is not None and (
            not invoice_id.isascii() or not invoice_id.isdigit() or len(invoice_id) > 40
        ):
            return False
        local_id = None
        prefix = 'friday-topup-'
        if order_id is not None:
            suffix = order_id.removeprefix(prefix)
            if (not order_id.startswith(prefix) or not suffix.isascii() or
                    not suffix.isdigit() or len(suffix) > 30):
                return False
            local_id = int(suffix)
        async with Session() as s:
            if local_id is not None:
                row = await s.get(Payment, local_id)
            elif invoice_id is not None:
                row = (await s.execute(select(Payment).where(
                    Payment.gateway == 'nowpayments',
                    Payment.gateway_invoice_id == invoice_id))).scalar_one_or_none()
            else:
                return False
            if not row or row.gateway != 'nowpayments' or row.status != 'created':
                return False
            if invoice_id is not None and row.gateway_invoice_id != invoice_id:
                return False
            expected_order = f'friday-topup-{row.id}'
            expected_invoice_id = row.gateway_invoice_id
            expected_amount = row.amount_fiat
            expected_currency = settings.wallet_currency.lower()
            local_id = row.id

        # Authenticated server-to-server read, independent of IPN claims.
        remote = await self.gateway.get_payment_status(payment_id)
        if str(remote.get('payment_id', '')) != payment_id:
            return False
        if str(remote.get('order_id', '')) != expected_order:
            return False
        remote_invoice = remote.get('invoice_id')
        # Older NOWPayments hosted invoice flows can return invoice_id=null.
        # Reject any *present* provider invoice ID that does not match ours.
        if remote_invoice is not None and str(remote_invoice) != expected_invoice_id:
            return False
        if remote.get('payment_status') != 'finished':
            return False
        if str(remote.get('price_currency', '')).lower() != expected_currency:
            return False
        try:
            price = Decimal(str(remote.get('price_amount', '')))
            paid = Decimal(str(remote.get('actually_paid', '')))
            if (not price.is_finite() or price != expected_amount or
                    not paid.is_finite() or paid <= 0):
                return False
        except (ValueError, TypeError, ArithmeticError):
            return False

        # PostgreSQL row locking and unique ledger refs make repeated events
        # idempotent even if multiple signed IPNs arrive simultaneously.
        async with Session.begin() as s:
            row = await s.get(Payment, local_id, with_for_update=True)
            if (not row or row.gateway != 'nowpayments' or row.status != 'created'
                    or row.gateway_invoice_id != expected_invoice_id
                    or row.amount_fiat != price):
                return False
            row.status = 'paid'
            row.paid_at = utcnow()
            await apply(s, row.user_id, row.amount_credit,
                        'topup', 'payment', str(row.id))
            user_id, amount = row.user_id, row.amount_credit
        try:
            await self.notifier.send(user_id,
                f'Payment confirmed! +{amount} {settings.wallet_currency} wallet credit.')
        except Exception:
            log.warning('Payment credited, but confirmation message could not be delivered')
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
