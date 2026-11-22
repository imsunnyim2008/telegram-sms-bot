"""Crash-resumable polling: every outstanding order is recovered from PostgreSQL."""
import asyncio
import logging
from datetime import timedelta, timezone
from html import escape

from sqlalchemy import select

from .config import settings
from .db import Activation, Payment, Session, Setting, utcnow
from .provider import ProviderError
from .service_ui import app_label_html

log = logging.getLogger(__name__)


def aware(dt):
    return dt.replace(tzinfo=timezone.utc) if dt and dt.tzinfo is None else dt


class Worker:
    def __init__(self, shop):
        self.shop = shop
        self._loops = 0

    async def run(self):
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception('Worker iteration failed')
            await asyncio.sleep(5)

    async def tick(self):
        self._loops += 1
        async with Session() as s:
            ids = (await s.scalars(select(Activation.id).where(
                Activation.status.in_(('pending', 'received', 'manual_review')),
                Activation.next_poll_at <= utcnow(),
            ).order_by(Activation.id).limit(30))).all()
        for aid in ids:
            try:
                await self.poll_order(aid)
            except Exception:
                log.exception('Polling failed for order %s', aid)
        if self._loops % 6 == 0:
            await self.poll_payments()
        if self._loops % 12 == 0:
            await self.fix_orphan_orders()
        if self._loops % 720 == 0:
            await self.cleanup()
            await self.low_balance_alert()

    async def poll_order(self, aid: int):
        async with Session() as s:
            row = await s.get(Activation, aid)
            if row is None:
                return
            status, oid, user_id, expires = row.status, row.provider_order_id, row.user_id, aware(row.expires_at)
            created, notified, code = aware(row.created_at), row.sms_notified, row.code
            service, provider_name = row.service, row.provider
            provider = self.shop.provider_for(provider_name)
        if status == 'manual_review':
            await self.shop.reconcile(aid)
            async with Session.begin() as s:
                row = await s.get(Activation, aid, with_for_update=True)
                if row and row.status == 'manual_review':
                    row.next_poll_at = utcnow() + timedelta(seconds=60)
            return
        if status == 'received':
            await self.deliver_and_finish(aid, user_id, oid, code, notified, service,
                                          provider_name)
            return
        if status != 'pending' or oid is None:
            return
        try:
            result = await provider.check(oid)
        except ProviderError:
            return
        sms = result.get('sms') or []
        received_code = next((str(msg.get('code')) for msg in sms
                              if isinstance(msg, dict) and msg.get('code')), None)
        provider_status = str(result.get('status', '')).upper()
        if received_code:
            async with Session.begin() as s:
                row = await s.get(Activation, aid, with_for_update=True)
                if row and row.status == 'pending':
                    row.code = received_code[:100]
                    row.status = 'received'
                    row.next_poll_at = utcnow()
            await self.deliver_and_finish(aid, user_id, oid, received_code, False,
                                          service, provider_name)
            return
        if provider_status in {'CANCELED', 'CANCELLED', 'EXPIRED', 'TIMEOUT', 'BANNED'}:
            outcome = 'expired' if provider_status in {'EXPIRED', 'TIMEOUT'} else 'cancelled'
            if await self.shop.refund(aid, outcome):
                await self.shop.notifier.send(user_id, f'Order #{aid} expired or was cancelled. Full refund issued.')
            return
        if utcnow() >= expires:
            try:
                res = await provider.cancel(oid)
                if str(res.get('status', '')).upper() in {'CANCELED', 'CANCELLED'}:
                    if await self.shop.refund(aid, 'expired'):
                        await self.shop.notifier.send(user_id, f'Order #{aid} expired. Full refund issued.')
            except ProviderError:
                # Never refund on an unconfirmed provider cancellation; retry next tick.
                pass
        else:
            interval = 10 if (utcnow() - created).total_seconds() > 120 else 5
            async with Session.begin() as s:
                row = await s.get(Activation, aid, with_for_update=True)
                if row and row.status == 'pending':
                    row.next_poll_at = utcnow() + timedelta(seconds=interval)

    async def deliver_and_finish(self, aid, user_id, oid, code, notified,
                                 service='', provider_name=None):
        if not notified:
            try:
                await self.shop.notifier.send(
                    user_id,
                    f'🔐 Your verification code for order #{aid}: {escape(str(code))}\n'
                    f'App: {app_label_html(service)}',
                    parse_mode='HTML',
                )
            except Exception:
                # Keep received state so we can retry notification after a restart.
                return
            async with Session.begin() as s:
                row = await s.get(Activation, aid, with_for_update=True)
                if row and row.status == 'received':
                    row.sms_notified = True
        try:
            provider = self.shop.provider_for(provider_name or settings.provider_name)
            response = await provider.finish(oid)
            if str(response.get('status', '')).upper() not in {'FINISHED', 'RECEIVED'}:
                return
        except ProviderError:
            return
        async with Session.begin() as s:
            row = await s.get(Activation, aid, with_for_update=True)
            if row and row.status == 'received':
                row.status = 'completed'
                row.completed_at = utcnow()

    async def fix_orphan_orders(self):
        # If process stopped after charging but before storing provider response, do not
        # silently refund: reconcile against provider order history first.
        async with Session.begin() as s:
            rows = (await s.scalars(select(Activation).where(
                Activation.status == 'ordering',
                Activation.created_at < utcnow() - timedelta(seconds=120),
            ).limit(30).with_for_update())).all()
            for row in rows:
                row.status = 'manual_review'
                row.next_poll_at = utcnow()

    async def poll_payments(self):
        # Hosted NOWPayments invoices generate payment IDs only when the payer
        # begins checkout. Trust only signed IPN + authenticated payment lookup.
        # Do not poll the invoice ID through the Crypto Bot status API.
        if not settings.payment_ready or settings.payment_provider != 'cryptobot':
            return
        async with Session() as s:
            invoice_ids = (await s.scalars(select(Payment.gateway_invoice_id).where(
                Payment.status == 'created').order_by(Payment.created_at).limit(20))).all()
        for invoice_id in invoice_ids:
            try:
                await self.shop.verify_and_credit(invoice_id)
            except Exception:
                log.warning('Payment status check failed for a pending invoice')

    async def cleanup(self):
        cutoff = utcnow() - timedelta(days=30)
        async with Session.begin() as s:
            rows = (await s.scalars(select(Activation).where(
                Activation.created_at < cutoff,
                Activation.code.is_not(None)).limit(1000))).all()
            for row in rows:
                row.code = None
                row.phone = None

    async def low_balance_alert(self):
        if not settings.provider_credentials_ready:
            return
        try:
            balance = await self.shop.provider.get_balance()
        except Exception:
            return
        if balance >= settings.provider_low_balance_alert:
            return
        today = utcnow().date().isoformat()
        async with Session.begin() as s:
            entry = await s.get(Setting, 'low_balance_alert_day', with_for_update=True)
            if entry and entry.value == today:
                return
            if entry:
                entry.value = today
            else:
                s.add(Setting(key='low_balance_alert_day', value=today))
        await self.shop.alert_admins(f'Low provider balance; top up your {settings.provider_name} wallet.')
