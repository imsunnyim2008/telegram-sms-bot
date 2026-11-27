from types import SimpleNamespace

import pytest
import app.handlers as handlers


class Card:
    def __init__(self):
        self.chat = SimpleNamespace(id=99, type='private')
        self.calls = []

    async def answer(self, text, **kwargs):
        self.calls.append((text, kwargs))

    async def edit_text(self, text, **kwargs):
        self.calls.append((text, kwargs))


@pytest.mark.asyncio
async def test_catalogue_pages_and_unavailable_cards(monkeypatch):
    names = [f'example{i:02}' for i in range(25)]
    async def catalogue():
        return names, set()
    flows = {}
    async def save(uid, flow):
        flows[uid] = flow
    async def read(uid):
        return flows.get(uid)
    monkeypatch.setattr(handlers, 'catalogue_data', catalogue)
    monkeypatch.setattr(handlers, 'save_flow', save)
    monkeypatch.setattr(handlers, 'read_flow', read)
    card = Card()
    await handlers.catalogue_page(card, 99, 'example')
    buttons = [b for row in card.calls[-1][1]['reply_markup'].inline_keyboard for b in row]
    assert '25 results' in card.calls[-1][0]
    assert sum(b.callback_data.startswith('catalogue:item:') for b in buttons) == 10
    assert all(len(b.callback_data.encode()) <= 64 for b in buttons)
    await handlers.catalogue_callback(card, 99, 'catalogue:page:2')
    assert 'Page 3 of 3' in card.calls[-1][0]
    await handlers.catalogue_callback(card, 99, 'catalogue:item:' + handlers.catalogue_token(names[0]))
    text, kwargs = card.calls[-1]
    assert 'not confirmed' in text
    assert not any(b.callback_data.startswith(('buy:', 'preview:'))
                   for row in kwargs['reply_markup'].inline_keyboard for b in row)
