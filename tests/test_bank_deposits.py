from decimal import Decimal
import json

import pytest
from sqlalchemy import select

from app import bank_deposits as bank
from app.db import BankDeposit, LedgerEntry, User
from app.moniepoint import parse_transfer, record_transfer
import test_inbot_pricing_topup as fixtures

setup_database = fixtures.setup_database
ADMIN = 7031009238


async def configure(session, monkeypatch):
    monkeypatch.setenv('MONIEPOINT_WEBHOOK_SECRET', 'unit-test-secret')
    await bank.update_config(session, ADMIN, 'name', 'Example Business')
    await bank.update_config(session, ADMIN, 'account', '0123456789')
    await bank.update_config(session, ADMIN, 'rate', '1500')
    await bank.update_config(session, ADMIN, 'enabled', True)


async def event(session, kind, ref, name='Test Sender', amount=50000, status='COMPLETED', wid='event-1'):
    body = json.dumps({'eventType':kind, 'data': {
        'amount':amount, 'transactionReference':ref, 'transactionStatus':status,
        'responseCode':'20000', 'retrievalReferenceNumber':'123456789012',
        'metaData':json.dumps({'customerName':name})}}).encode()
    await record_transfer(session, wid, parse_transfer(body))


def test_naira_minimum():
    assert bank.amount_kobo('500') == 50000
    assert bank.amount_kobo('500.50') == 50050
    for value in ('0', '-1', 'NaN', '1.001', '100', '499.99'):
        with pytest.raises(ValueError):
            bank.amount_kobo(value)


@pytest.mark.asyncio
async def test_verified_claim_requires_review_and_credits_once(setup_database, monkeypatch):
    session, _, _ = setup_database
    await configure(session, monkeypatch)
    async with session() as s:
        before = (await s.get(User, 99)).balance
    cid = await bank.create_claim(session, 99, 50000, '1500', 'id', 'ref-123')
    duplicate = await bank.create_claim(session, ADMIN, 50000, '1500', 'name', 'Test Sender')
    assert not await bank.candidates(session, cid)
    await event(session, 'V1_POS_WITHDRAWAL_TRANSACTION', 'ref-123', wid='withdrawal-1')
    assert not await bank.candidates(session, cid)
    await event(session, 'V1_TRANSFER_TRANSACTION', 'ref-123', status='PENDING', wid='pending-1')
    assert not await bank.candidates(session, cid)
    await event(session, 'V1_TRANSFER_TRANSACTION', 'ref-123', wid='transfer-1')
    matches = await bank.candidates(session, cid)
    assert len(matches) == 1
    assert len(await bank.candidates(session, duplicate)) == 1
    async with session() as s:
        assert (await s.get(User, 99)).balance == before
    with pytest.raises(ValueError):
        await bank.approve(session, 99, cid, matches[0].event_key)
    await bank.approve(session, ADMIN, cid, matches[0].event_key)
    with pytest.raises(ValueError):
        await bank.approve(session, ADMIN, duplicate, matches[0].event_key)
    with pytest.raises(ValueError):
        await bank.approve(session, ADMIN, cid, matches[0].event_key)
    async with session() as s:
        assert (await s.get(User, 99)).balance == before + Decimal('0.3333')
        claim = await s.get(BankDeposit, cid)
        assert claim.status == 'paid'
        assert len((await s.scalars(select(LedgerEntry).where(LedgerEntry.ref_id == str(claim.payment_id),
                                                           LedgerEntry.type == 'topup'))).all()) == 1


@pytest.mark.asyncio
async def test_name_collision_and_reference_lookup(setup_database, monkeypatch):
    session, _, _ = setup_database
    await configure(session, monkeypatch)
    name_claim = await bank.create_claim(session,99,50000,'1500','name','Test Sender')
    await event(session,'V1_TRANSFER_TRANSACTION','ref-a',wid='a')
    await event(session,'V1_TRANSFER_TRANSACTION','ref-b',wid='b')
    matches = await bank.candidates(session,name_claim)
    assert len(matches) == 2
    with pytest.raises(ValueError):
        await bank.approve(session,ADMIN,name_claim,matches[0].event_key)
    id_claim = await bank.create_claim(session,99,50000,'1500','id','ref-a')
    assert len(await bank.candidates(session,id_claim)) == 1


