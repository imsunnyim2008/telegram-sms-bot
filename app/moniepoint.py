"""Moniepoint POS webhook pilot. It never treats a transfer event as proof of an incoming deposit."""
import base64
import hashlib
import hmac
import json
import os
import re
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from .config import settings
from .db import BankEventMatch, MoniepointEvent, utcnow
from .operations import notice

TRANSFER_EVENTS = {'V1_TRANSFER_TRANSACTION', 'V1_POS_TRANSFER_TRANSACTION'}
WATCH_EVENTS = TRANSFER_EVENTS | {'V1_POS_COLLECTION_TRANSACTION', 'V1_POS_WITHDRAWAL_TRANSACTION'}


def webhook_secret():
    return os.environ.get('MONIEPOINT_WEBHOOK_SECRET', '').strip()


def valid_signature(body, webhook_id, timestamp, signature, secret):
    if not secret or not re.fullmatch(r'[A-Za-z0-9-]{1,100}', webhook_id or ''):
        return False
    if not re.fullmatch(r'[0-9]{13}', timestamp or '') or len(signature or '') != 44:
        return False
    data = webhook_id.encode() + b'__' + timestamp.encode() + b'__' + body
    expected = base64.b64encode(hmac.new(secret.encode(), data, hashlib.sha256).digest()).decode()
    return hmac.compare_digest(expected, signature)


def identity_key(value):
    normalized = ' '.join(value.casefold().split())
    return hmac.new(webhook_secret().encode(), normalized.encode(), hashlib.sha256).hexdigest()


def parse_transfer(body):
    event = json.loads(body)
    if not isinstance(event, dict):
        raise ValueError('Invalid event')
    kind = event.get('eventType')
    if not isinstance(kind, str) or kind not in WATCH_EVENTS:
        return None
    data = event.get('data')
    if not isinstance(data, dict):
        raise ValueError('Invalid transaction data')
    ref = data.get('transactionReference')
    amount = data.get('amount')
    if not isinstance(ref, str) or not 1 <= len(ref) <= 200:
        raise ValueError('Missing transaction reference')
    if type(amount) is not int or not 0 < amount <= 10**13:
        raise ValueError('Invalid amount')
    status = data.get('transactionStatus', '')
    code = data.get('responseCode', '')
    if not isinstance(status, str) or not re.fullmatch(r'[A-Za-z0-9_ -]{1,24}', status):
        raise ValueError('Invalid status')
    if not isinstance(code, str) or not re.fullmatch(r'[A-Za-z0-9_-]{0,12}', code):
        raise ValueError('Invalid response code')
    meta = data.get('metaData')
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except ValueError:
            meta = {}
    if not isinstance(meta, dict):
        meta = {}
    sender = meta.get('customerName')
    rrn = data.get('retrievalReferenceNumber')
    return {
        'sender_key': identity_key(sender) if isinstance(sender, str) and sender.strip() and webhook_secret() else None,
        'rrn_key': identity_key(rrn) if isinstance(rrn, str) and rrn.strip() and webhook_secret() else None,
        'event_type': kind, 'transaction_key': hashlib.sha256(ref.encode()).hexdigest(),
        'amount_kobo': amount, 'transaction_status': status,
        'response_code': code, 'has_merchant_reference': bool(data.get('merchantReference')),
        'has_narration': bool(data.get('narration')),
    }


async def record_transfer(session, webhook_id, data):
    # Permanent hashed event key prevents repeated delivery; the payload itself
    # may contain bank account numbers or names and must not be stored.
    key = hashlib.sha256(webhook_id.encode()).hexdigest()
    data = dict(data)
    match = {k: data.pop(k, None) for k in ('sender_key', 'rrn_key')}
    async with session.begin() as s:
        factory = pg_insert if s.bind.dialect.name == 'postgresql' else sqlite_insert
        stmt = factory(MoniepointEvent).values(event_key=key, received_at=utcnow(), **data)
        result = await s.execute(stmt.on_conflict_do_nothing(index_elements=['event_key'])
                                 .returning(MoniepointEvent.event_key))
        if result.scalar_one_or_none() is None:
            return False
        await s.execute(factory(BankEventMatch).values(event_key=key, **match).on_conflict_do_nothing(index_elements=['event_key']))
        amount = Decimal(data['amount_kobo']) / 100
        body = (f'🏦 Moniepoint pilot event received\nType: {data["event_type"]}\n'
                f'Amount: NGN {amount:.2f}\nStatus: {data["transaction_status"]}\n'
                f'Response: {data["response_code"] or "none"}\n'
                f'Merchant reference present: {"yes" if data["has_merchant_reference"] else "no"}\n'
                f'Narration present: {"yes" if data["has_narration"] else "no"}\n'
                f'Transaction fingerprint: {data["transaction_key"][:12]}\n'
                'No wallet credited by this event. Verify transfer direction and settlement in Moniepoint.')
        for admin in settings.admins:
            await notice(s, f'mp:{key}:{admin}', admin, body)
    return True


async def pilot_status(session):
    async with session() as s:
        last = await s.scalar(select(MoniepointEvent).order_by(MoniepointEvent.received_at.desc()).limit(1))
    lines = ['🏦 Moniepoint transfer pilot',
             f'Signing secret: {"configured" if webhook_secret() else "not configured"}',
             'Automatic wallet credit: OFF; matched claims require confirmed incoming settlement',
             'Last verified event: ' + (f'{last.event_type} • {last.transaction_status}' if last else 'none')]
    return '\n'.join(lines)
