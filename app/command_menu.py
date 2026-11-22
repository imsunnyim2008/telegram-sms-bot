"""Telegram's native Menu button shows PUBLIC commands for everyone.

Administrative commands are deliberately omitted from the native command list;
administrators can open their separate inline Admin Settings from /start or /admin.
"""
import logging

from aiogram.types import (BotCommand, BotCommandScopeAllPrivateChats,
                           BotCommandScopeChat, BotCommandScopeDefault,
                           MenuButtonCommands)


log = logging.getLogger(__name__)

PUBLIC_COMMANDS = [
    BotCommand(command='start', description='Open Friday main menu'),
    BotCommand(command='buy', description='Buy an SMS number'),
    BotCommand(command='balance', description='Check wallet balance'),
    BotCommand(command='topup', description='Add funds to your wallet'),
    BotCommand(command='orders', description='View your purchased numbers'),
    BotCommand(command='check', description='Check SMS for an order'),
    BotCommand(command='help', description='How to use Friday'),
]

PUBLIC_HELP = (
    '📚 Friday Commands\n\n'
    '/start — Open the main menu\n'
    '/buy — Choose an SMS service and country\n'
    '/balance — Check your wallet balance\n'
    '/topup 5 — Deposit $5 (or enter another amount)\n'
    '/orders — Show recent purchases\n'
    '/check 7 — Check SMS for order #7 (replace 7 with your order ID)\n'
    '/help — Show these instructions\n\n'
    'Use the blue Menu button beside the message field to access these commands. '
    'Only buy numbers for accounts you own or are authorized to test.'
)

ADMIN_HELP = (
    '⚙️ Friday · Admin Settings\n\n'
    '💵 Pricing\n'
    '/pricing — Open your web price manager\n'
    '/markup 50 — Set global markup percentage\n\n'
    '🧪 Diagnostics\n'
    '/setupcheck — Check provider/payment setup\n'
    '/testall — Run read-only system diagnostics\n'
    '/stats — View customer and order statistics\n\n'
    '👤 Customers & wallets\n'
    '/adjust USER_ID AMOUNT REASON — Adjust a wallet\n'
    '/ban USER_ID — Restrict a customer\n'
    '/unban USER_ID — Remove a restriction\n\n'
    '📣 Operations\n'
    '/broadcast MESSAGE — Message customers\n'
    '/resolve ORDER_ID OUTCOME — Resolve a reviewed SMS order\n'
    '/reconcilepayment PAYMENT_ID — Verify and recover a finished payment\n\n'
    '🔒 These commands are restricted to the bot administrator and are '
    'deliberately hidden from the public Menu button. '
    'Some commands require a user ID or order ID.'
)


async def register_native_menu(bot, admin_ids: set[int]):
    """Create native Telegram command list and explicit blue Menu button.

    Reapply on restart because BotFather or old command scopes may be stale.
    Never register admin-only commands in any public Telegram command scope.
    """
    try:
        await bot.set_my_commands(PUBLIC_COMMANDS, scope=BotCommandScopeDefault())
        await bot.set_my_commands(PUBLIC_COMMANDS, scope=BotCommandScopeAllPrivateChats())
        for uid in sorted(admin_ids):
            # Override a possible older admin chat-scoped menu with PUBLIC commands.
            await bot.set_my_commands(PUBLIC_COMMANDS, scope=BotCommandScopeChat(chat_id=uid))
        await bot.set_chat_menu_button(menu_button=MenuButtonCommands())
        log.info('Telegram native Menu registered with %d public commands', len(PUBLIC_COMMANDS))
        return True
    except Exception as exc:
        # A menu registration failure must never stop an active payment/SMS bot.
        # Do not log sensitive Bot API request URLs or tokens.
        log.warning('Telegram native Menu registration failed: %s', type(exc).__name__)
        return False
