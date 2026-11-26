"""Inline-keyboard Telegram UI. No financial operations live inside callbacks."""
import asyncio
import json
import logging
import time
from secrets import token_hex
from datetime import timedelta, timezone
from decimal import Decimal, InvalidOperation
from html import escape

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, CopyTextButton, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import String, cast, func, select

from .business import BotError
from .config import settings
from .country_ui import arrange_country_offers, country_flag
from .command_menu import PUBLIC_HELP, ADMIN_HELP
from .db import Activation, AdminAudit, BankDeposit, LedgerEntry, Payment, Session, Setting, SupportMessage, User, utcnow
from . import bank_deposits as bank
from .operations import LAGOS, STARTED_AT, audit, claim_purchase, notice, report, risk_summary
from .moniepoint import pilot_status
from .auto_maintenance import acknowledge, manual_mode
from .ledger import InsufficientFunds, apply
from .growth_controls import (set_service_pause, set_promo, redeem_promo, watch_stock,
                              record_referral, referral_count, SERVICE, PROMO)
from .service_ui import (app_label_html, custom_emoji_unavailable,
                         without_custom_emoji)

log = logging.getLogger(__name__)
router = Router()
shop = None  # assigned by app.main on startup


def set_shop(instance):
    global shop
    shop = instance


def keyboard(rows):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=label, callback_data=data) for label, data in row]
        for row in rows
    ])


MAIN = keyboard([
    [('📱 Buy number', 'services:0'), ('💰 Balance', 'balance')],
    [('💳 Top up', 'topup'), ('📋 My orders', 'orders')],
    [('📜 History', 'ui:history'), ('💸 Refunds', 'ui:refunds')],
    [('💳 My deposits', 'ui:deposits')],
    [('👤 My account', 'ui:account'), ('🎧 Support', 'ui:support')],
    [('❓ Help', 'help'), ('🧹 Clear chat', 'ui:clear')],
])

MAIN_ADMIN = keyboard([
    [('📱 Buy number', 'services:0'), ('💰 Balance', 'balance')],
    [('💳 Top up', 'topup'), ('📋 My orders', 'orders')],
    [('📜 History', 'ui:history'), ('💸 Refunds', 'ui:refunds')],
    [('💳 My deposits', 'ui:deposits')],
    [('👤 My account', 'ui:account'), ('🎧 Support', 'ui:support')],
    [('❓ Help', 'help'), ('🧹 Clear chat', 'ui:clear')],
    [('⚙️ Admin Settings', 'admin:settings')],
])


def main_menu_for(uid: int) -> InlineKeyboardMarkup:
    return MAIN_ADMIN if uid in settings.admins else MAIN


def admin_settings_keyboard() -> InlineKeyboardMarkup:
    return keyboard([
        [('💵 Change Public Prices', 'ps:services:0'), ('📈 Markup', 'ui:markup')],
        [('🛠 Maintenance', 'ui:maintenance'), ('📊 Business stats', 'ui:stats')],
        [('🗓 Reports', 'ui:reports'), ('🧪 System check', 'ui:testall')],
        [('🛡 Risk review', 'ui:risk'), ('📜 Admin activity', 'ui:audit')],
        [('🔍 Find customer', 'ui:searchcustomer'), ('💵 Low balance alert', 'ui:lowthreshold')],
        [('📡 Health dashboard', 'ui:health'), ('🏦 Moniepoint pilot', 'ui:moniepoint')],
        [('🏦 Bank transfers', 'ui:bankadmin')],
        [('🔌 API setup check', 'ui:setupcheck'), ('👥 Customers', 'ui:customers')],
        [('🎧 Support tickets', 'ui:tickets'), ('💳 Reconcile payment', 'ui:reconcilepayment')],
        [('📣 Broadcast', 'ui:broadcast'), ('⚖️ Review orders', 'ui:review')],
        [('⬅️ Back to Main Menu', 'admin:home')],
    ])

TERMS = ('Terms: Buy only for accounts you own or are authorized to test. '
         'Respect the provider and destination platform rules. No fraud, impersonation, '
         'spam, bulk account creation or automated platform evasion. '
         'Unused activation credits are returned after confirmed cancellation/expiry. '
         'An unknown provider purchase outcome may require review. '
         'Do not share codes or phone numbers publicly. Accept to continue.')


async def allowed(uid: int, target) -> bool:
    user = await shop.get_user(uid)
    if not user or user.is_banned or not user.accepted_terms:
        await target.answer('Please use /start and accept the Terms first.')
        return False
    return True


@router.message(CommandStart(ignore_case=True))
async def start(message: Message):
    u = await shop.ensure_user(message.from_user.id, message.from_user.username or '')
    payload = (getattr(message, 'text', '') or '').split(maxsplit=1)
    if not u.accepted_terms and len(payload) == 2 and payload[1].startswith('ref_'):
        ref = payload[1][4:]
        if ref.isascii() and ref.isdigit() and len(ref) <= 16:
            await record_referral(Session, message.from_user.id, int(ref))
    if u.is_banned:
        await message.answer('Account restricted. Contact support.')
    elif not u.accepted_terms:
        await show_selection_menu(
            message, TERMS, keyboard([[('✅ I accept', 'accept')]]),
        )
    else:
        await show_selection_menu(
            message, 'Welcome! Choose an option:', main_menu_for(message.from_user.id),
        )


@router.message(Command('admin', ignore_case=True))
async def admin_settings_command(message: Message):
    uid = message.from_user.id
    if uid not in settings.admins:
        return await message.answer('Admin only')
    if message.chat.type != 'private':
        return await message.answer('Open Friday in a private chat to manage admin settings.')
    await show_selection_menu(message, ADMIN_HELP, admin_settings_keyboard())


@router.message(Command('help', ignore_case=True))
async def help_command(message: Message):
    if await allowed(message.from_user.id, message):
        await show_selection_menu(message, PUBLIC_HELP,
                                  main_menu_for(message.from_user.id))


def menu_key(chat_id: int) -> str:
    return f'active_menu:{chat_id}'


async def discard_active_menu(message) -> None:
    """Retire only the latest BOT MENU, never payment invoices or order receipts.

    Deletion is best-effort: Telegram can refuse messages over 48 hours old.
    A failed cleanup never stops a bot command or a financial operation.
    """
    chat = getattr(message, 'chat', None)
    bot = getattr(message, 'bot', None)
    if getattr(chat, 'type', None) != 'private' or bot is None:
        return
    try:
        async with Session.begin() as s:
            row = await s.get(Setting, menu_key(chat.id))
            old_id = int(row.value) if row else None
            if row:
                await s.delete(row)
        if old_id:
            try:
                await bot.delete_message(chat_id=chat.id, message_id=old_id)
            except Exception:
                pass
    except Exception:
        pass


async def track_menu(message, sent) -> None:
    """Persist the active menu ID in the database across server restarts."""
    chat = getattr(message, 'chat', None)
    bot = getattr(message, 'bot', None)
    message_id = getattr(sent, 'message_id', None)
    if (getattr(chat, 'type', None) != 'private' or bot is None or
            not isinstance(message_id, int) or message_id <= 0):
        return
    try:
        async with Session.begin() as s:
            row = await s.get(Setting, menu_key(chat.id))
            previous = int(row.value) if row else None
            if row:
                row.value = str(message_id)
            else:
                s.add(Setting(key=menu_key(chat.id), value=str(message_id)))
        if previous and previous != message_id:
            try:
                await bot.delete_message(chat_id=chat.id, message_id=previous)
            except Exception:
                pass
    except Exception:
        pass


@router.message.middleware()
async def tidy_before_command(handler, event, data):
    """Blue Telegram Menu commands also retire the previous visible card."""
    if getattr(getattr(event, 'chat', None), 'type', None) != 'private':
        return
    if (getattr(event, 'text', '') or '').startswith('/'):
        await clear_flow(event.from_user.id)
        await discard_active_menu(event)
    return await handler(event, data)


async def remove_previous_menu(message) -> None:
    """Remove a completed selection/confirmation card without failing a purchase.

    A Telegram message may be too old, already removed, or inaccessible.
    The next screen is sent first; cleanup must not trigger another purchase.
    """
    try:
        await message.delete()
    except Exception:
        # Deletion is cosmetic; never mistake it for a failed paid transaction.
        pass


async def send_branded_message(target, html_text: str, **kwargs):
    """Use brand logos where supported, otherwise fall back to the app name."""
    try:
        return await target.answer(html_text, parse_mode="HTML", **kwargs)
    except TelegramBadRequest as exc:
        if not custom_emoji_unavailable(exc):
            raise
        return await target.answer(without_custom_emoji(html_text), **kwargs)


async def show_selection_menu(target, title: str, markup, *,
                              edit: bool = False, track: bool = True,
                              parse_mode: str | None = None):
    """Reuse the current Telegram card for menus, with optional branded HTML."""
    async def render(method):
        options = {'reply_markup': markup}
        if parse_mode is not None:
            options['parse_mode'] = parse_mode
        try:
            return await method(title, **options)
        except TelegramBadRequest as exc:
            if parse_mode == 'HTML' and custom_emoji_unavailable(exc):
                return await method(without_custom_emoji(title), reply_markup=markup)
            raise

    if edit:
        try:
            edited = await render(target.edit_text)
            if track:
                await track_menu(target, edited or target)
            return edited
        except TelegramBadRequest as exc:
            if 'message is not modified' in str(exc).lower():
                return
        except (AttributeError, TypeError):
            pass
        new_menu = await render(target.answer)
        if track:
            await track_menu(target, new_menu)
        await remove_previous_menu(target)
        return new_menu
    new_menu = await render(target.answer)
    if track:
        await track_menu(target, new_menu)
    return new_menu

