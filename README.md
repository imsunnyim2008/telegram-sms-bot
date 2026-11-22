## TextVerified SMS integration (November 2026)

Friday now has an **optional TextVerified** SMS provider, grounded in the official
API workflow: exchange account email/API key for bearer token, enumerate SMS
services, check available inventory and prices, purchase a verification, and
poll the specific order for incoming SMS and parsed code. TextVerified provides
**US numbers only**; Friday does not show fake UK, Canadian or Nigerian stock
for this provider. Existing Grizzly and 5SIM purchases remain assigned to
their original providers for SMS polling and cancellation.

The official Python SDK examples use `pip install textverified` and synchronous
`TextVerified(...)` calls. Friday's asynchronous FastAPI/aiogram server instead
calls the **same documented /api/pub/v2 endpoints** directly with its existing
`httpx` dependency, so the bot event loop stays responsive. No additional
SDK installation is needed for this async adapter.

**Render Environment — administrator setup (DO NOT commit your credentials):**

```env
PROVIDER_NAME=textverified
TEXTVERIFIED_API_USERNAME=<your TextVerified account email>
TEXTVERIFIED_API_KEY=<your private TextVerified API key>
WALLET_CURRENCY=USD
PROVIDER_PRICE_TO_WALLET=1
LIVE_PURCHASES_ENABLED=false
```

Do not reuse Grizzly's `PROVIDER_API_KEY`; TextVerified has its own credentials.
Keep `LIVE_PURCHASES_ENABLED=false` until `/setupcheck` reports connected,
`/buy` lists real stock/prices, and one authorized low-cost verification,
SMS delivery, and refund are independently tested. Switching provider alone
does not guarantee that a third-party platform will send an OTP.

**Money safety:** Purchases are never retried blindly. Uncertain chargeable
operations go to manual review, including failures fetching details after
TextVerified accepted a purchase. No wallet refund is applied merely because
TextVerified says a verification was cancelled; it must confirm a refund.
TextVerified's prices and credit balance are in USD; the conversion factor
must be exactly 1. Old active Grizzly orders continue checking through Grizzly,
as long as the old API credentials remain available.

**No automatic switch:** merging this code does not switch the production bot
or create charges. The administrator must configure Render environment values
above and redeploy; only then can this provider be used.

Official TextVerified sources:
- https://www.textverified.com/automate
- https://www.textverified.com/products/api
- https://github.com/Westbold/PythonClient

---

## Production audit / payment notification checks

Send `/testall` in Friday as an authorized admin. This **read-only** command
checks the bot's database, wallet-ledger consistency, stored invoice/order counts,
Grizzly API balance and stock, and NOWPayments API authentication. It does not
buy numbers, send SMS, charge cards, create cryptocurrency invoices, or move funds.

If NOWPayments IPNs are rejected, the Render app logs distinguish **missing**
signature headers from **HMAC mismatches** without printing secrets or payloads.
An HTTP 401 to a NOWPayments IPN means payment confirmation was **not**
processed; do not manually credit a wallet based solely on that webhook.

The HMAC verification accepts both the top-level alphabetically sorted
JSON payload described in NOWPayments' official IPN docs and the recursively
sorted form used by SDKs. Both require valid HMAC-SHA512 with the IPN secret.
No unsigned or tampered webhook is accepted.

**End-to-end live tests still necessary:** the merchant must verify the
payout wallet, create an authorized $2–$5 invoice, send a small actual
payment, watch the provider transaction reach `finished`, confirm the
Friday wallet increased **exactly once**, and separately test one authorized
SMS activation and confirmed cancellation/refund. These require funds and
user action and are never performed by `/testall`.

NOWPayments IPN reference:
https://nowpayments.zendesk.com/hc/en-us/articles/21395546303389-IPN-and-how-to-setup

# Telegram Virtual Number SMS Bot — Phone-Friendly V1

## NOWPayments integration (staged; requires review)

