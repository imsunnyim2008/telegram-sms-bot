"""Render/web deployment entrypoint: one web service runs Telegram + payment callbacks."""
import asyncio
import hashlib
import hmac
import json
import logging
from contextlib import asynccontextmanager

from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Update
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request

from .business import Shop
from .config import settings
from .db import engine, init_db, Session, TelegramUpdateReceipt
from .gateway import CryptoBot
from .nowpayments import NowPayments
from .pricing_web import router as pricing_router
from .handlers import router, set_shop
from .command_menu import register_native_menu
from .provider import FiveSim
from .grizzly import GrizzlySms
from .textverified import TextVerifiedSms
from .textverified_webhooks import (valid_signature as valid_textverified_signature,
                                    parse_event as parse_textverified_event,
                                    wake_matching_orders)
from .workers import Worker
from .operations import accept_finished_ipn
from .service_ui import custom_emoji_unavailable, without_custom_emoji
from sqlalchemy.exc import IntegrityError

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s %(message)s')
log = logging.getLogger(__name__)
# Grizzly authenticates with a query parameter; never log full provider URLs.
logging.getLogger('httpx').setLevel(logging.WARNING)
logging.getLogger('httpcore').setLevel(logging.WARNING)


class TelegramNotifier:
    def __init__(self, bot):
        self.bot = bot

    async def send(self, user_id: int, message: str, *, parse_mode: str | None = None):
        # Never print phone numbers or verification codes in logs.
        try:
            await self.bot.send_message(user_id, message, parse_mode=parse_mode)
        except TelegramBadRequest as exc:
            if parse_mode != "HTML" or not custom_emoji_unavailable(exc):
                raise
            # Only retry if the custom-emoji entity was rejected (e.g. bot
            # owner lacks Premium). Retain the app name, never send false logos.
            await self.bot.send_message(user_id, without_custom_emoji(message),
                                        parse_mode=None)


    async def send_api_alert(self, user_id, message, incident):
        await self.bot.send_message(user_id, message, reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text='✅ Acknowledge alert',
                                                   callback_data=f'ui:ackoutage:{incident}')]]))


runtime = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not settings.bot_token:
        raise RuntimeError('Set BOT_TOKEN securely in environment variables')
    if settings.live_purchases_enabled and not settings.live_ready:
        raise RuntimeError('Live buys require PROVIDER_API_KEY and a confirmed price conversion factor')
    if settings.payments_enabled and not settings.payment_ready:
        raise RuntimeError('Enabled payments require valid gateway API key, IPN secret and PUBLIC_BASE_URL')
    if (settings.live_purchases_enabled or settings.payments_enabled) and settings.database_url.startswith('sqlite'):
        raise RuntimeError('Use PostgreSQL before enabling money movement')
    await init_db()
    bot = Bot(settings.bot_token)
    try:
        await bot.get_me()  # fail startup on invalid token, not a fake green health check
    except Exception:
        await bot.session.close()
        raise
    # Keep legacy providers available so outstanding orders keep polling
    # the same provider even after new purchases switch to TextVerified.
    providers = {
        'grizzly': GrizzlySms(),
        'fivesim': FiveSim(),
        'textverified': TextVerifiedSms(),
    }
    provider = providers.get(settings.provider_name)
    if provider is None:
        raise RuntimeError('Unknown PROVIDER_NAME')
    gateway = NowPayments() if settings.payment_provider == 'nowpayments' else CryptoBot()
    shop = Shop(provider, gateway, TelegramNotifier(bot))
    shop.providers = providers
    set_shop(shop)
    dp = Dispatcher()
    dp.include_router(router)
    worker = Worker(shop)
    runtime['shop'] = shop
    runtime.update({'bot': bot, 'dp': dp, 'worker': worker})
    # Telegram's inbound HTTP webhook wakes a sleeping Render Free service.
    # Outbound long-polling cannot wake a sleeping service.
    if not settings.public_base_url.startswith('https://'):
        raise RuntimeError('Telegram webhook requires PUBLIC_BASE_URL with https')
    await bot.set_webhook(
        settings.public_base_url.rstrip('/') + '/webhook/telegram',
        secret_token=telegram_webhook_secret(),
        drop_pending_updates=False,
        allowed_updates=dp.resolve_used_update_types(),
        max_connections=4,
    )
    await register_native_menu(bot, settings.admins)
    tasks = [asyncio.create_task(worker.run()), asyncio.create_task(worker.run_health_monitor())]
    log.info('Telegram signed webhook and SMS polling worker started')
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        runtime.clear()
        for item in providers.values():
            await item.aclose()
        await gateway.aclose()
        await bot.session.close()
        await engine.dispose()


web = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
web.include_router(pricing_router)


@web.get('/')
async def home():
    return {'service': 'telegram-sms-bot', 'status': 'online'}


@web.get('/health')
async def health():
    return {'status': 'ok'}


async def process_payment(invoice_id: str):
    try:
        await runtime['shop'].verify_and_credit(invoice_id)
    except Exception:
        log.exception('Payment verification failed')


def telegram_webhook_secret() -> str:
    # Derived from the private BOT_TOKEN. Never emit in logs or URLs.
    return hashlib.sha256(('Friday-webhook-v1:' + settings.bot_token).encode()).hexdigest()


@web.post('/webhook/telegram')
async def telegram_webhook(request: Request):
    secret = request.headers.get('x-telegram-bot-api-secret-token', '')
    if not hmac.compare_digest(secret, telegram_webhook_secret()):
        raise HTTPException(status_code=403, detail='Invalid webhook authentication')
    body = await request.body()
    if len(body) > 65536:
        raise HTTPException(status_code=413, detail='Telegram event is too large')
    try:
        event = Update.model_validate(json.loads(body), context={'bot': runtime['bot']})
    except (ValueError, TypeError, KeyError):
        raise HTTPException(status_code=400, detail='Malformed Telegram update')
    if event.update_id < 0:
        raise HTTPException(status_code=400, detail='Malformed update ID')
    # One-time durable claim before executing potentially chargeable callbacks.
    # If a handler fails, user can retry their action; automatic Telegram
    # retries must NOT issue a second chargeable SMS purchase.
    try:
        async with Session.begin() as s:
            s.add(TelegramUpdateReceipt(update_id=event.update_id))
            await s.flush()
    except IntegrityError:
        return {'ok': True, 'duplicate': True}
    try:
        await runtime['dp'].feed_update(runtime['bot'], event)
    except Exception:
        log.exception('Telegram webhook handler failed; update claimed to avoid duplicate charges')
        # Return 200: a Telegram retry would not be safe for paid purchases.
    return {'ok': True}


@web.post('/webhook/textverified')
async def textverified_webhook(request: Request):
    """TextVerified.com SMS webhook; it cannot directly set codes or issue refunds."""
    if not settings.textverified_webhook_secret:
        # Fail closed until the dashboard-provided signing secret is added
        # to Render (not the TextVerified API key).
        raise HTTPException(status_code=503, detail='TextVerified webhook not configured')
    body = await request.body()
    if len(body) > 65536:
        raise HTTPException(status_code=413, detail='Webhook payload too large')
    signature = request.headers.get('x-webhook-signature', '')
    if not valid_textverified_signature(body, signature, settings.textverified_webhook_secret):
        raise HTTPException(status_code=401, detail='Invalid TextVerified signature')
    try:
        event = parse_textverified_event(body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail='Malformed TextVerified event') from exc
    # Durable dedup and ownership checks; regular poll worker retrieves the
    # code from the authenticated provider API. The public request is never
    # allowed to set an OTP, credit wallets, cancel, or purchase a number.
    await wake_matching_orders(event)
    return {'ok': True}


