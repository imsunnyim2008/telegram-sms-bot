"""Native Telegram command Menu is public; admin tools live in private settings."""
from types import SimpleNamespace

import pytest
from aiogram.types import (BotCommandScopeAllPrivateChats,
                           BotCommandScopeChat, BotCommandScopeDefault,
                           MenuButtonCommands)

from app import command_menu
import app.handlers as handlers
from app.config import settings


class FakeBot:
    def __init__(self, failed_admin=None):
        self.calls = []
        self.failed_admin = failed_admin

    async def set_my_commands(self, commands, scope):
        self.calls.append(('commands', commands, scope))
        if isinstance(scope, BotCommandScopeChat) and scope.chat_id == self.failed_admin:
            raise RuntimeError('Chat unavailable')
        return True

    async def set_chat_menu_button(self, menu_button):
        self.calls.append(('menu', menu_button))
        return True


@pytest.mark.asyncio
async def test_native_menu_has_only_all_working_public_commands():
    bot = FakeBot(failed_admin=73)
    assert await command_menu.register_native_menu(bot, {42, 73}) is True
    commands = [row for row in bot.calls if row[0] == 'commands']
    assert isinstance(commands[0][2], BotCommandScopeDefault)
    assert isinstance(commands[1][2], BotCommandScopeAllPrivateChats)
    assert {row[2].chat_id for row in commands[2:]} == {42, 73}
    assert any(isinstance(row[1], MenuButtonCommands) for row in bot.calls
               if row[0] == 'menu')
    expected = ['start', 'buy', 'balance', 'topup', 'orders', 'check', 'help']
    for _, cmds, _ in commands:
        assert [c.command for c in cmds] == expected
        assert 'pricing' not in [c.command for c in cmds]
        assert 'adjust' not in [c.command for c in cmds]
        assert 'admin' not in [c.command for c in cmds]
        assert 'broadcast' not in [c.command for c in cmds]


class FakeMessage:
    def __init__(self, uid=42, chat_type='private'):
        self.from_user = SimpleNamespace(id=uid, username='hello')
        self.chat = SimpleNamespace(type=chat_type)
        self.answers = []
        self.edits = []

    async def answer(self, text, **kwargs):
        self.answers.append((text, kwargs))

    async def edit_text(self, text, **kwargs):
        self.edits.append((text, kwargs))


class FakeCallback:
    def __init__(self, uid, message, data):
        self.from_user = SimpleNamespace(id=uid, username='hello')
        self.message = message
        self.data = data
        self.acks = []

    async def answer(self, *args, **kwargs):
        self.acks.append((args, kwargs))


@pytest.mark.asyncio
async def test_admin_settings_only_in_admin_main_menu(monkeypatch):
    monkeypatch.setattr(settings, 'admins', {42}, raising=False)
    monkeypatch.setattr(settings, 'public_base_url', 'https://friday.example.com')
    class Shop:
        async def ensure_user(self, uid, username):
            return SimpleNamespace(is_banned=False, accepted_terms=True)
    monkeypatch.setattr(handlers, 'shop', Shop())
    admin, guest = FakeMessage(42), FakeMessage(99)
    await handlers.start(admin)
    await handlers.start(guest)
    admin_buttons = [b.text for row in admin.answers[0][1]['reply_markup'].inline_keyboard
                     for b in row]
    guest_buttons = [b.text for row in guest.answers[0][1]['reply_markup'].inline_keyboard
                     for b in row]
    assert '⚙️ Admin Settings' in admin_buttons
    assert '⚙️ Admin Settings' not in guest_buttons
    assert guest_buttons == ['📱 Buy number', '💰 Balance', '💳 Top up', '📋 My orders', '❓ Help']


@pytest.mark.asyncio
async def test_admin_panel_shows_commands_only_for_owner(monkeypatch):
    monkeypatch.setattr(settings, 'admins', {42}, raising=False)
    monkeypatch.setattr(settings, 'public_base_url', 'https://friday.example.com')
    guest, admin = FakeMessage(99), FakeMessage(42)
    await handlers.admin_settings_command(guest)
    assert guest.answers[0][0] == 'Admin only'
    await handlers.admin_settings_command(admin)
    text, props = admin.answers[0]
    assert '/pricing' in text
    assert '/adjust USER_ID AMOUNT REASON' in text
    assert '/broadcast MESSAGE' in text
    assert '/setupcheck' in text
    btn = props['reply_markup'].inline_keyboard[0][0]
    assert btn.web_app.url == 'https://friday.example.com/admin/pricing'
    assert '/adjust' not in command_menu.PUBLIC_HELP
    assert '/pricing' not in command_menu.PUBLIC_HELP
    public_keyboard = handlers.main_menu_for(99)
    assert all(button.callback_data != 'admin:settings'
               for row in public_keyboard.inline_keyboard for button in row)


@pytest.mark.asyncio
async def test_admin_callback_cannot_show_settings_to_other_users(monkeypatch):
    monkeypatch.setattr(settings, 'admins', {42}, raising=False)
    async def allow(uid, target):
        return True
    monkeypatch.setattr(handlers, 'allowed', allow)
    guest, admin = FakeMessage(99), FakeMessage(42)
    await handlers.callbacks(FakeCallback(99, guest, 'admin:settings'))
    assert guest.answers[0][0] == 'Admin only'
    assert not guest.edits
    await handlers.callbacks(FakeCallback(42, admin, 'admin:settings'))
    assert len(admin.edits) == 1
    assert 'Admin Settings' in admin.edits[0][0]
    assert 'price manager' in admin.edits[0][0]
    assert 'Open Price Manager' in admin.edits[0][1]['reply_markup'].inline_keyboard[0][0].text
    assert '/stats' in admin.edits[0][0]
    assert 'admin:home' == admin.edits[0][1]['reply_markup'].inline_keyboard[-1][0].callback_data


@pytest.mark.asyncio
async def test_help_command_lists_all_public_commands_without_admin(monkeypatch):
    async def allow(uid, target):
        return True
    monkeypatch.setattr(handlers, 'allowed', allow)
    msg = FakeMessage(99)
    await handlers.help_command(msg)
    content = msg.answers[0][0]
    for command in ['start', 'buy', 'balance', 'topup', 'orders', 'check', 'help']:
        assert '/' + command in content
    assert '/adjust' not in content and '/pricing' not in content
