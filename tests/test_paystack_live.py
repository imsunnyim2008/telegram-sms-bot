from decimal import Decimal
import httpx
import pytest
import app.paystack_payments as ps
from app.config import settings
from app.db import Payment, User, LedgerEntry
from sqlalchemy import select
import test_inbot_pricing_topup as fixtures

setup_database = fixtures.setup_database


@pytest.mark.asyncio
async def test_live_credit_once_with_mock(setup_database, monkeypatch):
    session, _, _ = setup_database
    monkeypatch.setattr(ps, 'Session', session)
    monkeypatch.setattr(settings, 'paystack_secret_key', 'sk_live_fake')
    monkeypatch.setattr(settings, 'paystack_enabled', True)
    monkeypatch.setattr(settings, 'paystack_admin_only', False)
    monkeypatch.setattr(settings, 'paystack_ngn_per_usd', Decimal('1000'))
    state = {}
    from unittest.mock import AsyncMock
    notifier = AsyncMock()
    def respond(request):
        import json
        if request.method == 'POST':
            body = json.loads(request.content)
            state['reference'] = body['reference']
            assert body['channels'] == ['bank_transfer']
            return httpx.Response(200, json={'status':True,'data':{
                'reference':body['reference'],'authorization_url':'https://checkout.paystack.com/demo'}})
        return httpx.Response(200, json={'status':True,'data':{
            'reference':state['reference'],'domain':state.get('domain','test'),
            'status':'success','amount':50000,'currency':'NGN'}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        gateway = ps.PaystackLive('sk_live_fake', client)
        pid, _, _, credit = await ps.create_deposit(99,'500','tester@example.com',gateway=gateway,channel='bank_transfer')
        assert credit == Decimal('.5000')
        assert not await ps.settle(state['reference'],gateway=gateway,notifier=notifier)
        state['domain']='live'
        assert await ps.settle(state['reference'],gateway=gateway,notifier=notifier)
        assert not await ps.settle(state['reference'],gateway=gateway,notifier=notifier)
    notifier.send.assert_awaited_once()
    assert 'Top up successful!' in notifier.send.await_args.args[1]
    async with session() as db:
        assert (await db.get(User,99)).balance == Decimal('10.5000')
        assert (await db.get(Payment,pid)).status == 'paid'
        assert len((await db.scalars(select(LedgerEntry).where(LedgerEntry.ref_id==str(pid)))).all()) == 1
