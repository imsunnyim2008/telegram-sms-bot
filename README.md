# Telegram Virtual Number SMS Bot — Phone-Friendly V1

A Telegram bot using **aiogram 3**, **5SIM**, **Crypto Bot invoices**, **SQLAlchemy/Postgres**, and a FastAPI webhook. Built from the supplied 8-page blueprint. For **authorized SMS receiving only**. It does **not** bypass the third-party site's security rules. Provider, pricing, and payment account setup are required for live transactions.

## What works

- `/start` → terms acceptance, inline Buy/Balance/Top up/Orders/Help menu.
- Dynamic (60-second cached) **operator-allowlisted** services and countries from 5sim with inventory and cost, 50% configurable markup.
- Confirmation, balance hold, one real provider purchase (never blind-retried), SMS polling, code delivery, cancellation and refund on verified cancellation/expiry.
- Database-backed pending orders survive application restarts; uncertain provider purchases require order-history review and are never retried blindly.
- `/topup 5` creates a Crypto Bot fiat-denominated invoice; signed webhook + polling fallback independently confirm payment, with one ledger credit per paid invoice.
- Admin-only `/stats`, `/adjust USER AMOUNT reason`, `/ban`, `/unban`, `/markup PERCENT`, `/broadcast TEXT`, `/resolve ORDER_ID refund`; low-balance alert.
- Rate limit (3 seconds between purchases), 10 pending per user, blocked service list, 30-day code deletion.

**Safety and live readiness:** Live purchases are DISABLED by default because your provider's raw API price unit must be converted to the wallet fiat currency. The conversion factor is an **operator setting**, not a live FX feed. Carefully verify the actual 5SIM account balance unit and price unit before setting `PROVIDER_PRICE_TO_WALLET` and `LIVE_PURCHASES_ENABLED=true`. Never assume a raw value of `21` means `$21` or `$0.21`. An incorrect factor could cause a loss. `WALLET_CURRENCY=USD` is an example, not proof of a conversion. Crypto Bot must support your selected fiat currency. Do not accept real top-ups before validating prices and payment reconciliation in test mode. Failed or ambiguous purchase outcomes never cause another automatic buy.

## On your iPhone (no computer)

1. Open **Telegram** → **@BotFather** → `/newbot` → save your new **BOT_TOKEN** privately.
2. Open https://github.com/new in Safari, create an empty PRIVATE repo named `telegram-sms-bot` (no README). After you tell ChatGPT **"GitHub repo created"**, it can push the files via your linked GitHub integration; alternatively use GitHub's browser upload / Codespaces to add the contents of this ZIP.
3. Open **Render** in Safari, create a Postgres DB, copy its **internal** `postgresql://...` URL; change the scheme to `postgresql+asyncpg://...` in Render's bot environment variables.
4. Create a Render **Web Service** (paid Starter for reliable background polling; free services sleep): connect your private GitHub repo, runtime **Python 3**, build `pip install -r requirements.txt`, start `uvicorn app.main:web --host 0.0.0.0 --port $PORT`, instance count **1**. Add `BOT_TOKEN`, `ADMIN_IDS` (your numerical Telegram ID), `DATABASE_URL`; leave `LIVE_PURCHASES_ENABLED=false`, `PAYMENTS_ENABLED=false` initially. Save/deploy.
5. In Telegram, open your bot, send `/start`, accept the terms. Test `/balance` and `/orders`. Then configure the optional real-provider and payment keys in Render **Environment**; test against a very small controlled charge and gateway test environment before enabling live payments.

**Do not paste BotFather, provider, database or gateway tokens in chat or GitHub source code.** Put them in Render **Environment** variables, not public files. Do not deploy into the existing Ultron/WAYNE LOGS service.

### Provider

