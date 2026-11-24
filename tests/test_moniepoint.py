import base64
import hashlib
import hmac
import json

import httpx
import pytest
from sqlalchemy import select

import app.main as main
from app.db import MoniepointEvent, OpsNotice, User
from app.moniepoint import parse_transfer, valid_signature
import test_inbot_pricing_topup as fixtures

setup_database = fixtures.setup_database


def signed_event():
    body = json.dumps({
        'eventType': 'V1_TRANSFER_TRANSACTION',
        'data': {'amount': 10000, 'transactionReference': 'test-transfer',
                 'transactionStatus': 'COMPLETED', 'responseCode': '20000',
                 'merchantReference': 'invoice-test', 'narration': 'private narration',
                 'metaData': {'customerName': 'private name'}}}).encode()
    wid, stamp = 'test-event-123', '1728651860073'
    sig = base64.b64encode(hmac.new(b'test-secret',
                           wid.encode()+b'__'+stamp.encode()+b'__'+body,
                           hashlib.sha256).digest()).decode()
    return body, {'moniepoint-webhook-id': wid, 'moniepoint-webhook-timestamp': stamp,
                  'moniepoint-webhook-signature': sig}


def test_signature_and_payload():
    body, headers = signed_event()
    args = [headers['moniepoint-webhook-id'], headers['moniepoint-webhook-timestamp'],
            headers['moniepoint-webhook-signature'], 'test-secret']
    assert valid_signature(body, *args)
    assert not valid_signature(body+b' ', *args)
    data = parse_transfer(body)
    assert data['amount_kobo'] == 10000
    assert 'private' not in str(data)
    assert parse_transfer(b'{"eventType":"V1_POS_PURCHASE_TRANSACTION"}') is None
    with pytest.raises(ValueError):
        parse_transfer(b'{"eventType":"V1_POS_WITHDRAWAL_TRANSACTION"}')
    withdrawal = json.loads(body)
    withdrawal['eventType'] = 'V1_POS_WITHDRAWAL_TRANSACTION'
    assert parse_transfer(json.dumps(withdrawal).encode())['event_type'] == 'V1_POS_WITHDRAWAL_TRANSACTION'


@pytest.mark.asyncio
async def test_pilot_dedup_no_credit_and_rejects_forged_events(setup_database, monkeypatch):
    session, _, _ = setup_database
    monkeypatch.setattr(main, 'Session', session)
    monkeypatch.delenv('MONIEPOINT_WEBHOOK_SECRET', raising=False)
    body, headers = signed_event()
    async with session() as s:
        balance = (await s.get(User, 99)).balance
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.web),
                                 base_url='http://test') as client:
        assert (await client.post('/webhook/moniepoint', content=body, headers=headers)).status_code == 503
        monkeypatch.setenv('MONIEPOINT_WEBHOOK_SECRET', 'test-secret')
        assert (await client.post('/webhook/moniepoint', content=body)).status_code == 401
        assert (await client.post('/webhook/moniepoint', content=body+b' ', headers=headers)).status_code == 401
        for _ in range(2):
            assert (await client.post('/webhook/moniepoint', content=body, headers=headers)).status_code == 200
    async with session() as s:
        events = (await s.scalars(select(MoniepointEvent))).all()
        notices = (await s.scalars(select(OpsNotice))).all()
        assert len(events) == 1
        assert len(notices) == 1
        assert 'No wallet credited' in notices[0].body
        assert 'private narration' not in notices[0].body
        assert (await s.get(User, 99)).balance == balance
