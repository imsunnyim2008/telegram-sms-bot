"""Protect live USD topups and change country selling prices entirely in Telegram."""
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.handlers as handlers
import app.business as business
from app.business import Shop, BotError
from app.config import settings
from app.db import Base, User, Payment, Activation, LedgerEntry
from app.provider import PriceItem


class Target:
    def __init__(self, uid=99, text='', chat_type='private'):
        self.from_user = SimpleNamespace(id=uid, username='test')
        self.chat = SimpleNamespace(id=uid, type=chat_type)
        self.text = text
        self.answers = []
        self.edits = []
        self.message_id = 600

    async def answer(self, msg, **kwargs):
        self.answers.append((msg, kwargs))

    async def edit_text(self, msg, **kwargs):
        self.edits.append((msg, kwargs))


class Provider:
    def __init__(self):
        self.offers = [
            PriceItem('telegram', '12', Decimal('0.45'), 100),
            PriceItem('whatsapp', '12', Decimal('0.25'), 100),
        ]
    async def get_prices(self):
        return self.offers
    async def get_country_names(self):
        return {'12': 'USA (2)'}
    async def get_balance(self):
        return Decimal('20')
    async def buy_number(self, service, country, max_price):
        assert (service, country, max_price) == ('telegram', '12', Decimal('.45'))
        return {'id': 666, 'phone': '15557778888', 'price': Decimal('.45')}


class Gateway:
    def __init__(self):
        self.invoices = []
    async def create_invoice(self, amount, order_id):
        self.invoices.append((amount, order_id))
        return {'invoice_id': str(1000 + len(self.invoices)),
                'pay_url': 'https://nowpayments.io/pay/demo'}


class Notifier:
    async def send(self, uid, msg):
        pass


@pytest.fixture
async def setup_database(monkeypatch):
    engine = create_async_engine('sqlite+aiosqlite:///:memory:')
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(handlers, 'Session', session)
    monkeypatch.setattr(business, 'Session', session)
    monkeypatch.setattr(settings, 'admins', {7031009238}, raising=False)
    monkeypatch.setattr(settings, 'provider_price_to_wallet', Decimal('1'))
    monkeypatch.setattr(settings, 'min_topup', Decimal('5'))
    monkeypatch.setattr(settings, 'max_topup', Decimal('500'))
    monkeypatch.setattr(settings, 'payment_provider', 'nowpayments')
    monkeypatch.setattr(settings, 'payment_test_admin_only', False)
    monkeypatch.setattr(settings, 'payments_enabled', True)
    monkeypatch.setattr(settings, 'nowpayments_api_key', 'test-key')
    monkeypatch.setattr(settings, 'nowpayments_ipn_secret', 'test-ipn-key')
    monkeypatch.setattr(settings, 'public_base_url', 'https://bot.example')
    monkeypatch.setattr(settings, 'live_purchases_enabled', True)
    monkeypatch.setattr(settings, 'provider_api_key', 'test-provider-key')
    async with session.begin() as db:
        db.add(User(id=99, accepted_terms=True, balance=Decimal('10.00')))
        db.add(User(id=7031009238, accepted_terms=True, balance=Decimal('10.00')))
    gateway = Gateway()
    shop = Shop(Provider(), gateway, Notifier())
    monkeypatch.setattr(handlers, 'shop', shop)
    yield session, shop, gateway
    await engine.dispose()


@pytest.mark.asyncio
async def test_topup_prompts_and_rejects_under_5_without_creating_invoice(setup_database):
    session, shop, gateway = setup_database
    message = Target(uid=99)
    await handlers.prompt_topup(message, 99)
    assert 'Minimum deposit: $5' in message.answers[0][0]
    assert (await handlers.read_flow(99))['type'] == 'topup'
    message.text = '4'
    await handlers.handle_pending_text(message)
    assert 'Minimum top-up is $5' in message.answers[-1][0]
    assert not gateway.invoices
    assert (await handlers.read_flow(99))['type'] == 'topup'
    message.text = '4.999'
    await handlers.handle_pending_text(message)
    assert 'Minimum top-up is $5' in message.answers[-1][0]
    assert not gateway.invoices
    message.text = '5'
    await handlers.handle_pending_text(message)
    assert len(gateway.invoices) == 1
    assert gateway.invoices[0][0] == Decimal('5')
    assert 'Pay using the official link' in message.answers[-1][0]
    assert await handlers.read_flow(99) is None
    async with session() as db:
        assert await db.scalar(select(func.count()).select_from(Payment)) == 1