Friday can be configured to use NOWPayments hosted crypto checkout instead of Crypto Bot.
Changes are fail-closed: the existing payment integration remains the default and
`PAYMENTS_ENABLED=false` blocks all new customer top-ups until your credentials
and IPN callback are confirmed.

Add these secrets **only** in the Friday service's Render Environment settings:

```env
PAYMENT_PROVIDER=nowpayments
NOWPAYMENTS_API_KEY=<merchant API key from NOWPayments>
NOWPAYMENTS_IPN_SECRET=<IPN secret from Payment Settings>
PUBLIC_BASE_URL=https://telegram-sms-bot-tgcg.onrender.com
PAYMENTS_ENABLED=false
```

Set your **withdrawal/payout wallet** in NOWPayments before taking payment.
The IPN callback URL for this service is
`https://telegram-sms-bot-tgcg.onrender.com/webhook/nowpayments`.
It is sent in each invoice creation request and uses HMAC-SHA512
signature verification (`x-nowpayments-sig`) with recursively sorted JSON.
Each completed payment is re-checked using NOWPayments'
`GET /v1/payment/{payment_id}` before updating the DB ledger.
Wallet credit happens once per local invoice (unique ledger reference).
Only a `finished` payment with matching invoice ID, order ID, fiat currency
and amount qualifies. No credit occurs on `waiting`, `confirmed` or
`partially_paid` status. No customer can trigger a credit by
typing an invoice ID.

**Known limitation:** Hosted invoice payment IDs do not exist until checkout.
NOWPayments is therefore verified from signed IPN callbacks rather than
invoice-ID polling. Lost/failed IPNs require manual reconciliation using
the provider's dashboard; build a durable callback inbox, admin reconciliation
and integration tests before public launch. Render free tier sleeps;
use reliable hosting for real payments. Test an authorized small payment and
a refund/cancellation path before changing `PAYMENTS_ENABLED=true`.

Gateway docs: https://nowpayments.zendesk.com/hc/en-us/articles/21345824322717-API-and-endpoint-description
IPN docs: https://nowpayments.zendesk.com/hc/en-us/articles/21395546303389-IPN-and-how-to-setup


## Grizzly SMS integration (November 2026)

**Provider changed from 5SIM to Grizzly SMS.** The default backend is now
PROVIDER_NAME=grizzly; PROVIDER_NAME=fivesim retains the old backend.
Grizzly uses api.grizzlysms.com/stubs/handler_api.php and these actions:
getPrices, getBalance, getNumberV2, getStatus and setStatus.
The Grizzly API key is a query parameter; the app suppresses HTTP client URL logging.

**iPhone activation:** In Render → telegram-sms-bot → Environment, add:

    PROVIDER_NAME=grizzly
    PROVIDER_API_KEY=<your private Grizzly API key>

Do not share your token in chat or commit it to GitHub. After deployment
send /setupcheck to the Telegram bot as its admin. This checks the provider
balance without buying a number. Browse /buy for live prices and stock.

**Real money remains disabled for testing:**

    LIVE_PURCHASES_ENABLED=false
    PAYMENTS_ENABLED=false
    PROVIDER_PRICE_TO_WALLET=0

The Grizzly purchase response may include numeric currency code 643 (RUB).
Do not assume raw provider prices equal USD; establish a verified currency
conversion before setting the price factor or accepting customer money.

Payment is a separate integration with Crypto Pay and PAYMENT_API_KEY.
Never reuse your BotFather token for payments. Sandbox-test payment callbacks.

Grizzly getNumberV2 purchases are never blindly retried on network errors.
Ambiguous orders enter manual review. Grizzly getActiveActivations is not a
complete purchase history, so this app avoids automatic refunds based only
on a missing active order. Public use requires controlled tests, operator
reconciliation procedures and an always-on host.

Docs: https://grizzlysms.com/docs/activation-api-request-a-number-v2


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