@pytest.mark.asyncio
async def test_configuration_changes_disable_public_bank_payments(setup_database, monkeypatch):
    session, _, _ = setup_database
    await configure(session, monkeypatch)
    assert bank.ready(await bank.config(session))
    await bank.update_config(session,ADMIN,'account','1234567890')
    assert not bank.ready(await bank.config(session))
    with pytest.raises(ValueError):
        await bank.update_config(session,99,'enabled',True)


@pytest.mark.asyncio
async def test_remark_session_and_transaction_number_match_without_automatic_credit(setup_database, monkeypatch):
    session, _, _ = setup_database
    await configure(session, monkeypatch)
    remark = bank.new_remark()
    assert len(remark) == 30 and remark != bank.new_remark()
    session_id = '0' * 29 + '1'
    number = '123456789012345678901234'
    claim = await bank.create_claim(session, 99, 50000, '1500', 'remark', remark)
    session_claim = await bank.create_claim(session, 99, 50000, '1500', 'session', session_id)
    number_claim = await bank.create_claim(session, 99, 50000, '1500', 'id', number)
    payload = {'eventType':'V1_TRANSFER_TRANSACTION','data':{
        'amount':50000,'transactionReference':'real-reference','transactionStatus':'COMPLETED',
        'responseCode':'20000','narration': 'TRANSFER ' + remark,
        'sessionId': session_id,'transactionNumber':number}}
    data = parse_transfer(json.dumps(payload).encode())
    assert remark not in str(data)
    assert session_id not in str(data)
    assert number not in str(data)
    await record_transfer(session, 'matched-rem', data)
    for cid in (claim, session_claim, number_claim):
        assert len(await bank.candidates(session, cid)) == 1
    async with session() as s:
        assert (await s.get(User,99)).balance == Decimal('10.00')
        assert (await s.get(BankDeposit,claim)).status == 'pending'


@pytest.mark.asyncio
async def test_new_claim_minimum_and_previous_quote(setup_database, monkeypatch):
    session, _, _ = setup_database
    await configure(session, monkeypatch)
    with pytest.raises(ValueError, match='500'):
        await bank.create_claim(session, 99, 49999, '1500', 'id', 'old-reference')
    claim = await bank.create_claim(session, 99, 10000, '1500', 'id', 'old-reference', existing_quote=True)
    async with session() as s:
        assert (await s.get(BankDeposit, claim)).amount_kobo == 10000


@pytest.mark.asyncio
async def test_name_only_reuses_quote_and_checks_actual_bank_time(setup_database, monkeypatch):
    from datetime import timedelta
    from app.db import utcnow
    session, _, _ = setup_database
    await configure(session, monkeypatch)
    cid = await bank.create_claim(session, 99, 50000, '1500', 'remark', bank.new_remark())
    with pytest.raises(ValueError):
        await bank.submit_sender_name(session, ADMIN, cid, 'Test Sender')
    assert await bank.submit_sender_name(session, 99, cid, ' Test   Sender ') == cid
    assert await bank.submit_sender_name(session, 99, cid, 'test sender') == cid
    with pytest.raises(ValueError):
        await bank.submit_sender_name(session, 99, cid, 'Different Person')
    for ref, stamp in [('old', (utcnow()-timedelta(days=2)).isoformat()),
                       ('missing', None), ('valid', utcnow().isoformat())]:
        payload = {'eventType':'V1_TRANSFER_TRANSACTION','data':{
            'amount':50000,'transactionReference':ref,'transactionStatus':'COMPLETED',
            'responseCode':'20000','transactionTime':stamp,
            'metaData':{'customerName':'TEST SENDER'}}}
        await record_transfer(session, ref, parse_transfer(json.dumps(payload).encode()))
    matches = await bank.candidates(session,cid)
    assert len(matches) == 1
    async with session() as s:
        assert len((await s.scalars(select(BankDeposit))).all()) == 1
        assert (await s.get(User,99)).balance == Decimal('10.00')
