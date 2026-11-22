"""Inline-keyboard Telegram UI. No financial operations live inside callbacks."""
import asyncio
from decimal import Decimal, InvalidOperation

from aiogram import Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import func, select

from .business import BotError
from .config import settings
from .country_ui import arrange_country_offers, country_flag
from .db import Activation, Session, Setting, User
from .ledger import InsufficientFunds, apply

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


@router.message(CommandStart())
async def start(message: Message):
    u = await shop.ensure_user(message.from_user.id, message.from_user.username or '')
    if u.is_banned:
        await message.answer('Account restricted. Contact support.')
    elif not u.accepted_terms:
        await message.answer(TERMS, reply_markup=keyboard([[('✅ I accept', 'accept')]]))
    else:
        await message.answer('Welcome! Choose an option:', reply_markup=MAIN)


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


async def show_selection_menu(target, title: str, markup, *, edit: bool = False):
    """Reuse the same Telegram message for *every* menu page and Back button.

    Editing prevents previous country pages from stacking in chat. If Telegram
    refuses to edit an old/inaccessible menu, send a fresh one and remove the
    stale message; no purchase or wallet action happens in this helper.
    """
    if edit:
        try:
            return await target.edit_text(title, reply_markup=markup)
        except TelegramBadRequest as exc:
            if 'message is not modified' in str(exc).lower():
                return
        except (AttributeError, TypeError):
            pass
        new_menu = await target.answer(title, reply_markup=markup)
        await remove_previous_menu(target)
        return new_menu
    return await target.answer(title, reply_markup=markup)


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
        await callback.message.answer('Accepted. Welcome!', reply_markup=MAIN)
        return
    if not await allowed(uid, callback.message):
        return
    try:
        if data.startswith('services:'):
            await services(callback.message, int(data.split(':')[1]), edit=True)
        elif data.startswith('countries:'):
            _, service, page = data.split(':')
            await countries(callback.message, service, int(page), edit=True)
        elif data.startswith('preview:'):
            _, service, country = data.split(':')
            item, charge = await shop.quote(service, country)
            names = await shop.provider.get_country_names() if hasattr(shop.provider, 'get_country_names') else {}
            selected_country = names.get(country, f'Country #{country}')
            await callback.message.answer(
                f'{service.title()} • {country_flag(selected_country)} {selected_country}\n'
                f'Available: {item.stock}\nTotal price: {charge} {settings.wallet_currency}\n'
                'You are charged only if a code is received; no-code cancellations are refunded.',
                reply_markup=keyboard([[('✅ Confirm purchase', f'buy:{service}:{country}')],
                                       [('⬅️ Back', f'countries:{service}:0')]]),
            )
            # The country list is no longer needed after a country is selected.
            await remove_previous_menu(callback.message)
        elif data.startswith('buy:'):
            _, service, country = data.split(':')
            order = await shop.buy(uid, service, country)
            await callback.message.answer(
                f'✅ Order #{order.id}\nNumber: {order.phone}\n'
                f'Expires in up to {settings.activation_timeout_seconds // 60} minutes.\n'
                'Send an SMS to this number from your authorized account. '
                'Tap Check SMS if the message does not arrive automatically.',
                reply_markup=keyboard([[('🔄 Check SMS now', f'check:{order.id}')],
                                       [('❌ Cancel order', f'cancel:{order.id}')]]),
            )
            # Keep the new order receipt; remove the stale confirm-purchase card.
            await remove_previous_menu(callback.message)
        elif data.startswith('check:'):
            await check_sms(callback.message, uid, int(data.split(':')[1]))
        elif data.startswith('cancel:'):
            await shop.cancel(uid, int(data.split(':')[1]))
            await callback.message.answer('Cancellation submitted. Check /orders for current status.')
        elif data == 'balance':
            await show_balance(callback.message, uid)
        elif data == 'topup':
            await callback.message.answer(f'Top-up: send /topup 5 (amount in {settings.wallet_currency}).')
        elif data == 'orders':
            await show_orders(callback.message, uid)
        elif data == 'help':
            await callback.message.answer('Choose Buy number, service, country and confirm. '
                                          'Your code arrives here. /balance /orders /topup 5. '
                                          'Use /start for the menu.')
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


