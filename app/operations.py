"""Operational records; no secrets or raw payment payloads are stored here."""
import asyncio
import logging
import time
from datetime import timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from .config import settings
from .db import (Activation, AdminAudit, LedgerEntry, OpsNotice, Payment,
                 PaymentInbox, Setting, User, utcnow)

log = logging.getLogger(__name__)
LAGOS = ZoneInfo('Africa/Lagos')
STARTED_AT = time.monotonic()


async def insert_once(s, model, key, values):
    factory = pg_insert if s.bind.dialect.name == 'postgresql' else sqlite_insert
    stmt = factory(model).values(**values).on_conflict_do_nothing(index_elements=[key])
    await s.execute(stmt)


async def claim_purchase(session, uid, message_id):
    """One purchase attempt per Telegram confirmation card, even after restart."""
    async with session.begin() as s:
        factory = pg_insert if s.bind.dialect.name == 'postgresql' else sqlite_insert
        stmt = factory(Setting).values(key=f'purchase_claim:{uid}:{message_id}',
                                       value=utcnow().isoformat())
        result = await s.execute(stmt.on_conflict_do_nothing(index_elements=['key']).returning(Setting.key))
        return result.scalar_one_or_none() is not None


async def notice(s, key, recipient, body):
    await insert_once(s, OpsNotice, 'key', dict(key=key, recipient=recipient,
                     body=body, status='pending', attempts=0,
                     created_at=utcnow(), next_attempt_at=utcnow()))


def audit(s, actor, action, reference=''):
    s.add(AdminAudit(actor_id=actor, action=action[:40], reference=str(reference)[:100]))


async def accept_finished_ipn(session, payment_id, invoice_id, order_id):
    async with session.begin() as s:
        await insert_once(s, PaymentInbox, 'payment_id', dict(
            payment_id=payment_id, invoice_id=invoice_id, order_id=order_id,
            status='pending', attempts=0, created_at=utcnow(), next_attempt_at=utcnow()))


async def reconcile_inbox(session, shop):
    if not settings.payment_ready or settings.payment_provider != 'nowpayments':
        return
    async with session() as s:
        items = (await s.scalars(select(PaymentInbox).where(
            PaymentInbox.status == 'pending', PaymentInbox.next_attempt_at <= utcnow()
        ).order_by(PaymentInbox.next_attempt_at).limit(5))).all()
    for item in items:
        try:
            credited = await shop.verify_and_credit_nowpayments(
                item.invoice_id, item.payment_id, order_id=item.order_id)
            # A false result can mean already credited OR mismatch. Never guess.
            state = 'credited' if credited else 'pending'
            if not credited:
                async with session() as s:
                    query = select(Payment).where(Payment.gateway == 'nowpayments')
                    if item.order_id and item.order_id.startswith('friday-topup-'):
                        suffix = item.order_id.removeprefix('friday-topup-')
                        local = await s.scalar(query.where(Payment.id == int(suffix))) if suffix.isdigit() and len(suffix) <= 9 else None
                    elif item.invoice_id:
                        local = await s.scalar(query.where(Payment.gateway_invoice_id == item.invoice_id))
                    else:
                        local = None
                    if local and local.status == 'paid':
                        state = 'already_paid'
        except Exception:
            state = 'pending'
            log.warning('Payment inbox verification temporarily failed')
        async with session.begin() as s:
            row = await s.get(PaymentInbox, item.payment_id, with_for_update=True)
            if row is None or row.status != 'pending':
                continue
            row.attempts += 1
            if state == 'pending' and row.attempts >= 12:
                state = 'review'
                for admin in settings.admins:
                    await notice(s, f'ipnreview:{row.payment_id}:{admin}', admin,
                                 f'Payment {row.payment_id} requires manual reconciliation. No unverified credit was issued.')
            row.status = state
            row.next_attempt_at = utcnow() + timedelta(minutes=min(60, 2 ** min(row.attempts, 6)))


async def send_notices(session, shop):
    async with session() as s:
        items = (await s.scalars(select(OpsNotice).where(
            OpsNotice.status == 'pending', OpsNotice.next_attempt_at <= utcnow()
        ).order_by(OpsNotice.created_at).limit(20))).all()
    for item in items:
        try:
            await shop.notifier.send(item.recipient, item.body)
            status = 'sent'
        except Exception:
            status = 'failed' if item.attempts >= 4 else 'pending'
        async with session.begin() as s:
            row = await s.get(OpsNotice, item.key, with_for_update=True)
            if row and row.status == 'pending':
                row.attempts += 1
                row.status = status
                row.next_attempt_at = utcnow() + timedelta(minutes=5)
        await asyncio.sleep(.06)


