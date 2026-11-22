"""Selecting a country or completing a purchase removes only the previous menu."""
from decimal import Decimal
from types import SimpleNamespace

import pytest

import app.handlers as handlers
from app.business import BotError
from app.provider import PriceItem


class MenuMessage:
    def __init__(self, *, deletion_fails=False):
        self.events = []
        self.deletion_fails = deletion_fails

    async def answer(self, text, **kwargs):
        self.events.append(('send', text, kwargs))

    async def delete(self):
        self.events.append(('delete',))
        if self.deletion_fails:
            raise RuntimeError('Telegram cannot delete old message')


class MenuCallback:
    def __init__(self, data, message):
        self.data = data
        self.message = message
        self.from_user = SimpleNamespace(id=77)

    async def answer(self):
        self.message.events.append(('ack',))


@pytest.fixture
def allowed_user(monkeypatch):
    async def allow(uid, target):
        return True
    monkeypatch.setattr(handlers, 'allowed', allow)


@pytest.mark.asyncio
async def test_choose_country_sends_confirmation_then_deletes_country_list(monkeypatch, allowed_user):
    class FakeShop:
        async def quote(self, service, country, user_id=None):
            assert (service, country) == ('telegram', '12')
            return PriceItem('telegram', '12', Decimal('0.29'), 100), Decimal('0.4485')

    monkeypatch.setattr(handlers, 'shop', FakeShop())
    msg = MenuMessage()
    await handlers.callbacks(MenuCallback('preview:telegram:12', msg))
    assert [event[0] for event in msg.events] == ['ack', 'send', 'delete']
    assert 'Confirm purchase' in msg.events[1][2]['reply_markup'].inline_keyboard[0][0].text


@pytest.mark.asyncio
async def test_confirm_purchase_sends_order_then_deletes_confirmation(monkeypatch, allowed_user):
    class FakeShop:
        async def buy(self, uid, service, country):
            assert (uid, service, country) == (77, 'telegram', '12')
            return SimpleNamespace(id=7, phone='18176721188')

    monkeypatch.setattr(handlers, 'shop', FakeShop())
    msg = MenuMessage()
    await handlers.callbacks(MenuCallback('buy:telegram:12', msg))
    assert [event[0] for event in msg.events] == ['ack', 'send', 'delete']
    assert 'Order #7' in msg.events[1][1]
    assert 'Check SMS' in msg.events[1][2]['reply_markup'].inline_keyboard[0][0].text


@pytest.mark.asyncio
async def test_delete_denied_does_not_resubmit_or_hide_purchased_order(monkeypatch, allowed_user):
    class FakeShop:
        def __init__(self): self.buys = 0

        async def buy(self, uid, service, country):
            self.buys += 1
            return SimpleNamespace(id=8, phone='18176721188')

    shop = FakeShop()
    monkeypatch.setattr(handlers, 'shop', shop)
    msg = MenuMessage(deletion_fails=True)
    await handlers.callbacks(MenuCallback('buy:telegram:12', msg))
    assert shop.buys == 1
    assert [event[0] for event in msg.events] == ['ack', 'send', 'delete']


@pytest.mark.asyncio
async def test_failed_quote_keeps_country_list(monkeypatch, allowed_user):
    class FakeShop:
        async def quote(self, service, country, user_id=None):
            raise BotError('No numbers available')

    monkeypatch.setattr(handlers, 'shop', FakeShop())
    msg = MenuMessage()
    await handlers.callbacks(MenuCallback('preview:telegram:12', msg))
    assert [event[0] for event in msg.events] == ['ack', 'send']
    assert 'No numbers available' in msg.events[1][1]
