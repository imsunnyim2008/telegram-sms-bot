"""Protect live USD topups and change country selling prices entirely in Telegram."""
from decimal import Decimal
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.handlers as handlers
import app.business as business
from app.business import Shop
from app.config import settings
from app.db import Base, User, Payment, LedgerEntry, Setting
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
        self.order_seq = 665
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
        self.order_seq += 1
        return {'id': self.order_seq, 'phone': '15557778888', 'price': Decimal('.45')}


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


@pytest_asyncio.fixture
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
    assert 'Choose a deposit method' in message.answers[0][0]
    assert await handlers.read_flow(99) is None
    await handlers.bank_button(message, 99, ['bankcrypto'])
    assert 'Minimum deposit: $5' in message.edits[-1][0]
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
    assert 'Original $0.4500 USD' in row.text
    assert 'Public $0.6750 USD' in row.text
    await handlers.admin_price_prompt(admin, 7031009238, 'telegram', '12')
    assert 'Original provider price (admin): $0.4500' in admin.answers[-1][0]
    assert 'Current public price (customers): $0.6750' in admin.answers[-1][0]
    assert (await handlers.read_flow(7031009238))['type'] == 'price'
    admin.text = '0.95'
    await handlers.handle_pending_text(admin)
    assert 'PUBLIC PRICE SAVED' in admin.answers[-1][0]
    assert await handlers.read_flow(7031009238) is None
    item, quote = await shop.quote('telegram', '12')
    assert quote == Decimal('0.9500')
    _, admin_quote = await shop.quote('telegram', '12', user_id=7031009238)
    assert admin_quote == Decimal('0.4500')
    # Customers see and pay only the configured public price.
    await handlers.countries(customer, 'telegram')
    visible = customer.answers[-1][1]['reply_markup'].inline_keyboard[0][0].text
    assert '0.9500 USD' in visible and '0.4500 USD' not in visible
    # The administrator sees the ORIGINAL price in their normal /buy menu.
    await handlers.countries(admin, 'telegram')
    admin_visible = admin.answers[-1][1]['reply_markup'].inline_keyboard[0][0].text
    assert '0.4500 USD' in admin_visible and '0.9500 USD' not in admin_visible
    order = await shop.buy(99, 'telegram', '12')
    assert order.price_charged == Decimal('.9500')
    async with session.begin() as db:
        admin = await db.get(User, 7031009238)
        admin.balance = Decimal('0')
    admin_order = await shop.buy(7031009238, 'telegram', '12')
    assert admin_order.price_charged == Decimal('0')
    assert admin_order.cost_provider == Decimal('.45')
    async with session() as db:
        user = await db.get(User, 99)
        assert user.balance == Decimal('9.0500')
        debits = (await db.scalars(select(LedgerEntry).where(
            LedgerEntry.user_id == 99, LedgerEntry.type == 'purchase'))).all()
        assert len(debits) == 1 and debits[0].amount == Decimal('-.9500')
        admin_user = await db.get(User, 7031009238)
        assert admin_user.balance == Decimal('0')
        admin_debits = (await db.scalars(select(LedgerEntry).where(
            LedgerEntry.user_id == 7031009238, LedgerEntry.type == 'purchase'))).all()
        assert not admin_debits
    assert await shop.refund(admin_order.id, 'cancelled')
    assert not await shop.refund(admin_order.id, 'cancelled')
    async with session() as db:
        assert (await db.get(User, 7031009238)).balance == Decimal('0')
        assert not (await db.scalars(select(LedgerEntry).where(
            LedgerEntry.user_id == 7031009238, LedgerEntry.type == 'refund'))).all()


