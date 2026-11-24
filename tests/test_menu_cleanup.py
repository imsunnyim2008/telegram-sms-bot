"""Selecting a country or completing a purchase removes only the previous menu."""
from decimal import Decimal
from types import SimpleNamespace

import pytest

import app.handlers as handlers
from app.business import BotError
from app.provider import PriceItem


class MenuMessage:
    def __init__(self, *, deletion_fails=False):
        self.chat = SimpleNamespace(id=77, type='private')
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
    assert 'App: <tg-emoji emoji-id="5415999505952705793">📱</tg-emoji> Telegram' in msg.events[1][1]
    assert msg.events[1][2]['parse_mode'] == 'HTML'
    assert 'Number: 18176721188' in msg.events[1][1]
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


@pytest.mark.parametrize('service, expected_name, expected_id', [
    ('whatsapp', 'WhatsApp', '5418150421279572914'),
    ('facebook', 'Facebook', '5418039855936472761'),
    ('instagram', 'Instagram', '5418261965875217473'),
    ('google', 'Google', '5417924815237444975'),
    ('custom_service', 'Custom Service', None),
])
@pytest.mark.asyncio
async def test_purchase_receipt_shows_correct_app_for_every_service(
        monkeypatch, allowed_user, service, expected_name, expected_id):
    class FakeShop:
        async def buy(self, uid, chosen_service, country):
            assert (uid, chosen_service, country) == (77, service, '12')
            return SimpleNamespace(id=44, phone='15551110000')

    monkeypatch.setattr(handlers, 'shop', FakeShop())
    msg = MenuMessage()
    await handlers.callbacks(MenuCallback(f'buy:{service}:12', msg))
    assert [event[0] for event in msg.events] == ['ack', 'send', 'delete']
    body = msg.events[1][1]
    assert f'{expected_name}' in body
    if expected_id:
        assert f'<tg-emoji emoji-id="{expected_id}">📱</tg-emoji>' in body
    else:
        assert '<tg-emoji' not in body
    assert msg.events[1][2]['parse_mode'] == 'HTML'
    assert 'Number: 15551110000' in body