@pytest.mark.asyncio
async def test_administrator_can_select_country_and_set_only_public_price(setup_database):
    session, shop, gateway = setup_database
    admin = Target(uid=7031009238)
    customer = Target(uid=99)
    await handlers.admin_price_services(admin)
    first = admin.answers[-1][1]['reply_markup'].inline_keyboard[0][0]
    assert first.callback_data == 'ps:countries:telegram:0'
    await handlers.admin_price_countries(admin, 'telegram')
    row = admin.answers[-1][1]['reply_markup'].inline_keyboard[0][0]
    assert row.callback_data == 'ps:set:telegram:12'
    assert '$0.6750 USD' in row.text
    await handlers.admin_price_prompt(admin, 7031009238, 'telegram', '12')
    assert 'Current public price: $0.6750' in admin.answers[-1][0]
    assert (await handlers.read_flow(7031009238))['type'] == 'price'
    admin.text = '0.95'
    await handlers.handle_pending_text(admin)
    assert 'PUBLIC PRICE SAVED' in admin.answers[-1][0]
    assert await handlers.read_flow(7031009238) is None
    item, quote = await shop.quote('telegram', '12')
    assert quote == Decimal('0.9500')
    # Customer sees only the configured public price; no provider cost.
    await handlers.countries(customer, 'telegram')
    visible = customer.answers[-1][1]['reply_markup'].inline_keyboard[0][0].text
    assert '$0.9500' in visible and '$0.4500' not in visible
    order = await shop.buy(99, 'telegram', '12')
    assert order.price_charged == Decimal('.9500')
    async with session() as db:
        user = await db.get(User, 99)
        assert user.balance == Decimal('9.0500')
        debits = (await db.scalars(select(LedgerEntry).where(
            LedgerEntry.user_id == 99, LedgerEntry.type == 'purchase'))).all()
        assert len(debits) == 1 and debits[0].amount == Decimal('-.9500')


@pytest.mark.asyncio
async def test_public_user_cannot_set_price_or_access_admin_flow(setup_database):
    session, shop, gateway = setup_database
    guest = Target(99)
    await handlers.admin_pricing_command(guest)
    assert guest.answers[-1][0] == 'Admin only'
    assert await handlers.read_flow(99) is None
    msg = Target(uid=99, text='0.001')
    await handlers.handle_pending_text(msg)
    assert not msg.answers
    assert (await shop.quote('telegram', '12'))[1] == Decimal('.6750')


@pytest.mark.asyncio
async def test_clear_chat_requires_confirmation_and_bulk_deletes_own_chat(setup_database):
    msg = Target(99)
    msg.text = '/clear'
    await handlers.clear_chat_command(msg)
    assert '48 hours' in msg.answers[-1][0]
    assert msg.answers[-1][1]['reply_markup'].inline_keyboard[0][0].callback_data == 'clear:confirm'
    class Bot:
        def __init__(self):
            self.ids = []
        async def delete_messages(self, chat_id, message_ids):
            assert chat_id == 99
            self.ids.append(message_ids)
    bot = Bot()
    msg.bot = bot
    await handlers.clear_chat_confirm(msg, 99)
    assert len(bot.ids) == 5
    assert bot.ids[0][-1] == 600
    other = Target(123)
    other.bot = bot
    await handlers.clear_chat_confirm(other, 99)
    assert other.answers[-1][0].startswith('Chat cleanup is only')