@router.callback_query()
async def callbacks(callback: CallbackQuery):
    # Answer immediately; lengthy provider calls happen AFTER Telegram acknowledgement.
    uid = callback.from_user.id
    data = callback.data or ''
    if getattr(getattr(callback.message, 'chat', None), 'type', None) != 'private':
        await callback.answer('Open Friday in a private chat.', show_alert=True)
        return
    if data.startswith('unavailable:'):
        await callback.answer('No numbers in stock for this country and service.', show_alert=True)
        return
    await callback.answer()
    if data == 'accept':
        await shop.ensure_user(uid, callback.from_user.username or '')
        await shop.accept_terms(uid)
        await show_selection_menu(callback.message, 'Accepted. Welcome!',
                                  main_menu_for(uid), edit=True)
        return
    if not await allowed(uid, callback.message):
        return
    try:
        if data.startswith('ui:'):
            await button_action(callback, uid, data[3:])
        elif data.startswith('services:'):
            await services(callback.message, int(data.split(':')[1]), edit=True)
        elif data.startswith('countries:'):
            _, service, page = data.split(':')
            await countries(callback.message, service, int(page), edit=True,
                            viewer_id=uid)
        elif data.startswith('preview:'):
            _, service, country = data.split(':')
            item, charge = await shop.quote(service, country, user_id=uid)
            provider = getattr(shop, 'provider', None)
            names = (await provider.get_country_names()
                     if hasattr(provider, 'get_country_names') else {})
            selected_country = names.get(country, f'Country #{country}')
            price_label = 'Original provider price' if uid in settings.admins else 'Total price'
            await callback.message.answer(
                f'{service.title()} • {country_flag(selected_country)} {selected_country}\n'
                f'Available: {item.stock}\n{price_label}: {charge} {settings.wallet_currency}\n'
                'You are charged only if a code is received; no-code cancellations are refunded.',
                reply_markup=keyboard([[('✅ Confirm purchase', f'buy:{service}:{country}')],
                                       [('⬅️ Back', f'countries:{service}:0')]]),
            )
            # The country list is no longer needed after a country is selected.
            await remove_previous_menu(callback.message)
        elif data.startswith('buy:'):
            _, service, country = data.split(':')
            message_id = getattr(callback.message, 'message_id', None)
            if isinstance(message_id, int) and message_id > 0:
                date = getattr(callback.message, 'date', None)
                if date and (utcnow() - date).total_seconds() > 86400:
                    return await callback.message.answer('Purchase confirmation expired. Open Buy number again.')
                if not await claim_purchase(Session, uid, message_id):
                    return await callback.message.answer('This confirmation was already processed. Check My orders.')
            order = await shop.buy(uid, service, country)
            await send_branded_message(
                callback.message,
                f'✅ Order #{order.id}\nApp: {app_label_html(service)}\n'
                f'Number: {escape(str(order.phone))}\n'
                f'Expires in up to {settings.activation_timeout_seconds // 60} minutes.\n'
                'Send an SMS to this number from your authorized account. '
                'Tap Check SMS if the message does not arrive automatically.',
                reply_markup=keyboard([[('🔄 Check SMS now', f'check:{order.id}')],
                                       [('❌ Cancel order', f'cancel:{order.id}')]]),
            )
            # Keep the new order receipt; remove the stale confirm-purchase card.
            await remove_previous_menu(callback.message)
        elif data.startswith('done:'):
            await mark_customer_done(callback.message, uid, int(data.split(':')[1]))
        elif data.startswith('check:'):
            await check_sms(callback.message, uid, int(data.split(':')[1]), edit=True)
        elif data.startswith('cancel:'):
            await shop.cancel(uid, int(data.split(':')[1]))
            await show_selection_menu(
                callback.message, 'Cancellation submitted. Check /orders for current status.',
                keyboard([[('⬅️ Main menu', 'menu:home')]]), edit=True, track=False,
            )
        elif data == 'balance':
            await show_balance(callback.message, uid, edit=True)
        elif data == 'topup':
            await prompt_topup(callback.message, uid, edit=True)
        elif data == 'orders':
            await show_orders(callback.message, uid, edit=True)
        elif data == 'help':
            await show_selection_menu(callback.message, PUBLIC_HELP,
                                      main_menu_for(uid), edit=True)
        elif data == 'menu:home':
            # Leaving a price/top-up wizard must not leave a pending amount input.
            await clear_flow(uid)
            await show_selection_menu(callback.message, 'Welcome! Choose an option:',
                                      main_menu_for(uid), edit=True)
        elif data.startswith('ps:'):
            if uid not in settings.admins or callback.message.chat.type != 'private':
                await callback.message.answer('Admin only')
            else:
                parts = data.split(':')
                if parts[1] == 'services' and len(parts) == 3:
                    await admin_price_services(callback.message, int(parts[2]), edit=True)
                elif parts[1] == 'countries' and len(parts) == 4:
                    await admin_price_countries(callback.message, parts[2],
                                                int(parts[3]), edit=True)
                elif parts[1] == 'set' and len(parts) == 4:
                    await admin_price_prompt(callback.message, uid, parts[2], parts[3],
                                             edit=True)
                elif parts[1] == 'reset' and len(parts) == 4:
                    await admin_price_reset(callback.message, parts[2], parts[3],
                                            edit=True, actor=uid)
                else:
                    await callback.message.answer('Invalid selection. Use /setprice.')
        elif data == 'clear:confirm':
            await clear_chat_confirm(callback.message, uid)
        elif data == 'clear:cancel':
            await show_selection_menu(callback.message, 'Welcome! Choose an option:',
                                      main_menu_for(uid), edit=True)
        elif data == 'admin:settings':
            if uid not in settings.admins:
                await callback.message.answer('Admin only')
            elif callback.message.chat.type != 'private':
                await callback.message.answer('Open Friday in a private chat for admin settings.')
            else:
                await clear_flow(uid)
                await show_selection_menu(callback.message, ADMIN_HELP,
                                          admin_settings_keyboard(), edit=True)
        elif data == 'admin:home':
            await clear_flow(uid)
            if uid in settings.admins:
                await show_selection_menu(callback.message, 'Welcome! Choose an option:',
                                          main_menu_for(uid), edit=True)
    except (BotError, ProviderErrorMessage) as exc:
        await callback.message.answer(str(exc))
    except (ValueError, IndexError):
        await callback.message.answer('Invalid selection. Open /start to try again.')
    except Exception:
        await callback.message.answer('Service temporarily unavailable. Please try again later.')


# Keep a local alias to avoid leaking provider exception details in Telegram.
ProviderErrorMessage = BotError