async def countries(target, service: str, page: int = 0, *, edit: bool = False):
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
                price = f'{await shop.price(item)} {settings.wallet_currency}'
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

async def show_balance(target, uid):
    user = await shop.get_user(uid)
    await target.answer(f'Your balance: {user.balance} {settings.wallet_currency}', reply_markup=MAIN)


async def show_orders(target, uid):
    async with Session() as s:
        orders = (await s.scalars(select(Activation).where(Activation.user_id == uid)
                                  .order_by(Activation.id.desc()).limit(10))).all()
    if not orders:
        await target.answer('No orders yet.')
        return
    lines = []
    for row in orders:
        number = row.phone if row.status in ('pending', 'ordering', 'received') else (
            '••••' + row.phone[-4:] if row.phone else 'hidden')
        lines.append(f'#{row.id} {row.service} / {row.country}: {row.status} • {number} • '
                     f'{row.price_charged} {settings.wallet_currency} • /check {row.id}')
    await target.answer('\n'.join(lines))


@router.message(Command('buy'))
async def buy_command(message: Message):
    if await allowed(message.from_user.id, message):
        await services(message)


@router.message(Command('balance'))
async def balance_command(message: Message):
    if await allowed(message.from_user.id, message):
        await show_balance(message, message.from_user.id)


async def check_sms(target, user_id: int, activation_id: int) -> None:
    """Owner-only, on-demand provider lookup when the free server was asleep."""
    if activation_id <= 0:
        await target.answer('Invalid order number.')
        return
    async with Session() as s:
        row = await s.get(Activation, activation_id)
        if row is None or row.user_id != user_id:
            await target.answer('Order not found.')
            return
        status = row.status
    if status in ('pending', 'received'):
        from .workers import Worker
        try:
            await Worker(shop).poll_order(activation_id)
        except Exception:
            await target.answer('Provider status temporarily unavailable. Your order is still saved.')
            return
    async with Session() as s:
        row = await s.get(Activation, activation_id)
        if row is None or row.user_id != user_id:
            await target.answer('Order not found.')
            return
        status = row.status
        code = row.code
    if code and status in ('received', 'completed'):
        await target.answer(f'🔐 Code for order #{activation_id}: {code}\nStatus: {status}')
    elif status == 'pending':
        await target.answer(
            f'Order #{activation_id}: waiting for an SMS at the provider. '
            'If the sender has not sent a code, Friday cannot create one. '
            'Use /check again to refresh, or cancel a pending order if unused.')
    elif status == 'manual_review':
        await target.answer('This order requires support review. Your reserved funds are protected.')
    else:
        await target.answer(f'Order #{activation_id}: {status}. Check /balance for any confirmed refund.')


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


@router.message(Command('topup'))
async def topup_command(message: Message):
    uid = message.from_user.id
    if not await allowed(uid, message):
        return
    try:
        _, raw = message.text.split(maxsplit=1)
        amount = Decimal(raw.strip())
        if not amount.is_finite():
            raise ValueError()
        invoice = await shop.start_payment(uid, amount)
    except (ValueError, InvalidOperation, BotError) as exc:
        await message.answer(str(exc) if isinstance(exc, BotError)
                             else f'Usage: /topup 5 (amount in {settings.wallet_currency})')
        return
    await message.answer(
        f'Invoice for {invoice["amount"]} {settings.wallet_currency}. '
        'Only pay via the gateway link below. Your wallet credits after confirmed payment.',
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=('Pay via NOWPayments' if settings.payment_provider == 'nowpayments' else 'Pay via Crypto Bot'), url=invoice['url']),
        ]]),
    )


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
