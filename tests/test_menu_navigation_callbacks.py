"""Interactive menus should edit their existing Telegram card, not spam a new one."""
from decimal import Decimal
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import app.handlers as handlers
from app.db import Activation, Setting
import test_inbot_pricing_topup as test_fixtures

setup_database = test_fixtures.setup_database


class Card:
    def __init__(self, uid):
        self.from_user = SimpleNamespace(id=uid, username="test")
        self.chat = SimpleNamespace(id=uid, type="private")
        self.edits = []
        self.answers = []

    async def edit_text(self, body, **kwargs):
        self.edits.append((body, kwargs))

    async def answer(self, body, **kwargs):
        self.answers.append((body, kwargs))


class Click:
    def __init__(self, uid, message, data):
        self.from_user = SimpleNamespace(id=uid, username="test")
        self.message = message
        self.data = data

    async def answer(self, *args, **kwargs):
        pass


@pytest.mark.asyncio
async def test_customer_menu_navigation_edits_in_place(setup_database):
    session, shop, _ = setup_database
    card = Card(99)
    for index, (callback_data, marker) in enumerate([
        ("balance", "Your balance:"),
        ("help", "Friday Commands"),
        ("orders", "No orders yet"),
        ("topup", "Minimum deposit: $5"),
        ("menu:home", "Welcome! Choose an option"),
        ("clear:cancel", "Welcome! Choose an option"),
    ], start=1):
        await handlers.callbacks(Click(99, card, callback_data))
        assert len(card.edits) == index, callback_data
        assert not card.answers, callback_data
        assert marker.lower() in card.edits[-1][0].lower()
    assert await handlers.read_flow(99) is None


@pytest.mark.asyncio
async def test_admin_menu_reset_edits_in_place(setup_database):
    session, shop, _ = setup_database
    card = Card(7031009238)
    async with session.begin() as db:
        db.add(Setting(key="price:telegram:12", value="0.9500"))
    for index, data in enumerate([
        "admin:settings",
        "ps:services:0",
        "ps:countries:telegram:0",
        "ps:reset:telegram:12",
        "admin:settings",
        "admin:home",
    ], start=1):
        await handlers.callbacks(Click(7031009238, card, data))
        assert len(card.edits) == index, data
        assert not card.answers, data
    async with session() as db:
        assert await db.get(Setting, "price:telegram:12") is None


@pytest.mark.asyncio
async def test_order_refresh_keeps_check_cancel_buttons_in_single_card(setup_database):
    session, shop, _ = setup_database
    card = Card(99)
    async with session.begin() as db:
        db.add(Activation(
            user_id=99, service="telegram", country="12",
            status="manual_review", phone="15557778888",
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=15),
            cost_provider=Decimal("0.45"), price_charged=Decimal("0.95"),
        ))
    async with session() as db:
        aid = (await db.get(Activation, 1)).id

    await handlers.callbacks(Click(99, card, f"check:{aid}"))
    assert len(card.edits) == 1
    assert "support review" in card.edits[-1][0]
    assert not card.answers

    async with session.begin() as db:
        row = await db.get(Activation, aid)
        row.status = "pending"
    # Prevent external network requests in the on-demand status lookup.
    import app.workers as workers
    original = workers.Worker.poll_order

    async def poll_noop(self, activation_id):
        assert activation_id == aid

    workers.Worker.poll_order = poll_noop
    try:
        await handlers.callbacks(Click(99, card, f"check:{aid}"))
    finally:
        workers.Worker.poll_order = original
    assert len(card.edits) == 2
    content, kwargs = card.edits[-1]
    assert "15557778888" in content
    assert "App: ✈️ Telegram" in content
    buttons = [button.callback_data for row in kwargs["reply_markup"].inline_keyboard
               for button in row]
    assert f"check:{aid}" in buttons
    assert f"cancel:{aid}" in buttons
    assert "menu:home" in buttons
    assert not card.answers


@pytest.mark.asyncio
async def test_accept_terms_replaces_terms_prompt(setup_database):
    session, shop, _ = setup_database
    card = Card(99)
    await handlers.callbacks(Click(99, card, "accept"))
    assert len(card.edits) == 1
    assert card.edits[-1][0] == "Accepted. Welcome!"
    assert not card.answers


@pytest.mark.asyncio
async def test_latest_slash_command_menu_replaces_old_menu_without_deleting_invoices(setup_database):
    session, shop, _ = setup_database

    class Bot:
        def __init__(self):
            self.deleted = []

        async def delete_message(self, chat_id, message_id):
            self.deleted.append((chat_id, message_id))

    bot = Bot()
    request = SimpleNamespace(chat=SimpleNamespace(id=99, type="private"), bot=bot)
    await handlers.track_menu(request, SimpleNamespace(message_id=100))
    await handlers.track_menu(request, SimpleNamespace(message_id=101))
    assert bot.deleted == [(99, 100)]

    async with session() as db:
        current = await db.get(Setting, handlers.menu_key(99))
        assert current.value == "101"

    # Native /check, /help, /buy etc. retire only the menu, not unrelated
    # bot messages (such as a paid invoice or a sensitive SMS receipt).
    await handlers.discard_active_menu(request)
    assert bot.deleted == [(99, 100), (99, 101)]
    async with session() as db:
        assert await db.get(Setting, handlers.menu_key(99)) is None


@pytest.mark.asyncio
async def test_no_menu_tracking_or_deletion_in_groups(setup_database):
    session, _, _ = setup_database
    class Bot:
        async def delete_message(self, **kwargs):
            raise AssertionError("Must not delete group messages")

    request = SimpleNamespace(chat=SimpleNamespace(id=-1001, type="group"), bot=Bot())
    await handlers.track_menu(request, SimpleNamespace(message_id=123))
    await handlers.discard_active_menu(request)
    async with session() as db:
        assert await db.get(Setting, handlers.menu_key(-1001)) is None


@pytest.mark.asyncio
async def test_whatsapp_sms_status_and_orders_show_app_label(setup_database):
    session, shop, _ = setup_database
    card = Card(99)
    async with session.begin() as db:
        db.add(Activation(
            user_id=99, service="whatsapp", country="12",
            status="completed", phone="15557771111", code="678912",
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=15),
            cost_provider=Decimal("0.25"), price_charged=Decimal("0.80"),
        ))
    await handlers.callbacks(Click(99, card, "check:1"))
    assert "App: 💬 WhatsApp" in card.edits[-1][0]
    assert "Number: 15557771111" in card.edits[-1][0]
    assert "678912" in card.edits[-1][0]
    await handlers.callbacks(Click(99, card, "orders"))
    assert "App: 💬 WhatsApp" in card.edits[-1][0]
    assert "678912" not in card.edits[-1][0]
