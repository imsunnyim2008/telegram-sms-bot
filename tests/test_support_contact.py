import pytest
import app.handlers as handlers
import test_inbot_pricing_topup as fixtures

setup_database = fixtures.setup_database


@pytest.mark.asyncio
async def test_support_command_shows_direct_admin_link(setup_database):
    msg = fixtures.Target(uid=99, text='/support')
    await handlers.customer_support(msg)
    body, kwargs = msg.answers[-1]
    assert '@waynelogs26' in body
    buttons = kwargs['reply_markup'].inline_keyboard
    assert buttons[0][0].url == 'https://t.me/waynelogs26'
    assert buttons[0][0].callback_data is None


def test_order_support_retains_ticket_options():
    buttons = handlers.support_keyboard(12).inline_keyboard
    assert buttons[0][0].url == 'https://t.me/waynelogs26'
    assert buttons[1][0].callback_data == 'ui:supportwrite:12'
    assert buttons[2][0].callback_data == 'ui:thread:12'
