"""Crash-resumable polling: every outstanding order is recovered from PostgreSQL."""
import asyncio
import logging
import time
from datetime import timedelta, timezone
from html import escape

from sqlalchemy import delete, func, or_, select

from .auto_maintenance import mark_notice_sent, observe, pending_notices
from .config import settings
from .db import Activation, MaintenanceNotice, OpsNotice, Payment, Session, Setting, User, utcnow
from .provider import ProviderError
from .growth_controls import stock_notices
from .operations import reconcile_inbox, schedule_daily_report, schedule_risk_alert, send_notices
from .service_ui import app_label_html
from .textverified_webhooks import prune_old_receipts

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
        await self.deliver_maintenance_notices()
        if self._loops % 6 == 0:
            await self.poll_payments()
        if self._loops % 12 == 0:
            await self.fix_orphan_orders()
        if self._loops % 720 == 0:
            await self.cleanup()


    async def deliver_maintenance_notices(self):
        # Only the single production worker drains the queue. Never send codes,
        # balances or phone numbers in a broadcast.
        async with Session() as s:
            rows = (await s.scalars(select(MaintenanceNotice).where(
                MaintenanceNotice.status == 'pending',
                MaintenanceNotice.next_attempt_at <= utcnow(),
            ).order_by(MaintenanceNotice.id).limit(20))).all()
        for notice in rows:
            async with Session() as s:
                user = await s.get(User, notice.user_id)
                newer = await s.scalar(select(MaintenanceNotice.id).where(
                    MaintenanceNotice.user_id == notice.user_id,
                    MaintenanceNotice.id > notice.id).limit(1))
            if newer or not user or user.is_banned or not user.accepted_terms:
                status = 'skipped'
            else:
                text = ('🛠 FRIDAY is undergoing maintenance. New purchases are temporarily paused. '
                        'Existing orders and refunds remain available.'
                        if notice.state == 'on' else
                        '🟢 FRIDAY is back online. Purchase maintenance has ended. '
                        'New purchases are available subject to provider stock and availability.')
                try:
                    await self.shop.notifier.send(notice.user_id, text)
                    status = 'sent'
                except Exception:
                    status = 'failed' if notice.attempts >= 2 else 'pending'
            async with Session.begin() as s:
                row = await s.get(MaintenanceNotice, notice.id, with_for_update=True)
                if row and row.status == 'pending':
                    row.status = status
                    row.attempts += 1
                    row.next_attempt_at = utcnow() + timedelta(minutes=1)
            await asyncio.sleep(.06)

    async def run_operations(self):
        last_periodic = -1000.0
        while True:
            try:
                await send_notices(Session, self.shop)
                if time.monotonic() - last_periodic >= 60:
                    last_periodic = time.monotonic()
                    await asyncio.gather(reconcile_inbox(Session, self.shop),
                                         schedule_daily_report(Session),
                                         schedule_risk_alert(Session),
                                         self.low_balance_alert(),
                                         stock_notices(Session, self.shop))
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception('Operations worker iteration failed')
            await asyncio.sleep(5)

    async def run_health_monitor(self):
        # Independent of SMS polling. Start a fresh check every minute while online.
        while True:
            started = time.monotonic()
            try:
                await self.check_api_health()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception('API health monitor iteration failed')
            await asyncio.sleep(max(0, 60 - (time.monotonic() - started)))

    async def textverified_health(self):
        async def probe(operation):
            try:
                await asyncio.wait_for(operation(), timeout=45)
                return True
            except Exception:
                return False

        balance, catalogue = await asyncio.gather(
            probe(self.shop.provider.get_balance),
            probe(lambda: self.shop.provider.get_prices(fresh=True)),
        )
        await observe(Session, balance and catalogue, utcnow().timestamp())
        for notice in await pending_notices(Session):
            event = notice['event']
            incident = notice.get('incident')
            messages = {
                'outage': (f'🚨 FRIDAY: {settings.provider_name} outage detected.\n'
                           f'Incident: {incident}\n'
                           'Acknowledge this alert or reply directly to it. If the API is still down '
                           'after 30 minutes without acknowledgment, new purchases will automatically pause.'),
                'paused': '🛠 FRIDAY automatically enabled purchase maintenance after 30 minutes without acknowledgment. Customers will be notified.',
                'recovering': f'🟡 {settings.provider_name} is responding again. Friday will resume automatically after 10 continuous minutes of successful checks, unless you take manual control.',
                'recovered': f'🟢 {settings.provider_name} API is responding again.',
                'resumed': f'🟢 FRIDAY automatically disabled purchase maintenance after 10 continuous minutes of healthy {settings.provider_name} checks. Customers will be notified.',
            }
            for admin in settings.admins:
                if admin in notice.get('sent', []):
                    continue
                try:
                    if event == 'outage' and hasattr(self.shop.notifier, 'send_api_alert'):
                        await self.shop.notifier.send_api_alert(admin, messages[event], incident)
                    else:
                        await self.shop.notifier.send(admin, messages[event])
                except Exception:
                    log.warning('TextVerified alert delivery failed')
                    continue
                await mark_notice_sent(Session, notice['id'], admin, settings.admins)

    async def check_api_health(self):
        async def probe(label, operation):
            try:
                await asyncio.wait_for(operation(), timeout=25)
                healthy = True
            except Exception:
                healthy = False
            if label in ('NOWPayments', 'CryptoBot'):
                async with Session.begin() as s:
                    row = await s.get(Setting, 'ops_gateway_health', with_for_update=True)
                    value = f'{label}: {"healthy" if healthy else "unavailable"} • {utcnow():%Y-%m-%d %H:%M:%S} UTC'
                    if row:
                        row.value = value
                    else:
                        s.add(Setting(key='ops_gateway_health', value=value))
            for admin in settings.admins:
                key = f'api_health:{label}:{admin}'
                now = utcnow()
                async with Session() as s:
                    row = await s.get(Setting, key)
                    previous = row.value if row else ''
                if healthy:
                    if not previous.startswith('down:'):
                        continue
                    message = f'🟢 FRIDAY: {label} API is responding again.'
                    value = 'up'
                else:
                    if previous.startswith('down:'):
                        try:
                            if now.timestamp() - float(previous[5:]) < 1800:
                                continue
                        except ValueError:
                            pass
                    message = (f'🚨 FRIDAY: {label} API health check failed. '
                               'Check provider dashboard and Render logs. No funds moved.')
                    value = f'down:{now.timestamp()}'
                try:
                    await self.shop.notifier.send(admin, message)
                except Exception:
                    log.warning('API health alert delivery failed for %s', label)
                    continue
                async with Session.begin() as s:
                    row = await s.get(Setting, key, with_for_update=True)
                    if row:
                        row.value = value
                    else:
                        s.add(Setting(key=key, value=value))

        jobs = []
        if settings.provider_credentials_ready:
            if settings.provider_name in ('textverified', 'smspool'):
                jobs.append(self.textverified_health())
            else:
                jobs.extend([
                    probe(settings.provider_name + ' balance', self.shop.provider.get_balance),
                    probe(settings.provider_name + ' catalogue', self.shop.provider.get_prices),
                ])
        if settings.payment_ready:
            if settings.payment_provider == 'nowpayments':
                jobs.append(probe('NOWPayments', self.shop.gateway.check_credentials))
            else:
                jobs.append(probe('CryptoBot', lambda: self.shop.gateway._call('getMe', {})))
        await asyncio.gather(*jobs)

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
            await self.shop.refund(aid, outcome)
            return
        if utcnow() >= expires:
            try:
                res = await provider.cancel(oid)
                if str(res.get('status', '')).upper() in {'CANCELED', 'CANCELLED'}:
                    await self.shop.refund(aid, 'expired')
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
                    f'App: {app_label_html(service)}\n'
                    f'After using the code, confirm with /done {aid}.',
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
        cutoff = utcnow() - timedelta(days=1)
        async with Session.begin() as s:
            rows = (await s.scalars(select(Activation).where(
                func.coalesce(Activation.completed_at, Activation.created_at) < cutoff,
                Activation.status.in_(('completed', 'cancelled', 'expired', 'failed')),
                or_(Activation.code.is_not(None), Activation.phone.is_not(None))).limit(1000))).all()
            for row in rows:
                row.code = None
                row.phone = None
        async with Session.begin() as s:
            await s.execute(delete(OpsNotice).where(OpsNotice.status.in_(('sent', 'failed')),
                            OpsNotice.created_at < utcnow() - timedelta(days=30)))
            await s.execute(delete(MaintenanceNotice).where(
                MaintenanceNotice.status != 'pending',
                MaintenanceNotice.created_at < utcnow() - timedelta(days=7)))
        await prune_old_receipts()

    async def low_balance_alert(self):
        if not settings.provider_credentials_ready:
            return
        try:
            balance = await self.shop.provider.get_balance()
        except Exception:
            return
        async with Session() as s:
            row = await s.get(Setting, 'provider_low_balance_threshold')
        from decimal import Decimal
        threshold = Decimal(row.value) if row else settings.provider_low_balance_alert
        if balance >= threshold:
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
        await self.shop.alert_admins(f'Low {settings.provider_name} balance: {balance} raw units (alert threshold {threshold}).')