async def report(session, start, end):
    async with session() as s:
        flows = dict((await s.execute(select(LedgerEntry.type, func.sum(LedgerEntry.amount))
                     .where(LedgerEntry.created_at >= start, LedgerEntry.created_at < end)
                     .group_by(LedgerEntry.type))).all())
        groups = (await s.execute(select(Activation.provider, func.count(),
                  func.sum(Activation.price_charged), func.sum(Activation.cost_provider))
                  .where(Activation.status == 'completed', Activation.completed_at >= start,
                         Activation.completed_at < end).group_by(Activation.provider))).all()
        new_users = await s.scalar(select(func.count()).select_from(User).where(
            User.created_at >= start, User.created_at < end))
        orders = await s.scalar(select(func.count()).select_from(Activation).where(
            Activation.created_at >= start, Activation.created_at < end))
        pending = await s.scalar(select(func.count()).select_from(Payment).where(Payment.status == 'created'))
    zero = Decimal('0')
    revenue = sum((Decimal(str(g[2])) for g in groups), zero)
    # TextVerified is USD. Historical other providers have no stored FX snapshot.
    known_cost = sum((Decimal(str(g[3])) for g in groups if g[0] == 'textverified'
                      and settings.wallet_currency == 'USD'), zero)
    unknown = any(g[0] != 'textverified' or settings.wallet_currency != 'USD' for g in groups)
    cur = settings.wallet_currency
    profit = ('Unavailable: historical provider FX not recorded' if unknown else
              f'{revenue-known_cost:.4f} {cur} (before gateway/hosting fees)')
    return (f'📊 FRIDAY business report\n{start.astimezone(LAGOS):%d %b %Y %H:%M} – '
            f'{end.astimezone(LAGOS):%d %b %Y %H:%M} WAT\n'
            f'New customers: {new_users}\nOrders started: {orders}\n'
            f'Completed: {sum(g[1] for g in groups)}\n'
            f'Deposits: {flows.get("topup", zero):.4f} {cur}\n'
            f'Purchase debits: {-flows.get("purchase", zero):.4f} {cur}\n'
            f'Refund credits: {flows.get("refund", zero):.4f} {cur}\n'
            f'Completed-order revenue: {revenue:.4f} {cur}\n'
            f'Estimated gross profit: {profit}\n'
            f'Pending invoices (all dates): {pending}')


async def schedule_daily_report(session):
    now = utcnow().astimezone(LAGOS)
    end = now.replace(hour=0, minute=0, second=0, microsecond=0)
    start = end - timedelta(days=1)
    day = start.date().isoformat()
    async with session() as s:
        marker = await s.get(Setting, 'ops_daily_report')
        if marker and marker.value == day:
            return
    body = await report(session, start.astimezone(timezone.utc), end.astimezone(timezone.utc))
    async with session.begin() as s:
        for admin in settings.admins:
            await notice(s, f'daily:{day}:{admin}', admin, body)
        row = await s.get(Setting, 'ops_daily_report', with_for_update=True)
        if row:
            row.value = day
        else:
            s.add(Setting(key='ops_daily_report', value=day))


async def risk_summary(session, *, flags_only=False):
    """Review flags only: no bans, wallet changes, or claims of fraud."""
    since = utcnow() - timedelta(hours=24)
    async with session() as s:
        high = (await s.execute(select(Activation.user_id, func.count())
                .where(Activation.created_at >= since)
                .group_by(Activation.user_id).having(func.count() >= 20)
                .limit(20))).all()
        mismatches = (await s.scalars(select(User.id).where(
            User.balance != select(func.coalesce(func.sum(LedgerEntry.amount), 0))
            .where(LedgerEntry.user_id == User.id).correlate(User).scalar_subquery()
        ).limit(20))).all()
        pending = await s.scalar(select(func.count()).select_from(PaymentInbox)
                                 .where(PaymentInbox.status == 'review'))
    if flags_only and not (high or mismatches or pending):
        return None
    return ('Risk review (flags only, not proof of fraud)\n'
            + ('High order volume: ' + ', '.join(f'{uid}: {n}' for uid, n in high)
               if high else 'No high-volume accounts in last 24h')
            + '\nLedger mismatches: ' + (', '.join(map(str, mismatches)) or 'none')
            + f'\nPayment inbox review: {pending}')


async def schedule_risk_alert(session):
    day = utcnow().astimezone(LAGOS).date().isoformat()
    body = await risk_summary(session, flags_only=True)
    if not body:
        return
    async with session.begin() as s:
        for admin in settings.admins:
            await notice(s, f'risk:{day}:{admin}', admin, body)