@web.post('/webhook/cryptobot')
async def cryptobot_webhook(request: Request, background: BackgroundTasks):
    if settings.payment_provider != 'cryptobot' or not settings.payment_ready:
        raise HTTPException(status_code=503, detail='Crypto Bot payments disabled')
    raw = await request.body()
    if len(raw) > 65536:
        raise HTTPException(status_code=413, detail='Payload too large')
    signature = request.headers.get('crypto-pay-api-signature', '')
    if not CryptoBot.verify_webhook(raw, signature):
        raise HTTPException(status_code=401, detail='Invalid signature')
    try:
        data = json.loads(raw)
        if data.get('update_type') != 'invoice_paid':
            return {'ok': True}
        invoice_id = str(data['payload']['invoice_id'])
        if not invoice_id.isdigit() or len(invoice_id) > 90:
            raise ValueError('Malformed invoice')
    except (ValueError, KeyError, TypeError, AttributeError):
        raise HTTPException(status_code=400, detail='Malformed payment event')
    background.add_task(process_payment, invoice_id)
    return {'ok': True}


async def process_nowpayments_payment(order_id: str | None,
                                      invoice_id: str | None, payment_id: str):
    try:
        await runtime['shop'].verify_and_credit_nowpayments(
            invoice_id, payment_id, order_id=order_id)
    except Exception:
        # Never log API credentials, signed payloads, or private wallet data.
        log.exception('NOWPayments payment verification task failed')


@web.post('/webhook/nowpayments')
async def nowpayments_webhook(request: Request, background: BackgroundTasks):
    if settings.payment_provider != 'nowpayments' or not settings.payment_ready:
        raise HTTPException(status_code=503, detail='NOWPayments disabled')
    raw = await request.body()
    if len(raw) > 65536:
        raise HTTPException(status_code=413, detail='Payload too large')
    signature = request.headers.get('x-nowpayments-sig', '')
    if not signature:
        log.warning('NOWPayments IPN rejected: signature header missing')
        raise HTTPException(status_code=401, detail='Invalid signature')
    if not NowPayments.verify_webhook(raw, signature):
        # Do not print the signature, raw payload or secret.
        log.warning('NOWPayments IPN rejected: signature mismatch')
        raise HTTPException(status_code=401, detail='Invalid signature')
    try:
        event = json.loads(raw)
        if not isinstance(event, dict):
            raise ValueError()
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail='Malformed IPN event')
    if event.get('payment_status') != 'finished':
        # Intermediate statuses are handled by NOWPayments. Never credit them.
        return {'ok': True}
    order_id = event.get('order_id')
    if not isinstance(order_id, str) or not (
        order_id.startswith('friday-topup-') and
        order_id[len('friday-topup-'):].isascii() and
        order_id[len('friday-topup-'):].isdigit() and len(order_id) <= 55
    ):
        order_id = None
    invoice_id = event.get('invoice_id')
    invoice_id = str(invoice_id) if invoice_id is not None else None
    if invoice_id is not None and not (invoice_id.isascii() and
                                       invoice_id.isdigit() and len(invoice_id) <= 40):
        raise HTTPException(status_code=400, detail='Malformed invoice ID')
    payment_id = str(event.get('payment_id', ''))
    if not (payment_id.isascii() and payment_id.isdigit() and len(payment_id) <= 40):
        raise HTTPException(status_code=400, detail='Malformed payment ID')
    if not order_id and not invoice_id:
        log.warning('Signed finished NOWPayments IPN has no usable order reference')
        return {'ok': True}
    # Hosted invoice notifications may carry invoice_id=null. Local order_id
    # still uniquely links a customer to their previously created invoice.
    # Persist the signed reference before acknowledging the callback. The worker
    # can retry after a crash or an upstream timeout without double-crediting.
    await accept_finished_ipn(Session, payment_id, invoice_id, order_id)
    background.add_task(process_nowpayments_payment, order_id, invoice_id, payment_id)
    return {'ok': True}
