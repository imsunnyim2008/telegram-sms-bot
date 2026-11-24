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
    BotCommand(command='clear', description='Clear recent chat messages'),
]

PUBLIC_HELP = (
    '📚 Friday Commands\n\n'
    '/start — Open the main menu\n'
    '/buy — Choose an SMS service and country\n'
    '/balance — Check your wallet balance\n'
    '/topup — Enter your deposit amount (minimum $5)\n'
    '/orders — Show recent purchases\n'
    '/check 7 — Check SMS for order #7 (replace 7 with your order ID)\n'
    '/history — Recent wallet transactions\n'
    '/refunds — Confirmed refund history\n'
    '/receipt ORDER_ID — Download your order record\n'
    '/account — Your account statistics\n'
    '/done ORDER_ID — Confirm verification yourself\n'
    '/support ORDER_ID — Request an order review\n'
    '/help — Show these instructions\n'
    '/clear — Clear recent chat messages (confirmation required)\n\n'
    'Use the blue Menu button beside the message field to access these commands. '
    'Only buy numbers for accounts you own or are authorized to test.'
)

ADMIN_HELP = (
    '⚙️ Friday · Admin Settings\n\n'
    '💵 Pricing\n'
    '/setprice — Set a public selling price in this chat\n'
    '/pricing — Also opens in-chat pricing\n'
    '/markup 50 — Set global markup percentage\n'
    '/cancel — Cancel an unfinished price or deposit entry\n\n'
    '🧪 Diagnostics\n'
    '/setupcheck — Check provider/payment setup\n'
    '/testall — Run read-only system diagnostics\n'
    '/stats — View customer and order statistics\n'
    '/dailyreport — Daily order and wallet activity (UTC)\n'
    '/maintenance on/off — Pause or resume new purchases\n'
    '/tickets — List open support requests\n'
    '/closeticket ORDER_ID — Close a reviewed ticket\n'
    '/customer USER_ID — Inspect a customer account\n\n'
    '👤 Customers & wallets\n'
    '/adjust USER_ID AMOUNT REASON — Adjust a wallet\n'
    '/ban USER_ID — Restrict a customer\n'
    '/unban USER_ID — Remove a restriction\n\n'
    '📣 Operations\n'
    '/broadcast MESSAGE — Message customers\n'
    '/resolve ORDER_ID refund — Resolve a reviewed SMS order\n'
    '/reconcilepayment PAYMENT_ID — Verify and recover a finished payment\n\n'
    '🔒 These commands are restricted to the bot administrator and are '
    'deliberately hidden from the public Menu button. '
    'Some commands require a user ID or order ID.'
)


async def register_native_menu(bot, admin_ids: set[int]):
    """Create the Telegram Menu button and publish only customer commands.

    Register the default and all-private scopes, plus explicit admin-private
    overrides, so no stale admin command list leaks into a native menu.
    An unreachable admin chat must not prevent registration for everyone.
    """
    try:
        await bot.set_my_commands(PUBLIC_COMMANDS, scope=BotCommandScopeDefault())
        await bot.set_my_commands(PUBLIC_COMMANDS, scope=BotCommandScopeAllPrivateChats())
        await bot.set_chat_menu_button(menu_button=MenuButtonCommands())
    except Exception as exc:
        # No bot token, request URL or raw API payload in logs.
        log.warning('Telegram public Menu setup failed: %s', type(exc).__name__)
        return False

    for uid in sorted(admin_ids):
        try:
            # Admin commands are accessible inside Admin Settings, not through
            # Telegram's public Menu—even to the administrator.
            await bot.set_my_commands(PUBLIC_COMMANDS, scope=BotCommandScopeChat(chat_id=uid))
        except Exception as exc:
            log.warning('Telegram admin chat command scope unavailable: %s', type(exc).__name__)

    log.info('Telegram Menu registered with %d public commands', len(PUBLIC_COMMANDS))
    return True
