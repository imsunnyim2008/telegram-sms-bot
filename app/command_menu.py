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
    BotCommand(command='start', description='🏠 Main menu'),
    BotCommand(command='buy', description='📱 New number order'),
    BotCommand(command='balance', description='💰 Check wallet balance'),
    BotCommand(command='topup', description='💳 Fund wallet'),
    BotCommand(command='orders', description='📋 My orders'),
    BotCommand(command='check', description='🔄 Check order status'),
    BotCommand(command='history', description='📜 Wallet history'),
    BotCommand(command='refunds', description='💸 Refund history'),
    BotCommand(command='receipt', description='🧾 Order receipts'),
    BotCommand(command='account', description='👤 My account'),
    BotCommand(command='done', description='✅ Confirm completed order'),
    BotCommand(command='support', description='💬 Contact support'),
    BotCommand(command='help', description='How to use Friday'),
    BotCommand(command='clear', description='🧹 Clear chat'),
    BotCommand(command='referral', description='👥 My referrals'),
    BotCommand(command='faq', description='❓ Frequently asked questions'),
    BotCommand(command='terms', description='📋 Terms & conditions'),
    BotCommand(command='privacy', description='🔒 Data & privacy information'),
    BotCommand(command='about', description='☀️ About FRIDAY'),
]

PUBLIC_HELP = (
    '📚 Friday Commands\n\n'
    '/start — Open the main menu\n'
    '/buy — Choose an SMS service and country\n'
    '/balance — Check your wallet balance\n'
    '/topup — Enter your deposit amount (minimum $5)\n'
    '/orders — Show recent purchases\n'
    '/check — Select an order to check\n'
    '/history — Recent wallet transactions\n'
    '/refunds — Confirmed refund history\n'
    '/receipt — Download your order record\n'
    '/account — Your account statistics\n'
    '/done — Confirm verification yourself\n'
    '/support — Request an order review\n'
    '/help — Show these instructions\n'
    '/clear — Clear recent chat messages (confirmation required)\n\n'
    '/reportnumber ORDER_ID — Report a rejected number\n'
    '/stockalert SERVICE COUNTRY — One-time stock notice\n'
    '/stockoff — Cancel stock alerts\n'
    '/promo CODE — Apply an active promotion\n'
    '/referral — View your referral link\n'
    '/faq — Common questions\n'
    '/terms — Terms of use\n'
    '/privacy — Data and privacy information\n'
    '/about — About FRIDAY\n\n'
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
    '/servicemode SERVICE on/off — Pause one service\n'
    '/setpromo CODE PERCENT — Set margin discount (0 disables)\n\n'
    '🔒 These commands are restricted to the bot administrator and are '
    'deliberately hidden from the public Menu button. '
    'Tap the buttons below instead of typing commands. Only custom values need text input.'
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