@pytest.mark.asyncio
async def test_country_callbacks_and_purchase_previews_use_viewer_identity(setup_database):
    session, shop, _ = setup_database
    async with session.begin() as db:
        db.add(Setting(key='price:telegram:12', value='0.9500'))

    class Callback:
        def __init__(self, uid, data, target):
            self.from_user = SimpleNamespace(id=uid, username='test')
            self.data = data
            self.message = target

        async def answer(self, *args, **kwargs):
            pass

    admin = Target(uid=7031009238)
    customer = Target(uid=99)
    await handlers.callbacks(Callback(7031009238, 'countries:telegram:0', admin))
    admin_button = admin.edits[-1][1]['reply_markup'].inline_keyboard[0][0]
    assert '0.4500 USD' in admin_button.text
    assert '0.9500 USD' not in admin_button.text

    await handlers.callbacks(Callback(99, 'countries:telegram:0', customer))
    customer_button = customer.edits[-1][1]['reply_markup'].inline_keyboard[0][0]
    assert '0.9500 USD' in customer_button.text
    assert '0.4500 USD' not in customer_button.text

    await handlers.callbacks(Callback(7031009238, 'preview:telegram:12', admin))
    assert 'Original provider price: 0.4500 USD' in admin.answers[-1][0]
    await handlers.callbacks(Callback(99, 'preview:telegram:12', customer))
    assert 'Total price: 0.9500 USD' in customer.answers[-1][0]
    assert '0.4500' not in customer.answers[-1][0]


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


@pytest.mark.asyncio
async def test_topup_always_shows_both_choices_and_admin_bank_setup(setup_database, monkeypatch):
    session, _, gateway = setup_database
    monkeypatch.delenv('MONIEPOINT_WEBHOOK_SECRET', raising=False)
    message = Target(uid=7031009238)
    await handlers.prompt_topup(message, 7031009238)
    buttons = message.answers[-1][1]['reply_markup'].inline_keyboard
    assert {'ui:bankcrypto', 'ui:deposits'} <= {b.callback_data for row in buttons for b in row}
    assert 'ui:bankstart' not in {b.callback_data for row in buttons for b in row}
    await handlers.bank_button(message, 7031009238, ['bankstart'])
    assert 'Set account name' in str(message.edits[-1][1]['reply_markup'])
    assert not gateway.invoices


@pytest.mark.asyncio
async def test_bank_config_continues_without_restarting(setup_database, monkeypatch):
    session, _, gateway = setup_database
    monkeypatch.setenv('MONIEPOINT_WEBHOOK_SECRET', 'test-secret')
    uid = 7031009238
    message = Target(uid=uid)
    await handlers.save_flow(uid, {'type':'bank_input','action':'config','field':'name'})
    message.text = 'Example Merchant'
    await handlers.handle_pending_text(message)
    assert '10-digit Moniepoint account number' in message.answers[-1][0]
    assert (await handlers.read_flow(uid))['field'] == 'account'
    message.text = '0123456789'
    await handlers.handle_pending_text(message)
    assert 'exchange rate' in message.answers[-1][0]
    assert (await handlers.read_flow(uid))['field'] == 'rate'
    message.text = '1500'
    await handlers.handle_pending_text(message)
    assert 'Bank details saved' in message.answers[-1][0]
    assert await handlers.read_flow(uid) is None
    assert 'ui:banktoggle:on' in str(message.answers[-1][1]['reply_markup'])
    assert not gateway.invoices


@pytest.mark.asyncio
async def test_admin_balance_shows_provider_funds_only_in_private(setup_database):
    _, shop, _ = setup_database
    admin = Target(uid=7031009238)
    await handlers.show_balance(admin, 7031009238)
    assert 'Provider balance' in admin.answers[-1][0]
    assert '20 ' in admin.answers[-1][0]
    assert 'No FRIDAY top-up required' in admin.answers[-1][0]
    group = Target(uid=7031009238, chat_type='group')
    await handlers.show_balance(group, 7031009238)
    assert 'private chat' in group.answers[-1][0]
    customer = Target(uid=99)
    await handlers.show_balance(customer, 99)
    assert 'Your balance:' in customer.answers[-1][0]
    assert 'Provider balance' not in customer.answers[-1][0]


@pytest.mark.asyncio
async def test_topup_combined_paystack_with_crypto_and_no_standalone_bank_buttons(setup_database, monkeypatch):
    monkeypatch.setattr(handlers.paystack, 'eligible', lambda uid: True)
    message = Target(uid=99)
    await handlers.prompt_topup(message, 99)
    buttons = [b for row in message.answers[-1][1]['reply_markup'].inline_keyboard for b in row]
    assert [b.callback_data for b in buttons] == ['paystack:start', 'ui:bankcrypto', 'ui:deposits', 'menu:home']
    assert buttons[0].text == '💳 PAYSTACK TRANSFER OR DEBIT CARD'
    assert not any('bank transfer' in b.text.lower() for b in buttons)
