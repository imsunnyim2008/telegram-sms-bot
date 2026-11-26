"""Authenticated SMSPool webhook; only schedules existing owned orders for polling."""
import hashlib
import hmac
import json
import re
import time

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from .db import Activation, Session, TextVerifiedWebhookReceipt, utcnow


def valid_signature(body, header, secret, now=None):
    if not secret.startswith('whsec_') or len(header) > 256:
        return False
    match = re.fullmatch(r't=([0-9]{1,12}),v1=([a-f0-9]{64})', header)
    if not match:
        return False
    timestamp, signature = match.groups()
    if abs((time.time() if now is None else now) - int(timestamp)) > 300:
        return False
    expected = hmac.new(secret.encode(), timestamp.encode() + b'.' + body,
                        hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature, expected)


def parse_event(body):
    event = json.loads(body)
    if not isinstance(event, dict):
        raise ValueError('Invalid event')
    kind = event.get('event')
    if kind not in ('order.code', 'order.status', 'webhook.test'):
        return {'event': 'ignored'}
    eid = event.get('event_id')
    if not isinstance(eid, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,160}', eid):
        raise ValueError('Invalid event ID')
    if kind == 'webhook.test':
        return {'event': kind}
    order = event.get('order_code') or event.get('orderid')
    phone = event.get('phonenumber')
    if not isinstance(order, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,90}', order):
        raise ValueError('Invalid order reference')
    if not isinstance(phone, str) or not re.fullmatch(r'\+?[0-9]{8,20}', phone):
        raise ValueError('Invalid phone reference')
    return {'event': kind, 'order': order, 'phone': phone.lstrip('+'),
            'key': hashlib.sha256(('smspool:' + eid).encode()).hexdigest()}


async def wake_matching_order(event):
    if event['event'] not in ('order.code', 'order.status'):
        return False
    try:
        async with Session.begin() as s:
            rows = (await s.scalars(select(Activation).where(
                Activation.provider == 'smspool',
                Activation.provider_order_id == event['order'],
                Activation.status == 'pending',
            ).limit(2).with_for_update())).all()
            if len(rows) != 1 or str(rows[0].phone).lstrip('+') != event['phone']:
                return False
            # Reuse the existing bounded receipt store with a namespaced hash.
            # Never persist the message or code supplied by the webhook.
            s.add(TextVerifiedWebhookReceipt(key=event['key']))
            await s.flush()
            rows[0].next_poll_at = utcnow()
            return True
    except IntegrityError:
        return False