async def services(target, page: int = 0, *, edit: bool = False):
    try:
        items = await shop.provider.get_prices()
    except Exception as exc:
        log.warning('Price lookup failed: %s', type(exc).__name__)
        await target.answer('Live prices are temporarily unavailable.')
        return
    available = {x.service for x in items}
    popular = ['telegram', 'whatsapp', 'google', 'facebook', 'instagram', 'discord', 'amazon', 'microsoft']
    names = sorted(available, key=lambda s: (0, popular.index(s)) if s in popular else (1, s))
    page = max(0, min(page, max(0, (len(names) - 1) // 8)))
    chunk = names[page * 8:(page + 1) * 8]
    rows = [[(f'{name.title()}', f'countries:{name}:0')] for name in chunk]
    nav = []
    if page:
        nav.append(('⬅️ Prev', f'services:{page-1}'))
    if len(names) > (page + 1) * 8:
        nav.append(('Next ➡️', f'services:{page+1}'))
    if nav:
        rows.append(nav)
    await show_selection_menu(target, f'Choose a service ({len(names)} in stock):',
                              keyboard(rows), edit=edit)


async def countries(target, service: str, page: int = 0, *, edit: bool = False,
                    viewer_id: int | None = None):
    # Callback messages originate from the bot, not from the human pressing
    # the button. The callback handler must forward the real Telegram user ID.
    if viewer_id is None:
        viewer_id = getattr(getattr(target, 'from_user', None), 'id', None)
    if not settings.service_allowed(service):
        raise BotError('Service disabled')
    items = [x for x in await shop.provider.get_prices() if x.service == service]
    country_names = (await shop.provider.get_country_names()
                     if hasattr(shop.provider, 'get_country_names') else {})
    # The five preferred countries ALWAYS occupy the first five slots.
    # Missing provider offers are labelled unavailable, never sold as stock.
    # TextVerified currently only supplies US numbers. Never display four
    # invented foreign-country choices in this provider's catalogue.
    arranged = ([(x, 'USA', '🇺🇸') for x in items] if
                settings.provider_name == 'textverified' else
                arrange_country_offers(items, country_names))
    page = max(0, min(page, max(0, (len(arranged) - 1) // 8)))
    chunk = arranged[page * 8:(page + 1) * 8]
    rows = []
    for item, label, flag in chunk:
        if item is None:
            rows.append([(f'{flag} {label} • out of stock',
                          f'unavailable:{label.lower()}')])
        else:
            if settings.provider_price_to_wallet > 0:
                price = f'{await shop.price(item, user_id=viewer_id)} {settings.wallet_currency}'
            else:
                price = 'price setup required'
            rows.append([(f'{flag} {label} • {price} • {item.stock} left',
                          f'preview:{service}:{item.country}')])
    nav = []
    if page:
        nav.append(('⬅️ Prev', f'countries:{service}:{page-1}'))
    if len(arranged) > (page + 1) * 8:
        nav.append(('Next ➡️', f'countries:{service}:{page+1}'))
    if nav:
        rows.append(nav)
    rows.append([('Back to services', 'services:0')])
    await show_selection_menu(target, f'Select a country for {service}:',
                              keyboard(rows), edit=edit)

# The last step of a pricing or wallet wizard survives Render restarts.
# Never keep prices or financial flows only in an in-memory dict.
_FLOW_TIMEOUT = 15 * 60


def flow_key(uid: int) -> str:
    return f'flow:{uid}'


async def save_flow(uid: int, flow: dict):
    payload = {**flow, 'expires': int(time.time()) + _FLOW_TIMEOUT}
    async with Session.begin() as s:
        row = await s.get(Setting, flow_key(uid))
        if row:
            row.value = json.dumps(payload)
        else:
            s.add(Setting(key=flow_key(uid), value=json.dumps(payload)))


async def read_flow(uid: int) -> dict | None:
    async with Session() as s:
        row = await s.get(Setting, flow_key(uid))
    if not row:
        return None
    try:
        value = json.loads(row.value)
        if isinstance(value, dict) and int(value.get('expires', 0)) >= int(time.time()):
            return value
    except (ValueError, TypeError):
        pass
    return None


async def clear_flow(uid: int):
    async with Session.begin() as s:
        row = await s.get(Setting, flow_key(uid))
        if row:
            await s.delete(row)


def _pages(items, page: int, size: int = 8):
    page = max(0, min(page, max(0, (len(items) - 1) // size)))
    return page, items[page * size:(page + 1) * size]


async def admin_price_services(target, page: int = 0, *, edit: bool = False):
    prices = [p for p in await shop.provider.get_prices()
              if settings.service_allowed(p.service) and settings.country_allowed(p.country)]
    available = {p.service for p in prices}
    popular = ['telegram', 'whatsapp', 'google', 'facebook', 'instagram',
               'discord', 'amazon', 'microsoft']
    names = sorted(available, key=lambda s: (
        0, popular.index(s)) if s in popular else (1, s))
    page, chunk = _pages(names, page)
    rows = [[(name.title(), f'ps:countries:{name}:0')] for name in chunk]
    nav = []
    if page > 0:
        nav.append(('⬅️ Prev', f'ps:services:{page - 1}'))
    if (page + 1) * 8 < len(names):
        nav.append(('Next ➡️', f'ps:services:{page + 1}'))
    if nav:
        rows.append(nav)
    rows.append([('⬅️ Admin Settings', 'admin:settings')])
    await show_selection_menu(
        target, '💵 Change public price — Select a service:',
        keyboard(rows), edit=edit,
    )


async def admin_price_countries(target, service: str, page: int = 0,
                                *, edit: bool = False):
    if not settings.service_allowed(service):
        raise BotError('Service not available')
    offers = [p for p in await shop.provider.get_prices() if
              p.service == service and settings.country_allowed(p.country)]
    names = (await shop.provider.get_country_names()
             if hasattr(shop.provider, 'get_country_names') else {})
    # Show only countries with live offers (never invent provider stock).
    arranged = [(item, label, flag) for item, label, flag in
                arrange_country_offers(offers, names) if item is not None]
    page, chunk = _pages(arranged, page)
    rows = []
    for item, name, flag in chunk:
        # Admins can compare their original provider cost with the customer price.
        original = shop.original_price(item)
        public_price = await shop.price(item)
        rows.append([(f'{flag} {name} • Original ${original} USD | Public ${public_price} USD',
                      f'ps:set:{service}:{item.country}')])
    nav = []
    if page > 0:
        nav.append(('⬅️ Prev', f'ps:countries:{service}:{page - 1}'))
    if (page + 1) * 8 < len(arranged):
        nav.append(('Next ➡️', f'ps:countries:{service}:{page + 1}'))
    if nav:
        rows.append(nav)
    rows.append([('⬅️ Services', 'ps:services:0')])
    await show_selection_menu(
        target, f'💵 Change public price — {service.title()}\nChoose a country:',
        keyboard(rows), edit=edit,
    )


async def admin_price_prompt(target, uid: int, service: str, country: str,
                             *, edit: bool = False):
    if uid not in settings.admins:
        raise BotError('Admin only')
    item, current = await shop.quote(service, country)
    names = (await shop.provider.get_country_names()
             if hasattr(shop.provider, 'get_country_names') else {})
    name = names.get(country, f'Country #{country}')
    await save_flow(uid, {'type': 'price', 'service': service, 'country': country})
    await show_selection_menu(
        target,
        f'✏️ SET PUBLIC SELLING PRICE\n'
        f'Service: {service.title()}\nCountry: {country_flag(name)} {name}\n'
        f'Original provider price (admin): ${shop.original_price(item)} USD\n'
        f'Current public price (customers): ${current} USD\n\n'
        'Send your NEW public price in USD (example: 0.90).\n'
        'Customers will only see your selling price, not the provider cost.\n'
        'Their wallet will be debited this selling price when they purchase.\n'
        'To cancel, send /cancel.',
        keyboard([[('⬅️ Cancel / Main menu', 'menu:home')]]),
        edit=edit,
    )


async def admin_price_reset(target, service: str, country: str, *, edit: bool = False, actor=None):
    if not settings.service_allowed(service) or not settings.country_allowed(country):
        raise BotError('Invalid price selection')
    async with Session.begin() as s:
        row = await s.get(Setting, f'price:{service}:{country}')
        if row:
            await s.delete(row)
        if actor is not None:
            audit(s, actor, 'price_reset', f'{service}:{country}')
    await show_selection_menu(
        target, '✅ Custom public price reset. Global markup will apply to new purchases.',
        keyboard([[('💵 Change another price', 'ps:services:0')],
                  [('⬅️ Admin Settings', 'admin:settings')]]),
        edit=edit,
    )


async def admin_price_save(target, uid: int, flow: dict, raw: str):
    service, country = flow['service'], flow['country']
    if uid not in settings.admins:
        return
    try:
        value = Decimal(raw.strip().replace(chr(36), '', 1))
        if not value.is_finite() or not (0 < value <= 1000) or value.as_tuple().exponent < -4:
            raise ValueError()
    except (ValueError, InvalidOperation):
        await target.answer('Enter a valid USD price, e.g. 0.90 (up to 4 decimal places).')
        return
    item, _ = await shop.quote(service, country)
    from .business import Q
    from decimal import ROUND_UP
    floor = (item.cost * settings.provider_price_to_wallet).quantize(Q, rounding=ROUND_UP)
    if value < floor:
        await target.answer(f'Price too low. Minimum allowed: {floor} USD. Send a higher price.')
        return
    value = value.quantize(Q)
    async with Session.begin() as s:
        key = f'price:{service}:{country}'
        row = await s.get(Setting, key)
        if row:
            row.value = str(value)
        else:
            s.add(Setting(key=key, value=str(value)))
        audit(s, uid, 'price_set', f'{service}:{country}:{value}')
    await clear_flow(uid)
    await show_selection_menu(
        target,
        f'PUBLIC PRICE SAVED!\n{service.title()} / Country #{country}: {value} USD\n'
        'Buyers see only your selling price, which is debited from their wallet at purchase.',
        keyboard([
            [('Change Another Price', 'ps:services:0')],
            [('Reset This Price', f'ps:reset:{service}:{country}')],
            [('Admin Settings', 'admin:settings')],
        ]),
    )


async def show_balance(target, uid, *, edit: bool = False):
    user = await shop.get_user(uid)
    await show_selection_menu(
        target, f'Your balance: {user.balance} {settings.wallet_currency}',
        main_menu_for(uid), edit=edit,
    )


async def show_orders(target, uid, *, edit: bool = False, state='all'):
    async with Session() as s:
        query = select(Activation).where(Activation.user_id == uid)
        if state == 'active':
            query = query.where(Activation.status.in_(('pending', 'ordering', 'received', 'manual_review')))
        elif state == 'completed':
            query = query.where(Activation.status == 'completed')
        elif state == 'refunded':
            query = query.where(Activation.status.in_(('cancelled', 'expired', 'failed')),
                                Activation.id.in_(select(Activation.id).join(LedgerEntry,
                                    (LedgerEntry.user_id == uid) & (LedgerEntry.ref_type == 'activation') &
                                    (LedgerEntry.ref_id == cast(Activation.id, String)) &
                                    (LedgerEntry.type == 'refund'))))
        orders = (await s.scalars(query.order_by(Activation.id.desc()).limit(10))).all()
    lines = []
    for row in orders:
        number = row.phone if row.status in ('pending', 'ordering', 'received') else (
            '••••' + row.phone[-4:] if row.phone else 'hidden')
        lines.append(f'#{row.id} • App: {app_label_html(row.service)} / '
                     f'{escape(str(row.country))}\n'
                     f'{escape(row.status)} • {escape(number)} • '
                     f'{row.price_charged} {settings.wallet_currency} • /check {row.id}')
    await show_selection_menu(
        target, '\n'.join(lines) if lines else 'No orders yet.',
        keyboard([[ (f'Order #{row.id} · {row.service.title()}', f'ui:order:{row.id}') ] for row in orders]
                 + [[('All', 'ui:filterorders:all'), ('Active', 'ui:filterorders:active')],
                    [('Completed', 'ui:filterorders:completed'), ('Refunded', 'ui:filterorders:refunded')],
                    [('⬅️ Main menu', 'menu:home')]]), edit=edit,
        parse_mode='HTML',
    )


@router.message(Command('pricing', 'prices', 'setprice', 'changeprice', ignore_case=True))
async def admin_pricing_command(message: Message):
    """Manage public prices directly inside Telegram, without any Mini App."""
    uid = message.from_user.id
    if uid not in settings.admins:
        return await message.answer('Admin only')
    if message.chat.type != 'private':
        return await message.answer('Use /setprice in your private Friday chat.')
    await admin_price_services(message)


@router.message(Command('buy'))
async def buy_command(message: Message):
    if await allowed(message.from_user.id, message):
        await services(message)


@router.message(Command('balance'))
async def balance_command(message: Message):
    if await allowed(message.from_user.id, message):
        await show_balance(message, message.from_user.id)


async def check_sms(target, user_id: int, activation_id: int, *, edit: bool = False) -> None:
    """Owner-only SMS lookup; inline refresh edits the receipt instead of spamming chat."""
    async def respond(text: str, *, active: bool = False, confirm: bool = False) -> None:
        if not edit:
            if confirm:
                await send_branded_message(target, text, reply_markup=keyboard([[('✅ Verification Done', f'done:{activation_id}')]]))
            else:
                await send_branded_message(target, text)
            return
        rows = []
        if confirm:
            rows.append([('✅ Verification Done', f'done:{activation_id}')])
        if active:
            rows += [[('🔄 Check SMS now', f'check:{activation_id}')],
                     [('❌ Cancel order', f'cancel:{activation_id}')]]
        rows.append([('⬅️ Main menu', 'menu:home')])
        await show_selection_menu(target, text, keyboard(rows), edit=True, track=False,
                                  parse_mode='HTML')

    if activation_id <= 0:
        await respond('Invalid order number.')
        return
    async with Session() as s:
        row = await s.get(Activation, activation_id)
        if row is None or row.user_id != user_id:
            await respond('Order not found.')
            return
        status = row.status
        phone = row.phone
        service = row.service
    if status in ('pending', 'received'):
        from .workers import Worker
        try:
            await Worker(shop).poll_order(activation_id)
        except Exception:
            await respond(
                f'Order #{activation_id}\nApp: {app_label_html(service)}\n'
                f'Number: {escape(str(phone or "not assigned"))}\n'
                'Provider status temporarily unavailable. Your order is still saved.',
                active=True,
            )
            return
    async with Session() as s:
        row = await s.get(Activation, activation_id)
        if row is None or row.user_id != user_id:
            await respond('Order not found.')
            return
        status = row.status
        expires = row.expires_at
        code = row.code
        phone = row.phone
        service = row.service
    if code and status in ('received', 'completed'):
        await respond(f'🔐 Code for order #{activation_id}: {escape(code)}\n'
                      f'App: {app_label_html(service)}\nStatus: {escape(status)}'
                      + (f'\nNumber: {escape(str(phone))}' if phone else ''), confirm=True)
    elif status == 'pending':
        await respond(
            f'Order #{activation_id}\nApp: {app_label_html(service)}\n'
            f'Number: {escape(str(phone or "not assigned"))}\n'
            f'Time remaining: {remaining_time(expires)}.\n'
            'Waiting for an SMS at the provider. '
            'If the sender has not sent a code, Friday cannot create one. '
            'Use Check SMS to refresh, or cancel a pending order if unused.',
            active=True,
        )
    elif status == 'manual_review':
        await respond('This order requires support review. Your reserved funds are protected.')
    else:
        await respond(f'Order #{activation_id}: {status}. Check /balance for any confirmed refund.')

@router.message(Command('check'))
async def check_command(message: Message):
    if not await allowed(message.from_user.id, message):
        return
    parts = (message.text or '').split()
    if len(parts) != 2 or not parts[1].isdigit():
        await order_picker(message, message.from_user.id, 'check')
        return
    await check_sms(message, message.from_user.id, int(parts[1]))


@router.message(Command('orders'))
async def orders_command(message: Message):
    if await allowed(message.from_user.id, message):
        await show_orders(message, message.from_user.id)


async def send_topup_invoice(target, uid: int, raw: str) -> bool:
    try:
        amount = Decimal(raw.strip())
        if not amount.is_finite():
            raise InvalidOperation()
        # Shop enforces $5 minimum before rounding for EVERY user.
        invoice = await shop.start_payment(uid, amount)
    except (ValueError, InvalidOperation):
        await target.answer(f'Enter a valid amount in USD. Minimum top-up: ${settings.min_topup}.')
        return False
    except BotError as exc:
        await target.answer(str(exc))
        return False
    await clear_flow(uid)
    await target.answer(
        f'💳 Invoice for ${invoice["amount"]} USD. '
        'Pay using the official link below; your wallet credits after confirmation.',
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=('Pay via NOWPayments'
                if settings.payment_provider == 'nowpayments' else 'Pay via Crypto Bot'),
                url=invoice['url']),
        ]]),
    )
    # Payment invoice is a record; retire only the stale interactive top-up prompt.
    await discard_active_menu(target)
    return True


async def prompt_topup(target, uid: int, *, edit: bool = False, crypto_only=False):
    if hasattr(target, 'chat') and target.chat.type != 'private':
        return await target.answer('Open Friday in a private chat to top up.')
    cfg = await bank.config(Session)
    if not crypto_only:
        await clear_flow(uid)
        status = '' if bank.ready(cfg) else '\nBank transfer is not yet enabled; configuration is required.'
        return await show_selection_menu(target, 'Choose a deposit method:' + status,
            keyboard([[('🇳🇬 Nigerian bank transfer', 'ui:bankstart')],
                      [('🪙 Cryptocurrency', 'ui:bankcrypto')],
                      [('My bank claims', 'ui:bankclaims')], [('⬅️ Main menu', 'menu:home')]]), edit=edit)
    await save_flow(uid, {'type': 'topup'})
    await show_selection_menu(
        target,
        f'💳 Enter the amount you want to top up (USD).\n'
        f'✅ Minimum deposit: ${settings.min_topup}\n'
        f'❌ Amounts below ${settings.min_topup} will be rejected.\n\n'
        'Type an amount such as 5, 10, or 20 and send it.',
        keyboard([[('⬅️ Cancel / Main menu', 'menu:home')]]),
        edit=edit,
    )


@router.message(Command('topup', ignore_case=True))
async def topup_command(message: Message):
    uid = message.from_user.id
    if not await allowed(uid, message):
        return
    await prompt_topup(message, uid)


def admin_only(message: Message) -> bool:
    return message.from_user.id in settings.admins


@router.message(Command('setupcheck'))
async def admin_setupcheck(message: Message):
    """Admin-only, non-billing credential check; never return API keys."""
    if not admin_only(message):
        return await message.answer('Admin only')
    lines = ['🔎 SMS Bot production readiness',
             f'Provider: {settings.provider_name}',
             f'Wallet currency: {settings.wallet_currency}',
             f'Price conversion configured: {"YES" if settings.provider_price_to_wallet > 0 else "NO"}',
             f'SMS purchasing enabled: {"YES" if settings.live_purchases_enabled else "NO"}',
             f'Payment acceptance enabled: {"YES" if settings.payments_enabled else "NO"}',
             f'Payment gateway: {settings.payment_provider}']
    if not settings.provider_credentials_ready:
        lines.append(f'{settings.provider_name.title()} API: credentials missing')
    else:
        try:
            balance = await shop.provider.get_balance()
            lines.append(f'{settings.provider_name.title()} API: connected (provider balance {balance} raw units)')
        except Exception:
            lines.append(f'{settings.provider_name.title()} API: connection or authorization failed')
    if settings.provider_name == 'textverified':
        lines.append('TextVerified SMS webhook: ' +
                     ('configured' if settings.textverified_webhook_secret else
                      'NOT CONFIGURED (SMS polling still works)'))
    if settings.payment_provider == 'nowpayments':
        if not settings.nowpayments_api_key:
            lines.append('NOWPayments API: key missing')
        else:
            try:
                await shop.gateway.check_credentials()
                lines.append('NOWPayments API: connected')
            except Exception:
                lines.append('NOWPayments API: connection or authorization failed')
        lines.append('NOWPayments IPN secret: ' +
                     ('configured' if settings.nowpayments_ipn_secret else 'missing'))
        lines.append('Public callback URL: ' +
                     ('configured' if settings.public_base_url.startswith('https://') else 'missing'))
    elif not settings.payment_api_key:
        lines.append('Crypto Pay API: key missing')
    else:
        try:
            await shop.gateway._call('getMe', {})
            lines.append('Crypto Pay API: connected')
        except Exception:
            lines.append('Crypto Pay API: connection or authorization failed')
    if settings.provider_price_to_wallet <= 0:
        lines.append('Next: verify the provider price unit and conversion rate independently.')
    lines.append('This check did not purchase a number or create an invoice.')
    await message.answer('\n'.join(lines))


@router.message(Command('testall'))
async def admin_testall(message: Message):
    """Safe read-only production diagnostics. Never creates paid orders."""
    if not admin_only(message):
        return await message.answer('Admin only')
    lines = ['🧪 Friday system check (read-only)',
             'Telegram messaging: OK',
             f'Provider: {settings.provider_name}',
             f'Payment gateway: {settings.payment_provider}']
    try:
        async with Session() as s:
            users = await s.scalar(select(func.count()).select_from(User))
            orders = await s.scalar(select(func.count()).select_from(Activation))
            from .db import LedgerEntry, Payment
            invoices = await s.scalar(select(func.count()).select_from(Payment))
            paid = await s.scalar(select(func.count()).select_from(Payment)
                                  .where(Payment.status == 'paid'))
            mismatches = await s.scalar(select(func.count()).select_from(User).where(
                User.balance != (
                    select(func.coalesce(func.sum(LedgerEntry.amount), 0))
                    .where(LedgerEntry.user_id == User.id).scalar_subquery())))
        lines.append(f'Database: OK • {users} users, {orders} orders, {invoices} invoices')
        lines.append(f'Credited invoices: {paid}')
        lines.append('Wallet ledger: ' +
                     ('OK' if mismatches == 0 else f'REVIEW {mismatches} inconsistent wallets'))
    except Exception:
        lines.append('Database / ledger: FAILED — inspect Render logs')
    if settings.provider_credentials_ready:
        try:
            balance = await shop.provider.get_balance()
            prices = await shop.provider.get_prices()
            lines.append(f'{settings.provider_name}: OK • balance {balance}, {len(prices)} stocked offers')
        except Exception:
            lines.append(f'{settings.provider_name} API: FAILED (balance or stock lookup)')
    else:
        lines.append(f'{settings.provider_name} API: NOT CONFIGURED')
    if settings.payment_provider == 'nowpayments':
        if not settings.nowpayments_api_key:
            lines.append('NOWPayments API: MISSING')
        else:
            try:
                await shop.gateway.check_credentials()
                lines.append('NOWPayments API: OK')
            except Exception:
                lines.append('NOWPayments API: FAILED')
        lines.append('IPN secret: ' + ('SET' if settings.nowpayments_ipn_secret else 'MISSING'))
    else:
        lines.append('Payment provider: Crypto Bot (NOWPayments inactive)')
    lines.append('Payments enabled: ' + ('YES' if settings.payment_ready else 'NO'))
    lines.append('New top-ups: ' + ('ADMIN TEST ONLY' if settings.payment_test_admin_only else 'OPEN TO USERS'))
    lines.append('SMS buying enabled: ' + ('YES' if settings.live_ready else 'NO'))
    lines.append('⚠️ Provider payments, incoming IPNs, SMS arrival and refunds require'
                 ' separate real-world tests. No money moved by this check.')
    await message.answer('\n'.join(lines))


@router.message(Command('reconcilepayment'))
async def admin_reconcile_nowpayment(message: Message):
    """Recover a verified finished top-up when an IPN was lost or rejected."""
    if not admin_only(message):
        return await message.answer('Admin only')
    if settings.payment_provider != 'nowpayments':
        return await message.answer('NOWPayments is not selected')
    try:
        parts = message.text.split()
        if len(parts) != 2 or not parts[1].isascii() or not parts[1].isdigit() or len(parts[1]) > 40:
            return await prompt_button_input(message, message.from_user.id, 'reconcilepayment')
        payment_id = parts[1]
        remote = await shop.gateway.get_payment_status(payment_id)
        status = str(remote.get('payment_status', 'unknown')).lower()
        if status != 'finished':
            return await message.answer(f'NOWPayments payment status: {status}. Wallet unchanged.')
        order_id = remote.get('order_id')
        if not isinstance(order_id, str) or not order_id.startswith('friday-topup-'):
            return await message.answer('Payment does not match a Friday invoice. Wallet unchanged.')
        invoice_id = remote.get('invoice_id')
        invoice_id = str(invoice_id) if invoice_id is not None else None
        credited = await shop.verify_and_credit_nowpayments(
            invoice_id, payment_id, order_id=order_id)
        async with Session.begin() as s:
            audit(s, message.from_user.id, 'reconcile_payment', f'{payment_id}:{credited}')
        await message.answer(
            'Payment confirmed and wallet credited once.' if credited
            else 'Payment already credited or payment details failed strict matching. Check /testall.')
    except Exception:
        # Never log credentials or raw payment details to the Telegram conversation.
        await message.answer('Unable to reconcile payment securely. Check Render error logs.')


@router.message(Command('adjust'))
async def admin_adjust(message: Message):
    if not admin_only(message):
        return await message.answer('Admin only')
    if len(message.text.split()) == 1:
        return await customer_picker(message, message.from_user.id)
    try:
        _, uid, raw, reason = message.text.split(maxsplit=3)
        uid, amount = int(uid), Decimal(raw)
        if not amount.is_finite() or amount == 0 or not reason.strip():
            raise ValueError()
        async with Session.begin() as s:
            if not await s.get(User, uid):
                raise BotError('User has not started the bot')
            from uuid import uuid4
            ref = uuid4().hex
            await apply(s, uid, amount, 'admin_adjust', 'manual', ref, reason)
            audit(s, message.from_user.id, 'wallet_adjust', f'{uid}:{ref}')
        await message.answer('Balance adjustment applied and recorded in ledger.')
    except (ValueError, InvalidOperation, InsufficientFunds):
        await message.answer('Usage: /adjust USER_ID AMOUNT reason (negative for debit).')
    except BotError as exc:
        await message.answer(str(exc))


@router.message(Command('ban', 'unban'))
async def admin_ban(message: Message):
    if not admin_only(message):
        return await message.answer('Admin only')
    try:
        uid = int(message.text.split()[1])
    except (IndexError, ValueError):
        return await customer_picker(message, message.from_user.id)
    async with Session.begin() as s:
        user = await s.get(User, uid, with_for_update=True)
        if not user:
            return await message.answer('User not found')
        user.is_banned = message.text.split()[0].lower() == '/ban'
        audit(s, message.from_user.id, 'ban' if user.is_banned else 'unban', uid)
    await message.answer('Account status updated.')


@router.message(Command('markup'))
async def admin_markup(message: Message):
    if not admin_only(message):
        return await message.answer('Admin only')
    if len(message.text.split()) == 1:
        return await markup_picker(message)
    try:
        percent = Decimal(message.text.split()[1])
        if not percent.is_finite() or not 0 <= percent <= 500:
            raise ValueError()
    except (IndexError, ValueError, InvalidOperation):
        return await message.answer('Usage: /markup PERCENT (0–500)')
    async with Session.begin() as s:
        row = await s.get(Setting, 'markup_percent')
        if row:
            row.value = str(percent)
        else:
            s.add(Setting(key='markup_percent', value=str(percent)))
        audit(s, message.from_user.id, 'markup', str(percent))
    await message.answer(f'Markup set to {percent}%')


@router.message(Command('stats'))
async def admin_stats(message: Message):
    if not admin_only(message):
        return await message.answer('Admin only')
    async with Session() as s:
        users = await s.scalar(select(func.count()).select_from(User))
        total = await s.scalar(select(func.count()).select_from(Activation))
        success = await s.scalar(select(func.count()).select_from(Activation)
                                 .where(Activation.status == 'completed'))
        revenue = await s.scalar(select(func.coalesce(func.sum(Activation.price_charged), 0))
                                 .where(Activation.status == 'completed'))
        costs = await s.scalar(select(func.coalesce(func.sum(Activation.cost_provider), 0))
                               .where(Activation.status == 'completed'))
    converted = Decimal(str(costs)) * settings.provider_price_to_wallet
    try:
        provider_balance = str(await shop.provider.get_balance())
    except Exception:
        provider_balance = 'unavailable'
    await message.answer(f'Users: {users}\nOrders: {total}\nCompleted: {success}\n'
                         f'Success: {(100*success/total if total else 0):.1f}%\n'
                         f'Revenue: {revenue} {settings.wallet_currency}\n'
                         f'Estimated provider cost: {converted} {settings.wallet_currency}\n'
                         f'Estimated profit: {Decimal(str(revenue))-converted} {settings.wallet_currency}\n'
                         f'Provider balance (raw): {provider_balance}')


@router.message(Command('broadcast'))
async def admin_broadcast(message: Message):
    if not admin_only(message):
        return await message.answer('Admin only')
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        return await prompt_button_input(message, message.from_user.id, 'broadcast')
    if len(parts[1]) > 1000:
        return await message.answer('Maximum broadcast length is 1000 characters.')
    async with Session() as s:
        ids = (await s.scalars(select(User.id).where(User.is_banned.is_(False), User.accepted_terms.is_(True)))).all()
    delivered, failed = 0, 0
    for uid in ids:
        try:
            await shop.notifier.send(uid, parts[1])
            delivered += 1
        except Exception:
            failed += 1
        await asyncio.sleep(.045)
    async with Session.begin() as s:
        audit(s, message.from_user.id, 'broadcast', f'delivered={delivered};failed={failed}')
    await message.answer(f'Broadcast finished: delivered {delivered}, failed {failed}')


@router.message(Command('resolve'))
async def admin_resolve(message: Message):
    """Operator-confirmed manual refund; use only after investigating provider order."""
    if not admin_only(message):
        return await message.answer('Admin only')
    if len(message.text.split()) == 1:
        return await review_picker(message)
    try:
        _, raw_id, action = message.text.split(maxsplit=2)
        aid = int(raw_id)
        if action.lower() != 'refund':
            raise ValueError()
        async with Session() as s:
            row = await s.get(Activation, aid)
            if not row or row.status != 'manual_review':
                return await message.answer('Order is not under manual review')
        if await shop.refund(aid, 'failed'):
            async with Session.begin() as s:
                audit(s, message.from_user.id, 'manual_refund', aid)
            await message.answer(f'Order #{aid} refunded; confirm external provider allocation separately.')
            await shop.alert_admins(f'Manual order #{aid} refund authorized by admin {message.from_user.id}.')
        else:
            await message.answer('Order was already resolved')
    except (IndexError, ValueError):
        await message.answer('Usage: /resolve ORDER_ID refund (manual review only)')

@router.message(Command('cancel', ignore_case=True))
async def cancel_flow_command(message: Message):
    await clear_flow(message.from_user.id)
    await message.answer('Cancelled. Use /start to open the menu.')


@router.message(Command('clear', ignore_case=True))
async def clear_chat_command(message: Message):
    if message.chat.type != 'private':
        return await message.answer('Chat cleanup is available only in a private chat.')
    await show_selection_menu(
        message,
        '🧹 Clear Friday chat?\n'
        'This deletes recent Telegram messages from this private chat, '
        'including messages containing verification codes.\n'
        'Telegram bots cannot delete messages older than 48 hours or guarantee '
        'removal of the entire history. Your wallet and orders are NOT deleted.\n'
        'For a complete history wipe, use Telegram > Clear History.',
        keyboard([
            [('🗑️ Yes, clear recent messages', 'clear:confirm')],
            [('Cancel', 'clear:cancel')],
        ]),
    )


async def clear_chat_confirm(target, uid: int):
    if not hasattr(target, 'chat') or target.chat.type != 'private' or target.chat.id != uid:
        await target.answer('Chat cleanup is only available in your own private chat.')
        return
    # Telegram's deleteMessages API accepts at most 100 IDs at a time,
    # and messages over 48h old cannot be deleted. Scan up to 500 recent IDs.
    last = target.message_id
    errors = 0
    for _ in range(5):
        if last <= 0:
            break
        first = max(1, last - 99)
        try:
            await target.bot.delete_messages(
                chat_id=uid, message_ids=list(range(first, last + 1)))
        except Exception:
            errors += 1
        last = first - 1
    await clear_flow(uid)
    if errors:
        await target.answer(
            '🧹 Tried clearing recent messages. Some older or protected messages '
            'could not be removed. For the entire chat, use Telegram > Clear History.'
        )
    # If all delete requests succeeded, leave the chat empty.



def remaining_time(expires):
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    seconds = max(0, int((expires - utcnow()).total_seconds()))
    return f'{seconds // 60}m {seconds % 60}s'


async def private_customer(message):
    return (message.chat.type == 'private' and
            await allowed(message.from_user.id, message))


async def private_admin(message):
    if message.chat.type != 'private' or not admin_only(message):
        await message.answer('Admin only in private chat.')
        return False
    return True


@router.message(Command('history', 'refunds'))
async def transaction_history(message: Message):
    if not await private_customer(message):
        return
    uid = message.from_user.id
    refunds = message.text.split()[0].split('@')[0].lower() == '/refunds'
    async with Session() as s:
        query = select(LedgerEntry).where(LedgerEntry.user_id == uid)
        if refunds:
            query = query.where(LedgerEntry.type == 'refund')
        rows = (await s.scalars(query.order_by(LedgerEntry.id.desc()).limit(15))).all()
    lines = ['Confirmed refunds' if refunds else 'Recent wallet transactions']
    for row in rows:
        lines.append(f'#{row.id} • {row.created_at:%Y-%m-%d} • {row.type} '
                     f'{row.amount:+.4f} {settings.wallet_currency}')
    if not rows:
        lines.append('No recorded transactions.')
    if refunds:
        lines.append('Pending cancellations are not refunds until confirmed. See /orders.')
    await message.answer('\n'.join(lines))


@router.message(Command('receipt'))
async def order_receipt(message: Message):
    if not await private_customer(message):
        return
    parts = message.text.split()
    if len(parts) != 2 or not parts[1].isdigit() or len(parts[1]) > 9:
        return await order_picker(message, message.from_user.id, 'receipt')
    async with Session() as s:
        row = await s.get(Activation, int(parts[1]))
        if row is None or row.user_id != message.from_user.id:
            return await message.answer('Order not found.')
    from aiogram.types import BufferedInputFile
    body = (f'FRIDAY order record #{row.id}\n'
            f'Service: {row.service}\nStatus: {row.status}\n'
            f'Created: {row.created_at:%Y-%m-%d %H:%M} UTC\n'
            f'Original charge: {row.price_charged} {settings.wallet_currency}\n'
            'For confirmed refunds see /refunds. This is an order record, not a tax invoice.\n')
    await message.answer_document(BufferedInputFile(body.encode(), filename=f'order-{row.id}.txt'))


@router.message(Command('account'))
async def customer_account(message: Message):
    if not await private_customer(message):
        return
    uid = message.from_user.id
    async with Session() as s:
        user = await s.get(User, uid)
        counts = (await s.execute(select(Activation.status, func.count())
                                  .where(Activation.user_id == uid)
                                  .group_by(Activation.status))).all()
    if user is None:
        return
    await message.answer(f'Your account\nBalance: {user.balance} {settings.wallet_currency}\n'
                         + '\n'.join(f'{state}: {count}' for state, count in counts)
                         + '\nUse /history for wallet activity.')


@router.message(Command('done'))
async def customer_done(message: Message):
    if not await private_customer(message):
        return
    parts = message.text.split()
    if len(parts) != 2 or not parts[1].isdigit() or len(parts[1]) > 9:
        return await order_picker(message, message.from_user.id, 'done')
    await mark_customer_done(message, message.from_user.id, int(parts[1]))


async def mark_customer_done(message, uid: int, aid: int):
    async with Session.begin() as s:
        row = await s.get(Activation, aid, with_for_update=True)
        if row is None or row.user_id != uid:
            return await message.answer('Order not found.')
        if row.status not in ('received', 'completed') or not row.sms_notified:
            return await message.answer('No delivered SMS recorded for this order.')
        key = f'customer_done:{aid}'
        if await s.get(Setting, key) is None:
            s.add(Setting(key=key, value=utcnow().isoformat()))
    await message.answer(f'✅ Order #{aid}: verification marked done by you. '
                         'This is a customer confirmation, not confirmation from the destination app.')


@router.message(Command('support'))
async def customer_support(message: Message):
    if not await private_customer(message):
        return
    parts = message.text.split()
    if len(parts) != 2 or not parts[1].isdigit() or len(parts[1]) > 9:
        return await order_picker(message, message.from_user.id, 'support')
    aid, uid = int(parts[1]), message.from_user.id
    created = False
    async with Session.begin() as s:
        row = await s.get(Activation, aid, with_for_update=True)
        if row is None or row.user_id != uid:
            return await message.answer('Order not found.')
        key = f'support_order:{aid}'
        if await s.get(Setting, key) is None:
            s.add(Setting(key=key, value=utcnow().isoformat()))
            created = True
    if created:
        try:
            await shop.alert_admins(f'Customer requested support review for order #{aid}. Use /tickets.')
        except Exception:
            log.warning('Support notification failed; ticket remains stored.')
    await message.answer(f'Support request for order #{aid} recorded. Do not share your verification code.',
                         reply_markup=keyboard([[('✏️ Describe issue', f'ui:supportwrite:{aid}'),
                                                 ('📜 View replies', f'ui:thread:{aid}')]]))


@router.message(Command('tickets'))
async def admin_tickets(message: Message):
    if not await private_admin(message):
        return
    await ticket_picker(message)


@router.message(Command('closeticket'))
async def admin_close_ticket(message: Message):
    if not await private_admin(message):
        return
    parts = message.text.split()
    if len(parts) != 2 or not parts[1].isdigit() or len(parts[1]) > 9:
        return await ticket_picker(message)
    async with Session.begin() as s:
        order = await s.get(Activation, int(parts[1]), with_for_update=True)
        ticket = await s.get(Setting, f'support_order:{int(parts[1])}')
        if order is None or ticket is None:
            return await message.answer('Ticket not found.')
        await s.delete(ticket)
        audit(s, message.from_user.id, 'close_ticket', order.id)
        uid = order.user_id
    try:
        await shop.notifier.send(uid, f'Support review for order #{parts[1]} was closed. Use /orders for its status.')
    except Exception:
        log.warning('Support closure notification failed.')
    await message.answer('Support ticket closed. No wallet or order state was changed.')


@router.message(Command('maintenance'))
async def admin_maintenance(message: Message):
    if not await private_admin(message):
        return
    parts = message.text.lower().split()
    if len(parts) != 2 or parts[1] not in ('on', 'off'):
        return await maintenance_picker(message)
    await set_maintenance(parts[1], actor=message.from_user.id)
    await maintenance_picker(message)


async def set_maintenance(value, actor=None):
    return await manual_mode(Session, value, actor=actor)


@router.message(Command('ack'))
async def admin_ack(message: Message):
    if not await private_admin(message):
        return
    if await acknowledge(Session):
        await message.answer('✅ Outage acknowledged. Automatic pause is cancelled for this incident. Monitoring continues.')
    else:
        await message.answer('No current TextVerified outage to acknowledge.')


async def maintenance_picker(target, *, edit=False):
    async with Session() as s:
        row = await s.get(Setting, 'purchase_maintenance')
    state = row.value if row else 'off'
    await show_selection_menu(
        target, f'🛠 Purchase maintenance: {state.upper()}\n'
        'Existing orders and refunds remain available. Customers are notified when the mode changes.',
        keyboard([[('🔴 Maintenance ON', 'ui:maint:on'), ('🟢 Maintenance OFF', 'ui:maint:off')],
                  [('⬅️ Admin Settings', 'admin:settings')]]), edit=edit)


@router.message(Command('dailyreport'))
async def admin_dailyreport(message: Message):
    if not await private_admin(message):
        return
    start = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    async with Session() as s:
        orders = (await s.execute(select(Activation.status, func.count())
                                  .where(Activation.created_at >= start)
                                  .group_by(Activation.status))).all()
        ledger = (await s.execute(select(LedgerEntry.type, func.sum(LedgerEntry.amount))
                                  .where(LedgerEntry.created_at >= start)
                                  .group_by(LedgerEntry.type))).all()
    await message.answer(f'Daily report (UTC) {start:%Y-%m-%d}\nOrders created today:\n'
                         + ('\n'.join(f'{state}: {count}' for state, count in orders) or 'None')
                         + '\nWallet activity today:\n'
                         + ('\n'.join(f'{kind}: {amount:+.4f} {settings.wallet_currency}'
                                      for kind, amount in ledger) or 'None')
                         + '\nWallet flows are not net profit.')


@router.message(Command('customer'))
async def admin_customer(message: Message):
    if not await private_admin(message):
        return
    parts = message.text.split()
    if len(parts) != 2 or not parts[1].isdigit() or len(parts[1]) > 16:
        return await customer_picker(message, message.from_user.id)
    await customer_card(message, int(parts[1]))



class ButtonRequest:
    """Reuse existing command handlers with the real callback actor and chat."""
    def __init__(self, target, actor, text):
        self.target = target
        self.from_user = actor
        self.chat = target.chat
        self.text = text

    def __getattr__(self, name):
        return getattr(self.target, name)


async def order_picker(target, uid, action, *, edit=False):
    async with Session() as s:
        query = select(Activation).where(Activation.user_id == uid)
        if action == 'done':
            query = query.where(Activation.status.in_(('received', 'completed')),
                                Activation.sms_notified.is_(True))
        orders = (await s.scalars(query.order_by(Activation.id.desc()).limit(10))).all()
    rows = [[(f'#{row.id} · {row.service.title()} · {row.status}', f'ui:{action}:{row.id}')]
            for row in orders]
    rows.append([('⬅️ Main menu', 'menu:home')])
    await show_selection_menu(target, 'Select an order:' if orders else 'No matching orders.',
                              keyboard(rows), edit=edit)


async def customer_picker(target, uid, *, page=0, edit=False):
    if uid not in settings.admins:
        return await target.answer('Admin only')
    page = max(0, min(page, 10000))
    async with Session() as s:
        users = (await s.scalars(select(User).order_by(User.id).offset(page * 8).limit(9))).all()
    rows = [[(f'{("@" + row.username) if row.username else "Customer"} · {row.id}',
              f'ui:customer:{row.id}')] for row in users[:8]]
    nav = []
    if page:
        nav.append(('⬅️ Previous', f'ui:customers:{page - 1}'))
    if len(users) > 8:
        nav.append(('Next ➡️', f'ui:customers:{page + 1}'))
    if nav:
        rows.append(nav)
    rows.append([('⬅️ Admin Settings', 'admin:settings')])
    await show_selection_menu(target, '👥 Select a customer:' if users else 'No customers on this page.',
                              keyboard(rows), edit=edit)


async def customer_card(target, customer_id, *, edit=False):
    async with Session() as s:
        user = await s.get(User, customer_id)
        count = await s.scalar(select(func.count()).select_from(Activation)
                               .where(Activation.user_id == customer_id))
    if not user:
        return await target.answer('Customer not found.')
    await show_selection_menu(
        target, f'Customer {user.id}\nOrders: {count}\nBalance: {user.balance} {settings.wallet_currency}\n'
        f'Restricted: {"Yes" if user.is_banned else "No"}',
        keyboard([[('🔓 Unban' if user.is_banned else '🔒 Ban',
                    f'ui:{"unban" if user.is_banned else "ban"}:{user.id}')],
                  [('💰 Adjust balance', f'ui:adjust:{user.id}')],
                  [('⬅️ Customers', 'ui:customers')]]), edit=edit)


async def markup_picker(target, *, edit=False):
    async with Session() as s:
        row = await s.get(Setting, 'markup_percent')
    current = row.value if row else settings.markup_percent
    await show_selection_menu(target, f'📈 Current markup: {current}%\nChoose a new markup:',
                              keyboard([[('0%', 'ui:percent:0'), ('25%', 'ui:percent:25')],
                                        [('50%', 'ui:percent:50'), ('100%', 'ui:percent:100')],
                                        [('✏️ Custom percentage', 'ui:input:markup')],
                                        [('⬅️ Admin Settings', 'admin:settings')]]), edit=edit)


async def ticket_picker(target, *, edit=False):
    async with Session() as s:
        rows = (await s.scalars(select(Setting).where(Setting.key.startswith('support_order:'))
                                .order_by(Setting.value.desc()).limit(20))).all()
    buttons = [[(f'Open ticket #{row.key.split(":")[1]}', f'ui:thread:{row.key.split(":")[1]}')]
               for row in rows]
    await show_selection_menu(target, '🎧 Open support tickets' if rows else 'No open tickets.',
                              keyboard(buttons + [[('⬅️ Admin Settings', 'admin:settings')]]), edit=edit)


async def review_picker(target, *, edit=False):
    async with Session() as s:
        rows = (await s.scalars(select(Activation).where(Activation.status == 'manual_review')
                                .order_by(Activation.id.desc()).limit(20))).all()
    buttons = [[(f'Review #{row.id} · {row.service.title()}', f'ui:revieworder:{row.id}')]
               for row in rows]
    await show_selection_menu(target, 'Review provider allocation independently before any refund.'
                              if rows else 'No orders under manual review.',
                              keyboard(buttons + [[('⬅️ Admin Settings', 'admin:settings')]]), edit=edit)


async def prompt_button_input(target, uid, action, *, customer_id=None, edit=False):
    if uid not in settings.admins:
        return await target.answer('Admin only')
    prompts = {
        'markup': 'Enter only the new markup percentage (0–500).',
        'reconcilepayment': 'Enter the NOWPayments payment ID only. Friday will verify it before crediting.',
        'broadcast': 'Enter the announcement text (up to 1000 characters). Do not include private data.',
        'adjust': 'Enter the signed amount only (positive to credit, negative to debit).',
    }
    if action not in prompts:
        return await target.answer('Invalid action.')
    await save_flow(uid, {'type': 'button_input', 'action': action, 'customer_id': customer_id})
    await show_selection_menu(target, prompts[action],
                              keyboard([[('Cancel', 'ui:cancel')]]), edit=edit)


async def stage_confirmation(target, uid, action, value, description, *, edit=False):
    nonce = token_hex(8)
    await save_flow(uid, {'type': 'button_confirm', 'action': action,
                          'value': value, 'nonce': nonce})
    await show_selection_menu(target, 'Confirm action:\n' + description,
                              keyboard([[('✅ Confirm', f'ui:confirm:{nonce}'), ('❌ Cancel', 'ui:cancel')]]),
                              edit=edit)


async def consume_confirmation(uid, nonce):
    async with Session.begin() as s:
        row = await s.get(Setting, flow_key(uid), with_for_update=True)
        if row is None:
            return None
        try:
            flow = json.loads(row.value)
            if (flow.get('type') != 'button_confirm' or flow.get('nonce') != nonce
                    or int(flow.get('expires', 0)) < int(time.time())):
                return None
        except (ValueError, TypeError, AttributeError):
            return None
        await s.delete(row)
        return flow


async def handle_button_input(message, uid, flow, raw):
    action = flow.get('action')
    if action in ('markup', 'adjust'):
        try:
            amount = Decimal(raw)
            if not amount.is_finite() or len(raw) > 18:
                raise ValueError()
            if action == 'markup' and not 0 <= amount <= 500:
                raise ValueError()
            if action == 'adjust' and (amount == 0 or abs(amount) > Decimal('1000000')):
                raise ValueError()
        except (ValueError, InvalidOperation):
            return await message.answer('Invalid amount. Enter a valid number, or press Cancel.')
        if action == 'adjust':
            await save_flow(uid, {'type': 'button_input', 'action': 'adjust_reason',
                                  'customer_id': flow['customer_id'], 'amount': str(amount)})
            return await show_selection_menu(message, 'Enter a short reason for the wallet adjustment.',
                                             keyboard([[('Cancel', 'ui:cancel')]]))
        return await stage_confirmation(message, uid, 'markup', str(amount), f'Set markup to {amount}%.')
    if action == 'adjust_reason':
        if not 1 <= len(raw) <= 240:
            return await message.answer('Enter a reason of 1–240 characters.')
        value = f'{flow["customer_id"]} {flow["amount"]} {raw}'
        return await stage_confirmation(message, uid, 'adjust', value,
                                        f'Customer {flow["customer_id"]}\nAmount: {flow["amount"]}\nReason: {raw}')
    if action == 'reconcilepayment':
        if not raw.isascii() or not raw.isdigit() or len(raw) > 40:
            return await message.answer('Enter a valid numeric NOWPayments payment ID.')
        return await stage_confirmation(message, uid, action, raw, f'Verify payment {raw} and reconcile if eligible.')
    if action == 'broadcast':
        if len(raw) > 1000:
            return await message.answer('Maximum announcement length is 1000 characters.')
        return await stage_confirmation(message, uid, action, raw, 'Send this announcement to customers:\n' + raw)


async def button_action(callback, uid, action):
    target, actor = callback.message, callback.from_user
    parts = action.split(':')
    name = parts[0]
    public = {'history', 'refunds', 'account', 'support', 'receipt', 'done', 'check', 'order', 'clear',
              'deposits', 'depositreceipt', 'filterorders', 'thread', 'supportwrite', 'opcancel',
              'bankstart', 'bankcrypto', 'bankclaims', 'bankmethod'}
    if name not in public and uid not in settings.admins:
        return await target.answer('Admin only')
    if name == 'ackoutage' and len(parts) == 2:
        if await acknowledge(Session, parts[1]):
            return await target.answer('✅ Outage acknowledged. Monitoring continues. Any existing maintenance remains unchanged.')
        return await target.answer('This outage alert is no longer current.')
    if name == 'confirm' and len(parts) == 2:
        flow = await consume_confirmation(uid, parts[1])
        if not flow:
            return await target.answer('Confirmation expired or already used.')
        command = flow['action']
        handlers = {'markup': admin_markup, 'adjust': admin_adjust, 'broadcast': admin_broadcast,
                    'reconcilepayment': admin_reconcile_nowpayment, 'ban': admin_ban,
                    'unban': admin_ban, 'closeticket': admin_close_ticket, 'resolve': admin_resolve}
        if command in handlers:
            return await handlers[command](ButtonRequest(target, actor, f'/{command} {flow["value"]}'))
        return
    if name == 'bankfinal':
        flow = await read_flow(uid)
        await clear_flow(uid)
        if not flow or flow.get('type') != 'bank_approve':
            return await target.answer('Confirmation expired.')
        try:
            await bank.approve(Session, uid, int(flow['claim']), flow['event'])
            return await target.answer('Confirmed bank deposit credited once.')
        except ValueError as exc:
            return await target.answer(str(exc))
    await clear_flow(uid)
    if await bank_button(target, uid, parts):
        return
    if await operations_button(target, uid, parts):
        return
    if name == 'cancel':
        return await show_selection_menu(target, 'Cancelled. Admin Settings', admin_settings_keyboard(), edit=True)
    if name == 'maintenance':
        return await maintenance_picker(target, edit=True)
    if name == 'maint' and len(parts) == 2 and parts[1] in ('on', 'off'):
        await set_maintenance(parts[1], actor=uid)
        return await maintenance_picker(target, edit=True)
    if name == 'customers':
        return await customer_picker(target, uid, page=int(parts[1]) if len(parts) == 2 else 0, edit=True)
    if name == 'customer' and len(parts) == 2:
        return await customer_card(target, int(parts[1]), edit=True)
    if name == 'markup':
        return await markup_picker(target, edit=True)
    if name == 'percent' and len(parts) == 2 and parts[1] in ('0', '25', '50', '100'):
        return await stage_confirmation(target, uid, 'markup', parts[1], f'Set markup to {parts[1]}%.', edit=True)
    if name == 'tickets':
        return await ticket_picker(target, edit=True)
    if name == 'review':
        return await review_picker(target, edit=True)
    if name == 'revieworder' and len(parts) == 2:
        aid = int(parts[1])
        async with Session() as s:
            row = await s.get(Activation, aid)
        if not row or row.status != 'manual_review':
            return await target.answer('Order is not under manual review.')
        return await show_selection_menu(target, f'Order #{aid} · {row.service}\n'
                                         f'Wallet charge: {row.price_charged}\n'
                                         'Confirm external provider allocation independently before authorizing a refund.',
                                         keyboard([[('Authorize refund', f'ui:resolve:{aid}')],
                                                   [('⬅️ Review orders', 'ui:review')]]), edit=True)
    if name in ('ban', 'unban', 'closeticket', 'resolve') and len(parts) == 2:
        value = str(int(parts[1]))
        if name == 'resolve':
            value += ' refund'
        return await stage_confirmation(target, uid, name, value, f'{name.title()} {value}?', edit=True)
    if name == 'adjust' and len(parts) == 2:
        return await prompt_button_input(target, uid, 'adjust', customer_id=int(parts[1]), edit=True)
    if name in ('broadcast', 'reconcilepayment'):
        return await prompt_button_input(target, uid, name, edit=True)
    if name == 'input' and parts == ['input', 'markup']:
        return await prompt_button_input(target, uid, 'markup', edit=True)
    if name in ('support', 'receipt', 'done', 'check'):
        if len(parts) == 1:
            return await order_picker(target, uid, name, edit=True)
        if len(parts) == 2:
            aid = int(parts[1])
            if name == 'check':
                return await check_sms(target, uid, aid, edit=True)
            if name == 'done':
                return await mark_customer_done(target, uid, aid)
            handler = customer_support if name == 'support' else order_receipt
            return await handler(ButtonRequest(target, actor, f'/{name} {aid}'))
    if name == 'order' and len(parts) == 2:
        aid = int(parts[1])
        async with Session() as s:
            row = await s.get(Activation, aid)
        if not row or row.user_id != uid:
            return await target.answer('Order not found.')
        rows = [[('🔄 Check status', f'ui:check:{aid}'), ('🧾 Receipt', f'ui:receipt:{aid}')],
                [('🎧 Support', f'ui:support:{aid}')]]
        if row.sms_notified and row.status in ('received', 'completed'):
            rows.append([('✅ Verification Done', f'ui:done:{aid}')])
        if row.status == 'pending':
            rows.append([('❌ Cancel order', f'cancel:{aid}')])
        rows.append([('⬅️ My orders', 'orders')])
        return await show_selection_menu(target, f'Order #{aid} · {row.service.title()}\nStatus: {row.status}',
                                         keyboard(rows), edit=True)
    read_handlers = {'history': transaction_history, 'refunds': transaction_history, 'account': customer_account,
                     'stats': admin_stats, 'dailyreport': admin_dailyreport, 'testall': admin_testall,
                     'setupcheck': admin_setupcheck, 'clear': clear_chat_command}
    if name in read_handlers and len(parts) == 1:
        return await read_handlers[name](ButtonRequest(target, actor, f'/{name}'))
    await target.answer('Invalid selection.')



async def deposits_card(target, uid):
    async with Session() as s:
        rows = (await s.scalars(select(Payment).where(Payment.user_id == uid)
                                .order_by(Payment.id.desc()).limit(15))).all()
    buttons = [[(f'#{row.id} · {row.amount_fiat} {settings.wallet_currency} · {row.status}',
                 f'ui:depositreceipt:{row.id}')] for row in rows]
    await show_selection_menu(target, '💳 Your deposits — select an invoice for its status or receipt.'
                              if rows else 'No deposits yet.',
                              keyboard(buttons + [[('⬅️ Main menu', 'menu:home')]]), edit=True)


async def deposit_receipt(target, uid, pid):
    async with Session() as s:
        row = await s.get(Payment, pid)
        if not row or row.user_id != uid:
            return await target.answer('Deposit not found.')
    if row.status != 'paid':
        return await target.answer(f'Deposit #{row.id}: {row.status}. No confirmed receipt yet.')
    from aiogram.types import BufferedInputFile
    body = (f'FRIDAY deposit receipt #{row.id}\nAmount credited: {row.amount_credit} '
            f'{settings.wallet_currency}\nStatus: paid\nGateway: {row.gateway}\n'
            f'Paid: {row.paid_at:%Y-%m-%d %H:%M} UTC\n' if row.paid_at else
            f'FRIDAY deposit receipt #{row.id}\nAmount credited: {row.amount_credit} '
            f'{settings.wallet_currency}\nStatus: paid\nGateway: {row.gateway}\n')
    await target.answer_document(BufferedInputFile(body.encode(), filename=f'deposit-{pid}.txt'))


async def support_thread(target, uid, aid):
    async with Session() as s:
        order = await s.get(Activation, aid)
        if not order or (order.user_id != uid and uid not in settings.admins):
            return await target.answer('Order not found.')
        ticket = await s.get(Setting, f'support_order:{aid}')
        rows = (await s.scalars(select(SupportMessage).where(SupportMessage.order_id == aid)
                                .order_by(SupportMessage.id.desc()).limit(8))).all()
    lines = [f'🎧 Support ticket #{aid} · {"Open" if ticket else "Closed"}']
    lines += [f'{"Support" if row.from_admin else "Customer"}: {row.body[:300]}'
              for row in reversed(rows)]
    if not rows:
        lines.append('No messages yet.')
    buttons = []
    if ticket:
        buttons.append([('✏️ Reply', f'ui:supportwrite:{aid}')])
        if uid in settings.admins:
            buttons.append([('Close ticket', f'ui:closeticket:{aid}')])
    buttons.append([('⬅️ Back', 'ui:tickets' if uid in settings.admins else 'orders')])
    await show_selection_menu(target, '\n'.join(lines), keyboard(buttons), edit=True)



async def bank_admin_card(target, edit=True):
    cfg = await bank.config(Session)
    await show_selection_menu(target,
        '🏦 Direct bank transfers\n'
        f'Account name: {cfg.get("name", "not set")}\n'
        f'Account number: {cfg.get("account", "not set")}\n'
        f'NGN per USD: {cfg.get("rate", "not set")}\n'
        f'Minimum bank deposit: ₦{bank.MIN_NAIRA:,}\n'
        f'Customer deposits: {"enabled" if bank.ready(cfg) else "disabled"}\n'
        'Authenticated event matching + administrator confirmation of settled incoming payment.',
        keyboard([[('Set account name', 'ui:bankset:name'), ('Set account number', 'ui:bankset:account')],
                  [('Exchange rate', 'ui:bankset:rate')],
                  [('Enable', 'ui:banktoggle:on'), ('Disable', 'ui:banktoggle:off')],
                  [('Review deposit claims', 'ui:bankreview')],
                  [('⬅️ Admin Settings', 'admin:settings')]]), edit=edit)


async def bank_review_card(target, claim_id):
    async with Session() as s:
        row = await s.get(BankDeposit, claim_id)
    if not row:
        return await target.answer('Claim not found.')
    matches = await bank.candidates(Session, claim_id) if row.status == 'pending' else []
    text = (f'Bank claim #{row.id}\nCustomer: {row.user_id}\n'
            f'Amount: NGN {Decimal(row.amount_kobo)/100:.2f}\n'
            f'Wallet credit: USD {row.credit}\n'
            f'Matching method: {row.method}\nStatus: {row.status}\n'
            f'Authenticated candidate transactions: {len(matches)}\n'
            'A matching POS event does not prove funds entered your account. Check the incoming settled credit, account and customer in Moniepoint.')
    buttons = []
    if row.status == 'pending':
        if len(matches) == 1:
            text += f'\nTransaction fingerprint: {matches[0].transaction_key[:12]}'
            buttons.append([('Confirm settled incoming transfer', f'ui:bankconfirm:{row.id}')])
        buttons.append([('Reject claim', f'ui:bankreject:{row.id}')])
    buttons.append([('⬅️ Review claims', 'ui:bankreview')])
    await show_selection_menu(target, text, keyboard(buttons), edit=True)


async def bank_button(target, uid, parts):
    name = parts[0]
    if name == 'bankcrypto':
        await prompt_topup(target, uid, edit=True, crypto_only=True)
    elif name == 'bankstart':
        cfg = await bank.config(Session)
        if not bank.ready(cfg):
            if uid in settings.admins:
                await bank_admin_card(target)
            else:
                await show_selection_menu(target, 'Bank transfers are currently unavailable. Choose cryptocurrency or contact support.',
                    keyboard([[('🪙 Cryptocurrency', 'ui:bankcrypto')], [('⬅️ Main menu', 'menu:home')]]), edit=True)
        else:
            await save_flow(uid, {'type': 'bank_input', 'action': 'amount'})
            await show_selection_menu(target,
                f'🏦 Bank Transfer — Nigeria\n\n'
                f'Bank: Moniepoint\nMinimum deposit: ₦{bank.MIN_NAIRA:,}\n'
                f'Exchange rate: ₦{Decimal(cfg["rate"]):,.2f} = $1 USD\n\n'
                'Enter your deposit amount in naira to continue.',
                keyboard([[('⬅️ Back to payment methods', 'topup')]]), edit=True)
    elif name == 'bankmethod' and len(parts) == 3 and parts[1] in ('id', 'name', 'session'):
        # The quote is persisted separately from button flow, which is cleared on navigation.
        async with Session() as s:
            quote = await s.get(Setting, f'bankquote:{uid}')
        if not quote:
            await target.answer('Open Bank transfer again.')
        else:
            q = json.loads(quote.value)
            if q['nonce'] != parts[2] or q['expires'] < time.time():
                await target.answer('Quote expired. Open Bank transfer again.')
            else:
                await save_flow(uid, {**q, 'type': 'bank_input', 'action': 'claim', 'method': 'name'})
                await show_selection_menu(target,
                    '✅ Payment verification\n\nEnter the full name on the bank account you used to send the money.\n\nFriday will check your name, amount and transaction time.',
                    keyboard([[('⬅️ Cancel', 'ui:opcancel')]]), edit=True)
    elif name == 'bankclaims':
        async with Session() as s:
            rows = (await s.scalars(select(BankDeposit).where(BankDeposit.user_id == uid)
                                    .order_by(BankDeposit.id.desc()).limit(10))).all()
        lines = []
        for r in rows:
            state = r.status
            if state == 'pending' and len(await bank.candidates(Session, r.id)) == 1:
                state = 'matched — awaiting settlement review'
            lines.append(f'#{r.id}: NGN {Decimal(r.amount_kobo)/100:.2f} · {state}')
        body = 'Your bank deposit claims\n' + ('\n'.join(lines) or 'No claims yet.')
        await show_selection_menu(target, body, keyboard([[('⬅️ Main menu', 'menu:home')]]), edit=True)
    elif uid not in settings.admins:
        return False
    elif name == 'bankadmin':
        await bank_admin_card(target)
    elif name == 'bankset' and len(parts)==2 and parts[1] in ('name','account','rate'):
        await save_flow(uid, {'type':'bank_input','action':'config','field':parts[1]})
        await show_selection_menu(target, {'name':'Enter bank account name.', 'account':'Enter 10-digit Moniepoint account number.',
                                          'rate':'Enter NGN per USD exchange rate.'}[parts[1]],
                                  keyboard([[('Cancel','ui:opcancel')]]), edit=True)
    elif name == 'banktoggle' and len(parts)==2 and parts[1] in ('on','off'):
        try:
            await bank.update_config(Session,uid,'enabled',parts[1]=='on')
            await bank_admin_card(target)
        except ValueError as exc:
            await target.answer(str(exc))
    elif name == 'bankreview':
        async with Session() as s:
            rows = (await s.scalars(select(BankDeposit).where(BankDeposit.status=='pending')
                                    .order_by(BankDeposit.id).limit(20))).all()
        await show_selection_menu(target, 'Pending bank claims' if rows else 'No pending claims.',
            keyboard([[(f'#{r.id} · NGN {Decimal(r.amount_kobo)/100:.2f}', f'ui:bankview:{r.id}')] for r in rows]
                      + [[('⬅️ Bank transfers','ui:bankadmin')]]), edit=True)
    elif name == 'bankview' and len(parts)==2:
        await bank_review_card(target,int(parts[1]))
    elif name == 'bankconfirm' and len(parts)==2:
        claim_id=int(parts[1])
        matches=await bank.candidates(Session,claim_id)
        if len(matches)!=1:
            await target.answer('No unique authenticated match.')
        else:
            await save_flow(uid, {'type':'bank_approve','claim':claim_id,'event':matches[0].event_key})
            await show_selection_menu(target,
                'Only confirm if you independently checked the actual incoming settled credit in Moniepoint, and confirmed that this customer made the transfer. Never approve withdrawals or outgoing transfers.',
                keyboard([[('I verified the settled incoming payment','ui:bankfinal'),('Cancel','ui:opcancel')]]),edit=True)
    elif name == 'bankreject' and len(parts)==2:
        try:
            await bank.reject(Session,uid,int(parts[1]))
            await target.answer('Claim rejected. No wallet credited.')
        except ValueError as exc:
            await target.answer(str(exc))
    else:
        return False
    return True


async def bank_input(message,uid,flow,raw):
    try:
        action=flow.get('action')
        if action=='config':
            field = flow['field']
            await bank.update_config(Session,uid,field,raw)
            cfg = await bank.config(Session)
            next_field = next((key for key in ('name', 'account', 'rate') if not cfg.get(key)), None)
            if next_field:
                await save_flow(uid, {'type':'bank_input','action':'config','field':next_field})
                prompt = {'name':'Enter bank account name.',
                          'account':'Enter your 10-digit Moniepoint account number.',
                          'rate':'Enter the exchange rate in NGN per USD.'}[next_field]
                return await show_selection_menu(message, '✅ Saved. Next: ' + prompt,
                    keyboard([[('Cancel / Bank settings','ui:bankadmin')]]))
            await clear_flow(uid)
            return await show_selection_menu(message, '✅ Bank details saved. Review them and tap Enable to start accepting transfers.',
                keyboard([[('Review bank settings','ui:bankadmin')],
                          [('Enable bank transfers','ui:banktoggle:on')]]))
        if not await private_customer(message):
            return
        cfg=await bank.config(Session)
        if not bank.ready(cfg):
            raise ValueError('Bank deposits are unavailable.')
        if action=='amount':
            kobo=bank.amount_kobo(raw)
            remark = bank.new_remark()
            claim = await bank.create_claim(Session, uid, kobo, cfg['rate'], 'remark', remark)
            q={'kobo':kobo,'rate':cfg['rate'],'nonce':token_hex(6),'expires':time.time()+86400,
               'account':cfg['account'],'name':cfg['name'], 'remark_claim':claim}
            async with Session.begin() as s:
                row=await s.get(Setting,f'bankquote:{uid}',with_for_update=True)
                if row:
                    row.value=json.dumps(q)
                else:
                    s.add(Setting(key=f'bankquote:{uid}',value=json.dumps(q)))
            await clear_flow(uid)
            return await show_selection_menu(message,
                f'🏦 Bank Transfer Details\n\nBank: Moniepoint\nAccount name: {cfg["name"]}\nAccount number: {cfg["account"]}\n'
                f'Amount to transfer: ₦{Decimal(kobo)/100:,.2f}\nRate: ₦{Decimal(cfg["rate"]):,.2f} / USD\n\n'
                f'Payment reference: #{claim}\nOptional transfer remark: {remark}\n\n'
                'After sending the exact amount, tap I Have Paid and enter the full sender name.',
                InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text='📋 Copy account number', copy_text=CopyTextButton(text=cfg['account']))],
                    [InlineKeyboardButton(text='📋 Copy transfer remark', copy_text=CopyTextButton(text=remark))],
                    [InlineKeyboardButton(text='✅ I Have Paid', callback_data=f'ui:bankmethod:name:{q["nonce"]}')],
                    [InlineKeyboardButton(text='⬅️ Cancel', callback_data='ui:opcancel')]
                ]))
        if action=='claim':
            if flow['expires']<time.time() or cfg['account']!=flow['account'] or cfg['name']!=flow['name']:
                raise ValueError('Bank details or quote changed. Open a new quote and contact support if already paid.')
            if not flow.get('remark_claim'):
                raise ValueError('Open your bank deposit request again.')
            cid = await bank.submit_sender_name(Session, uid, flow['remark_claim'], raw)
            await clear_flow(uid)
            matches=await bank.candidates(Session,cid)
            return await message.answer(f'🏦 Deposit #{cid}\n\n'
                + ('✅ Matching bank transaction found. Awaiting settlement confirmation.' if len(matches)==1 else
                   '⏳ Verification pending. No unique matching bank transaction yet. Please do not transfer again.'),
                reply_markup=keyboard([[('🔄 Check deposit status','ui:bankclaims')]]))
    except ValueError as exc:
        await message.answer(str(exc))


async def operations_button(target, uid, parts):
    name = parts[0]
    if name == 'deposits':
        await deposits_card(target, uid)
    elif name == 'depositreceipt' and len(parts) == 2:
        await deposit_receipt(target, uid, int(parts[1]))
    elif name == 'filterorders' and len(parts) == 2 and parts[1] in ('all', 'active', 'completed', 'refunded'):
        await show_orders(target, uid, edit=True, state=parts[1])
    elif name == 'thread' and len(parts) == 2:
        await support_thread(target, uid, int(parts[1]))
    elif name == 'supportwrite' and len(parts) == 2:
        aid = int(parts[1])
        async with Session() as s:
            order = await s.get(Activation, aid)
            ticket = await s.get(Setting, f'support_order:{aid}')
        if not order or (order.user_id != uid and uid not in settings.admins) or not ticket:
            await target.answer('Ticket not available.')
        else:
            await save_flow(uid, {'type': 'ops_input', 'action': 'support', 'order': aid})
            await show_selection_menu(target, 'Type your support message (up to 500 characters). Never include OTPs, passwords or full phone numbers.',
                                      keyboard([[('Cancel', 'ui:opcancel')]]), edit=True)
    elif name == 'opcancel':
        await show_selection_menu(target, 'Cancelled.', main_menu_for(uid), edit=True)
    elif uid not in settings.admins:
        return False
    elif name == 'reports':
        await show_selection_menu(target, '📊 Business reports — select a period.',
                                  keyboard([[('Today', 'ui:report:1'), ('Last 7 days', 'ui:report:7')],
                                            [('Last 30 days', 'ui:report:30')],
                                            [('⬅️ Admin Settings', 'admin:settings')]]), edit=True)
    elif name == 'report' and len(parts) == 2 and parts[1] in ('1', '7', '30'):
        end = utcnow()
        start = end.astimezone(LAGOS).replace(hour=0, minute=0, second=0, microsecond=0)
        start -= timedelta(days=int(parts[1])-1)
        await show_selection_menu(target, await report(Session, start, end),
                                  keyboard([[('⬅️ Reports', 'ui:reports')]]), edit=True)
    elif name == 'risk':
        await show_selection_menu(target, await risk_summary(Session),
                                  keyboard([[('⬅️ Admin Settings', 'admin:settings')]]), edit=True)
    elif name == 'audit':
        async with Session() as s:
            rows = (await s.scalars(select(AdminAudit).order_by(AdminAudit.id.desc()).limit(15))).all()
        body = '📜 Recent admin activity\n' + ('\n'.join(
            f'{row.created_at:%m-%d %H:%M} UTC · {row.actor_id} · {row.action} · {row.reference}'
            for row in rows) or 'No records since this feature was added.')
        await show_selection_menu(target, body, keyboard([[('⬅️ Admin Settings', 'admin:settings')]]), edit=True)
    elif name == 'searchcustomer':
        await save_flow(uid, {'type': 'ops_input', 'action': 'search'})
        await show_selection_menu(target, 'Enter a Telegram username or numeric customer ID.',
                                  keyboard([[('Cancel', 'ui:opcancel')]]), edit=True)
    elif name == 'lowthreshold':
        async with Session() as s:
            row = await s.get(Setting, 'provider_low_balance_threshold')
        threshold = row.value if row else settings.provider_low_balance_alert
        await save_flow(uid, {'type': 'ops_input', 'action': 'threshold'})
        await show_selection_menu(target, f'Current low balance alert: {threshold} provider units.\nEnter a new nonnegative threshold (USD for TextVerified).',
                                  keyboard([[('Cancel', 'ui:opcancel')]]), edit=True)
    elif name == 'moniepoint':
        await show_selection_menu(target, await pilot_status(Session),
                                  keyboard([[('Refresh', 'ui:moniepoint'), ('⬅️ Admin Settings', 'admin:settings')]]), edit=True)
    elif name == 'health':
        async with Session() as s:
            row = await s.get(Setting, 'textverified_auto_state')
            state = json.loads(row.value) if row else {}
            pause = await s.get(Setting, 'purchase_maintenance')
            gateway = await s.get(Setting, 'ops_gateway_health')
        stamp = state.get('last_sample')
        age = max(0, int(utcnow().timestamp()-stamp)) if stamp else None
        tv = ('stale / unknown' if age is None or age > 120 else
              'recovering' if state.get('healthy_since') else
              'outage' if state.get('incident') else 'healthy')
        await show_selection_menu(target, f'📡 System health\nDatabase: responding\n'
                                  f'TextVerified: {tv}\nLast sample: {str(age)+"s ago" if age is not None else "none"}\n'
                                  f'Payment gateway: {gateway.value if gateway else "not sampled"}\n'
                                  f'Purchase maintenance: {pause.value if pause else "off"}\n'
                                  f'Process uptime: {int((time.monotonic()-STARTED_AT)//60)} min\n'
                                  'Monitoring is available while the host is running.',
                                  keyboard([[('Refresh', 'ui:health'), ('⬅️ Admin Settings', 'admin:settings')]]), edit=True)
    else:
        return False
    return True


async def operations_input(message, uid, flow, raw):
    action = flow.get('action')
    if action == 'support':
        if not await private_customer(message):
            return
        if len(raw) > 500:
            return await message.answer('Please use at most 500 characters.')
        aid = int(flow['order'])
        async with Session.begin() as s:
            order = await s.get(Activation, aid, with_for_update=True)
            ticket = await s.get(Setting, f'support_order:{aid}')
            if not order or not ticket or (order.user_id != uid and uid not in settings.admins):
                return await message.answer('Ticket not available.')
            # Administrators can reply to other customers; their own orders remain customer messages.
            from_admin = uid in settings.admins and order.user_id != uid
            row = SupportMessage(order_id=aid, actor_id=uid, from_admin=from_admin, body=raw)
            s.add(row)
            await s.flush()
            recipients = [order.user_id] if from_admin else settings.admins
            for recipient in recipients:
                await notice(s, f'support:{row.id}:{recipient}', recipient,
                             f'New support message on order #{aid}. Open the ticket in Friday to read it.')
            if from_admin:
                audit(s, uid, 'support_reply', aid)
        await clear_flow(uid)
        return await message.answer('Message saved. The recipient will be notified.',
                                    reply_markup=keyboard([[('View ticket', f'ui:thread:{aid}')]]))
    if uid not in settings.admins:
        await clear_flow(uid)
        return
    if action == 'search':
        query = raw.lstrip('@')
        async with Session() as s:
            if query.isascii() and query.isdigit() and len(query) <= 16:
                rows = (await s.scalars(select(User).where(User.id == int(query)))).all()
            else:
                if not query or len(query) > 32 or not all(ch.isascii() and (ch.isalnum() or ch == '_') for ch in query):
                    return await message.answer('Enter a valid Telegram username or numeric ID.')
                rows = (await s.scalars(select(User).where(func.lower(User.username) == query.lower()).limit(15))).all()
        await clear_flow(uid)
        return await show_selection_menu(message, 'Customer results:' if rows else 'No matching customer.',
            keyboard([[(f'{row.username or "Customer"} · {row.id}', f'ui:customer:{row.id}')] for row in rows]
                     + [[('Search again', 'ui:searchcustomer'), ('⬅️ Admin Settings', 'admin:settings')]]))
    if action == 'threshold':
        try:
            amount = Decimal(raw)
            if not amount.is_finite() or not 0 <= amount <= 1000000 or len(raw) > 16:
                raise ValueError()
        except (ValueError, InvalidOperation):
            return await message.answer('Enter a valid nonnegative threshold.')
        async with Session.begin() as s:
            row = await s.get(Setting, 'provider_low_balance_threshold', with_for_update=True)
            if row:
                row.value = str(amount)
            else:
                s.add(Setting(key='provider_low_balance_threshold', value=str(amount)))
            audit(s, uid, 'low_balance_threshold', str(amount))
        await clear_flow(uid)
        await message.answer(f'Low balance threshold saved: {amount}.')


@router.message(Command('servicemode'))
async def service_mode_command(message: Message):
    if not await private_admin(message):
        return
    parts = message.text.split()
    if len(parts) != 3 or not SERVICE.fullmatch(parts[1]) or not settings.service_allowed(parts[1]) or parts[2] not in ('on', 'off'):
        return await message.answer('Usage: /servicemode SERVICE on/off (on pauses new purchases)')
    await set_service_pause(Session, message.from_user.id, parts[1], parts[2])
    await message.answer(f'Service {parts[1]} maintenance: {parts[2]}. Existing orders unchanged.')


@router.message(Command('setpromo'))
async def promo_config_command(message: Message):
    if not await private_admin(message):
        return
    parts = message.text.split()
    try:
        if len(parts) != 3 or len(parts[2]) > 8:
            raise ValueError()
        await set_promo(Session, message.from_user.id, parts[1].upper(), Decimal(parts[2]))
    except (ValueError, InvalidOperation):
        return await message.answer('Usage: /setpromo CODE PERCENT (0-100% of markup only; 0 disables)')
    await message.answer('Promotion saved. Discount applies only to the margin, never to provider cost.')


@router.message(Command('promo'))
async def promo_command(message: Message):
    if not await private_customer(message):
        return
    parts = message.text.split()
    if len(parts) != 2 or not PROMO.fullmatch(parts[1].upper()):
        return await message.answer('Usage: /promo CODE')
    if message.from_user.id in settings.admins:
        return await message.answer('Admin already pays the original provider price.')
    if await redeem_promo(Session, message.from_user.id, parts[1].upper()):
        return await message.answer('Promotion applied to eligible future purchases while active.')
    await message.answer('Promotion unavailable.')


@router.message(Command('stockalert', 'stockoff'))
async def stock_alert_command(message: Message):
    if not await private_customer(message):
        return
    uid = message.from_user.id
    parts = message.text.split()
    if parts[0].split('@')[0] == '/stockoff':
        async with Session.begin() as s:
            rows = (await s.scalars(select(Setting).where(Setting.key.startswith(f'watch:{uid}:')))).all()
            for row in rows:
                await s.delete(row)
        return await message.answer('Stock alerts disabled.')
    if len(parts) != 3 or not settings.service_allowed(parts[1]) or not settings.country_allowed(parts[2]):
        return await message.answer('Usage: /stockalert SERVICE COUNTRY (country code, e.g. us)')
    try:
        saved = await watch_stock(Session, uid, parts[1], parts[2])
    except ValueError:
        saved = False
    await message.answer('One-time stock alert saved. /stockoff cancels alerts.' if saved else 'Alert unavailable or maximum 5 active alerts reached.')


@router.message(Command('referral'))
async def referral_command(message: Message):
    if not await private_customer(message):
        return
    uid = message.from_user.id
    bot = await message.bot.get_me()
    count = await referral_count(Session, uid)
    await message.answer(f'Your referral link: https://t.me/{bot.username}?start=ref_{uid}\n'
                         f'Referrals recorded: {count}\\nNo automatic wallet reward is enabled.')


@router.message(Command('reportnumber'))
async def report_number_command(message: Message):
    if not await private_customer(message):
        return
    parts = message.text.split()
    if len(parts) != 2 or not parts[1].isdigit() or len(parts[1]) > 9:
        return await message.answer('Usage: /reportnumber ORDER_ID. Do not include phone numbers or codes.')
    aid, uid = int(parts[1]), message.from_user.id
    async with Session.begin() as s:
        row = await s.get(Activation, aid, with_for_update=True)
        if not row or row.user_id != uid:
            return await message.answer('Order not found.')
        key = f'support_order:{aid}'
        if await s.get(Setting, key) is None:
            s.add(Setting(key=key, value=utcnow().isoformat()))
        report_key = f'number_report:{aid}'
        if await s.get(Setting, report_key) is None:
            s.add(Setting(key=report_key, value=utcnow().isoformat()))
            for admin in settings.admins:
                await notice(s, f'numberreport:{aid}:{admin}', admin,
                             f'Customer reported a rejected number for order #{aid}. Review through /tickets.')
    await message.answer(f'Report recorded for order #{aid}. Refunds require confirmed eligibility.',
                         reply_markup=keyboard([[('View ticket', f'ui:thread:{aid}')]]))


@router.message(F.text)
async def handle_pending_text(message: Message):
    # Only input explicitly requested by this bot is interpreted as an amount.
    # Do not process random texts, group messages, or bot commands as payment input.
    if message.chat.type != 'private' or not message.from_user:
        return
    raw = (message.text or '').strip()
    if not raw or raw.startswith('/'):
        return
    uid = message.from_user.id
    reply = getattr(message, 'reply_to_message', None)
    if uid in settings.admins and reply and getattr(reply, 'from_user', None):
        original = reply.text or ''
        bot = getattr(message, 'bot', None)
        if bot and reply.from_user.id == bot.id and original.startswith('🚨 FRIDAY: TextVerified outage'):
            marker = next((line[10:] for line in original.splitlines() if line.startswith('Incident: ')), '')
            if marker and await acknowledge(Session, marker):
                return await message.answer('✅ Outage acknowledged. Monitoring continues.')
    flow = await read_flow(uid)
    if not flow:
        return
    if flow.get('type') == 'bank_input':
        return await bank_input(message, uid, flow, raw)
    if flow.get('type') == 'ops_input':
        return await operations_input(message, uid, flow, raw)
    if flow.get('type') == 'button_input':
        if uid not in settings.admins:
            await clear_flow(uid)
            return
        await handle_button_input(message, uid, flow, raw)
    elif flow.get('type') == 'topup':
        if await allowed(uid, message):
            await send_topup_invoice(message, uid, raw)
    elif flow.get('type') == 'price':
        if uid not in settings.admins:
            await clear_flow(uid)
            return await message.answer('Admin only')
        try:
            await admin_price_save(message, uid, flow, raw)
        except BotError as exc:
            await message.answer(str(exc))