Get a **5sim protocol** API key from 5sim.net. Docs: https://5sim.net/docs (calls are `GET /v1/guest/prices`, `GET /v1/user/profile`, `GET /v1/user/buy/activation/{country}/any/{product}?maxPrice=...`, `GET /v1/user/check/{id}`, `GET /v1/user/cancel/{id}`, `GET /v1/user/finish/{id}`, and `GET /v1/user/orders`). Provider balance and stock change. 5SIM can expire orders before 15 minutes. The bot honors the earlier provider expiry.

### Payment gateway

Create an app in Telegram's **@CryptoBot** (or **@CryptoTestnetBot** for testnet) and obtain the separate Crypto Pay API key. The mainnet API is `https://pay.crypt.bot/api` and the testnet URL is `https://testnet-pay.crypt.bot/api`. Configurable `PAYMENT_BASE_URL`. In your Crypto Bot app settings set a HTTPS webhook ending in `https://YOUR-RENDER-URL.onrender.com/webhook/cryptobot` (signature is derived from your API token). User's paid invoice is checked against amount, fiat currency and local payment ID before ledger credit. Pending invoices are also rechecked every 30 seconds. Never send paid signals manually to the API.

### Developer setup

```sh
cp .env.example .env
# Edit BOT_TOKEN and ADMIN_IDS in .env
pip install -r requirements-dev.txt
uvicorn app.main:web --host 0.0.0.0 --port 10000
# New terminal: pytest -q ; ruff check app tests
# Or with Postgres: docker compose up --build
```

The root `/health` endpoint returns a simple online indicator. Telegram polling uses one process; **do not run multiple copies with the same BOT_TOKEN**, or duplicate purchases and conflicting Telegram updates can occur. Managed Postgres is mandatory for concurrent money safety; SQLite is for single-process development only. Because purchases/refunds affect real money, perform independent QA, payment verification, and production security review before accepting customers.

### Troubleshooting

- `/start` doesn't reply: check `BOT_TOKEN`, Render service logs, ensure no other bot process is using polling.
- Prices blank: 5SIM may be unavailable or have no stock. Confirm provider API and country/service allowlist. More services can be explicitly enabled by changing `ALLOWED_SERVICES` in Render.
- "Prices are not configured": verify 5SIM unit, set conversion factor, then enable live purchases.
- Payment disabled: set `PAYMENT_API_KEY` and `PAYMENTS_ENABLED=true`; ensure `WALLET_CURRENCY` is a supported Crypto Pay fiat.
- An order enters **manual_review**: 5SIM returned an uncertain purchase result. Bot does NOT automatically purchase again. It checks recent orders but will never deliver another customer's SMS based only on timing. If no matching orders appear after expiry (and provider history is reachable), the wallet is refunded. Possible matches require operator investigation.
- Render free tier sleeps during idle periods and cannot guarantee 5-second SMS polling. Use a continuously running paid service and persistent database.

### Known limits before commercial launch

- The provider has **no documented idempotency key for buys**; an ambiguous 5SIM purchase can require operator investigation.
- The buy flow sends the first SMS code then closes the provider order, so follow-up SMS on the same number may not be received. This differs from the blueprint's aspirational "second SMS" requirement.
- This version uses SQLAlchemy `create_all()` for initial schema setup, not versioned Alembic migrations. Add migrations before making schema changes against customer data.
- Wallet value is set using a manual provider price factor. A verified accounting/FX policy is required before real customers use balances.
- Price quotes are 60-second cached and may differ from current stock. The provider's `maxPrice` prevents paying more than the quoted raw price.
- The balance checks and DB row locking require Postgres; scale beyond 1 worker only after adding distributed locks and a durable work queue.
- Tests use mocks and local DBs. A full security audit, migration test and load test are outstanding.

Read `DECISIONS.md` for project choices and `tests/` for coverage.

**Important:** Not every platform accepts temporary/virtual numbers. The bot does not generate OTPs or bypass destination-platform controls; it only forwards SMS codes received from an approved provider.