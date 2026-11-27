"""Flags, preferred country positions, and single-message pagination."""
from decimal import Decimal
from types import SimpleNamespace

import pytest

import app.handlers as handlers
from app.country_ui import (FAVORITE_COUNTRIES, arrange_country_offers,
                            country_flag, country_iso2)
from app.provider import PriceItem


def offer(country, cost='0.50', stock=99):
    return PriceItem('telegram', country, Decimal(cost), stock)


def test_country_flags_for_provider_variants():
    assert country_flag('USA (2)') == '🇺🇸'
    assert country_flag('United Kingdom') == '🇬🇧'
    assert country_flag('Canada') == '🇨🇦'
    assert country_flag('Nigeria') == '🇳🇬'
    assert country_flag('Japan') == '🇯🇵'
    assert country_flag('Puerto Rico') == '🇵🇷'
    assert country_flag('Philippines') == '🇵🇭'
    assert country_flag('Réunion') == '🇷🇪'
    assert country_iso2('South Korea') == 'KR'


def test_five_priority_countries_are_first_in_requested_order():
    items = [offer('11', '0.10'), offer('12', '0.80'),
             offer('13', '0.60'), offer('14', '0.40'),
             offer('15', '0.70'), offer('16', '0.30')]
    names = {'11': 'Philippines', '12': 'Japan', '13': 'Nigeria',
             '14': 'UK', '15': 'Canada', '16': 'USA (2)'}
    actual = arrange_country_offers(items, names)
    assert [label for _, label, _ in actual[:5]] == [
        'USA', 'Canada', 'UK', 'Nigeria', 'Japan']
    assert [item.country for item, _, _ in actual[:6]] == [
        '16', '15', '14', '13', '12', '11']
    assert [flag for _, _, flag in actual[:5]] == [
        '🇺🇸', '🇨🇦', '🇬🇧', '🇳🇬', '🇯🇵']
    assert len(FAVORITE_COUNTRIES) == 5


def test_out_of_stock_favorites_are_visible_but_not_purchasable():
    actual = arrange_country_offers([offer('11')], {'11': 'Japan'})
    assert [entry[0] for entry in actual[:4]] == [None] * 4
    assert actual[4][0].country == '11'


class FakeMessage:
    def __init__(self):
        self.chat = SimpleNamespace(id=123, type='private')
        self.calls = []

    async def answer(self, text, **kwargs):
        self.calls.append(('send', text, kwargs))

    async def edit_text(self, text, **kwargs):
        self.calls.append(('edit', text, kwargs))


class FakeCallback:
    def __init__(self, data, message):
        self.data = data
        self.message = message
        self.from_user = SimpleNamespace(id=123)

    async def answer(self, *args, **kwargs):
        self.message.calls.append(('ack', args, kwargs))


@pytest.mark.asyncio
async def test_country_list_paginations_edit_same_message_not_send_new_ones(monkeypatch):
    class Provider:
        async def get_prices(self):
            return [offer(str(i), str(Decimal(i + 10) / 100))
                    for i in range(20)]
        async def get_country_names(self):
            return {'0': 'USA (2)', '1': 'Canada', '2': 'UK',
                    '3': 'Nigeria', '4': 'Japan',
                    **{str(i): f'Country #{i}' for i in range(5, 20)}}

    class Shop:
        provider = Provider()
        async def price(self, item, user_id=None):
            return item.cost

    async def allowed(uid, message):
        return True

    monkeypatch.setattr(handlers, 'shop', Shop())
    monkeypatch.setattr(handlers, 'allowed', allowed)
    flows = {}
    async def read_flow(uid):
        return flows.get(uid)
    async def save_flow(uid, flow):
        flows[uid] = flow
    async def clear_flow(uid):
        flows.pop(uid, None)
    monkeypatch.setattr(handlers, 'read_flow', read_flow)
    monkeypatch.setattr(handlers, 'save_flow', save_flow)
    monkeypatch.setattr(handlers, 'clear_flow', clear_flow)
    msg = FakeMessage()
    await handlers.callbacks(FakeCallback('countries:telegram:0', msg))
    await handlers.callbacks(FakeCallback('countries:telegram:1', msg))
    await handlers.callbacks(FakeCallback('countries:telegram:2', msg))
    await handlers.callbacks(FakeCallback('services:0', msg))
    assert [call[0] for call in msg.calls] == [
        'ack', 'edit', 'ack', 'edit', 'ack', 'edit', 'ack', 'edit']
    assert 'Enter the service name' in msg.calls[-1][1]
    assert flows[123] == {'type': 'service_search'}
    first_row = msg.calls[1][2]['reply_markup'].inline_keyboard[0][0]
    assert first_row.text.startswith('🇺🇸 USA')
    assert first_row.callback_data == 'preview:telegram:0'
    assert msg.calls[1][2]['reply_markup'].inline_keyboard[1][0].text.startswith('🇨🇦 Canada')


@pytest.mark.asyncio
async def test_unavailable_country_button_never_calls_shop_buy(monkeypatch):
    async def blocked(*_):
        raise AssertionError('No provider calls for out-of-stock countries')
    monkeypatch.setattr(handlers, 'allowed', blocked)
    msg = FakeMessage()
    await handlers.callbacks(FakeCallback('unavailable:usa', msg))
    assert msg.calls[0][0] == 'ack'
    assert msg.calls[0][2]['show_alert'] is True
    assert len(msg.calls) == 1
