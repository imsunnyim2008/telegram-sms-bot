"""Inline-keyboard Telegram UI. No financial operations live inside callbacks."""
import asyncio
import json
import time
from decimal import Decimal, InvalidOperation
from html import escape

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import func, select

from .business import BotError
from .config import settings
from .country_ui import arrange_country_offers, country_flag
from .command_menu import PUBLIC_HELP, ADMIN_HELP
from .db import Activation, Session, Setting, User
from .ledger import InsufficientFunds, apply
from .service_ui import (app_label_html, custom_emoji_unavailable,
                         without_custom_emoji)

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
    [('❓ Help', 'help')],
])

MAIN_ADMIN = keyboard([
    [('📱 Buy number', 'services:0'), ('💰 Balance', 'balance')],
    [('💳 Top up', 'topup'), ('📋 My orders', 'orders')],
    [('❓ Help', 'help')],
    [('⚙️ Admin Settings', 'admin:settings')],
])


def main_menu_for(uid: int) -> InlineKeyboardMarkup:
    return MAIN_ADMIN if uid in settings.admins else MAIN


def admin_settings_keyboard() -> InlineKeyboardMarkup:
    return keyboard([
        [('💵 Change Public Prices', 'ps:services:0')],
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
    if (getattr(event, 'text', '') or '').startswith('/'):
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
        if data.startswith('services:'):
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
                                            edit=True)
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
                await show_selection_menu(callback.message, ADMIN_HELP,
                                          admin_settings_keyboard(), edit=True)
        elif data == 'admin:home':
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
    except Exception:
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
    arranged = arrange_country_offers(items, country_names)
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


async def admin_price_reset(target, service: str, country: str, *, edit: bool = False):
    if not settings.service_allowed(service) or not settings.country_allowed(country):
        raise BotError('Invalid price selection')
    async with Session.begin() as s:
        row = await s.get(Setting, f'price:{service}:{country}')
        if row:
            await s.delete(row)
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


async def show_orders(target, uid, *, edit: bool = False):
    async with Session() as s:
        orders = (await s.scalars(select(Activation).where(Activation.user_id == uid)
                                  .order_by(Activation.id.desc()).limit(10))).all()
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
        keyboard([[('⬅️ Main menu', 'menu:home')]]), edit=edit,
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
    async def respond(text: str, *, active: bool = False) -> None:
        if not edit:
            await send_branded_message(target, text)
            return
        rows = []
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
        code = row.code
        phone = row.phone
        service = row.service
    if code and status in ('received', 'completed'):
        await respond(f'🔐 Code for order #{activation_id}: {escape(code)}\n'
                      f'App: {app_label_html(service)}\nStatus: {escape(status)}'
                      + (f'\nNumber: {escape(str(phone))}' if phone else ''))
    elif status == 'pending':
        await respond(
            f'Order #{activation_id}\nApp: {app_label_html(service)}\n'
            f'Number: {escape(str(phone or "not assigned"))}\n'
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
        await message.answer('Usage: /check ORDER_ID (see /orders)')
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


async def prompt_topup(target, uid: int, *, edit: bool = False):
    if hasattr(target, 'chat') and target.chat.type != 'private':
        return await target.answer('Open Friday in a private chat to top up.')
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
    parts = (message.text or '').split(maxsplit=1)
    if len(parts) == 1:
        await prompt_topup(message, uid)
    else:
        await send_topup_invoice(message, uid, parts[1])


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
    if not settings.provider_api_key:
        lines.append(f'{settings.provider_name.title()} API: key missing')
    else:
        try:
            balance = await shop.provider.get_balance()
            lines.append(f'{settings.provider_name.title()} API: connected (provider balance {balance} raw units)')
        except Exception:
            lines.append(f'{settings.provider_name.title()} API: connection or authorization failed')
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
    if settings.provider_api_key:
        try:
            balance = await shop.provider.get_balance()
            prices = await shop.provider.get_prices()
            lines.append(f'Grizzly: OK • balance {balance}, {len(prices)} stocked offers')
        except Exception:
            lines.append('Grizzly API: FAILED (balance or stock lookup)')
    else:
        lines.append('Grizzly API: NOT CONFIGURED')
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
            return await message.answer('Usage: /reconcilepayment PAYMENT_ID')
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
    try:
        _, uid, raw, reason = message.text.split(maxsplit=3)
        uid, amount = int(uid), Decimal(raw)
        if not amount.is_finite() or amount == 0 or not reason.strip():
            raise ValueError()
        async with Session.begin() as s:
            if not await s.get(User, uid):
                raise BotError('User has not started the bot')
            from uuid import uuid4
            await apply(s, uid, amount, 'admin_adjust', 'manual', uuid4().hex, reason)
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
        return await message.answer('Usage: /ban USER_ID or /unban USER_ID')
    async with Session.begin() as s:
        user = await s.get(User, uid, with_for_update=True)
        if not user:
            return await message.answer('User not found')
        user.is_banned = message.text.split()[0].lower() == '/ban'
    await message.answer('Account status updated.')


@router.message(Command('markup'))
async def admin_markup(message: Message):
    if not admin_only(message):
        return await message.answer('Admin only')
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
    if len(parts) < 2 or len(parts[1]) > 1000:
        return await message.answer('Usage: /broadcast TEXT (max 1000 characters)')
    async with Session() as s:
        ids = (await s.scalars(select(User.id).where(User.is_banned.is_(False)))).all()
    delivered, failed = 0, 0
    for uid in ids:
        try:
            await shop.notifier.send(uid, parts[1])
            delivered += 1
        except Exception:
            failed += 1
        await asyncio.sleep(.045)
    await message.answer(f'Broadcast finished: delivered {delivered}, failed {failed}')


@router.message(Command('resolve'))
async def admin_resolve(message: Message):
    """Operator-confirmed manual refund; use only after investigating provider order."""
    if not admin_only(message):
        return await message.answer('Admin only')
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
    flow = await read_flow(uid)
    if not flow:
        return
    if flow.get('type') == 'topup':
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
