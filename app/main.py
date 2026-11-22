"""Render/web deployment entrypoint: one web service runs Telegram + payment callbacks."""
import asyncio
import json
import logging
from contextlib import asynccontextmanager

from aiogram import Bot, Dispatcher
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request

from .business import Shop
from .config import settings
from .db import engine, init_db
from .gateway import CryptoBot
from .handlers import router, set_shop
from .provider import FiveSim
from .grizzly import GrizzlySms
from .workers import Worker

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s %(message)s')
log = logging.getLogger(__name__)
# Grizzly authenticates with a query parameter; never log full provider URLs.
logging.getLogger('httpx').setLevel(logging.WARNING)
logging.getLogger('httpcore').setLevel(logging.WARNING)


class TelegramNotifier:
    def __init__(self, bot):
        self.bot = bot

    async def send(self, user_id: int, message: str):
        # Do not print messages/codes in logs.
        await self.bot.send_message(user_id, message)


runtime = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not settings.bot_token:
        raise RuntimeError('Set BOT_TOKEN securely in environment variables')
    if settings.live_purchases_enabled and not settings.live_ready:
        raise RuntimeError('Live buys require PROVIDER_API_KEY and a confirmed price conversion factor')
    if settings.payments_enabled and not settings.payment_ready:
        raise RuntimeError('Enabled payments require PAYMENT_API_KEY')
    if (settings.live_purchases_enabled or settings.payments_enabled) and settings.database_url.startswith('sqlite'):
        raise RuntimeError('Use PostgreSQL before enabling money movement')
    await init_db()
    bot = Bot(settings.bot_token)
    try:
        await bot.get_me()  # fail startup on invalid token, not a fake green health check
    except Exception:
        await bot.session.close()
        raise
    provider = GrizzlySms() if settings.provider_name == 'grizzly' else FiveSim()
    gateway = CryptoBot()
    shop = Shop(provider, gateway, TelegramNotifier(bot))
    set_shop(shop)
    dp = Dispatcher()
    dp.include_router(router)
    worker = Worker(shop)
    runtime['shop'] = shop
    tasks = [asyncio.create_task(dp.start_polling(bot, handle_signals=False)),
             asyncio.create_task(worker.run())]
    log.info('Telegram bot and persistent pollers started')
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        runtime.clear()
        await provider.aclose()
        await gateway.aclose()
        await bot.session.close()
        await engine.dispose()


web = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


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


@web.post('/webhook/cryptobot')
async def cryptobot_webhook(request: Request, background: BackgroundTasks):
    if not settings.payment_ready:
        raise HTTPException(status_code=503, detail='Payments disabled')
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
