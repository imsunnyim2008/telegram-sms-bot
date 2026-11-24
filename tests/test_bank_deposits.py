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


async def event(session, kind, ref, name='Test Sender', amount=10000, status='COMPLETED', wid='event-1'):
    body = json.dumps({'eventType':kind, 'data': {
        'amount':amount, 'transactionReference':ref, 'transactionStatus':status,
        'responseCode':'20000', 'retrievalReferenceNumber':'123456789012',
        'metaData':json.dumps({'customerName':name})}}).encode()
    await record_transfer(session, wid, parse_transfer(body))


def test_any_positive_naira_amount():
    assert bank.amount_kobo('0.01') == 1
    assert bank.amount_kobo('1') == 100
    assert bank.amount_kobo('100.50') == 10050
    for value in ('0', '-1', 'NaN', '1.001'):
        with pytest.raises(ValueError):
            bank.amount_kobo(value)


@pytest.mark.asyncio
async def test_verified_claim_requires_review_and_credits_once(setup_database, monkeypatch):
    session, _, _ = setup_database
    await configure(session, monkeypatch)
    async with session() as s:
        before = (await s.get(User, 99)).balance
    cid = await bank.create_claim(session, 99, 10000, '1500', 'id', 'ref-123')
    duplicate = await bank.create_claim(session, ADMIN, 10000, '1500', 'name', 'Test Sender')
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
        assert (await s.get(User, 99)).balance == before + Decimal('0.0666')
        claim = await s.get(BankDeposit, cid)
        assert claim.status == 'paid'
        assert len((await s.scalars(select(LedgerEntry).where(LedgerEntry.ref_id == str(claim.payment_id),
                                                           LedgerEntry.type == 'topup'))).all()) == 1


@pytest.mark.asyncio
async def test_name_collision_and_reference_lookup(setup_database, monkeypatch):
    session, _, _ = setup_database
    await configure(session, monkeypatch)
    name_claim = await bank.create_claim(session,99,10000,'1500','name','Test Sender')
    await event(session,'V1_TRANSFER_TRANSACTION','ref-a',wid='a')
    await event(session,'V1_TRANSFER_TRANSACTION','ref-b',wid='b')
    matches = await bank.candidates(session,name_claim)
    assert len(matches) == 2
    with pytest.raises(ValueError):
        await bank.approve(session,ADMIN,name_claim,matches[0].event_key)
    id_claim = await bank.create_claim(session,99,10000,'1500','id','ref-a')
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
