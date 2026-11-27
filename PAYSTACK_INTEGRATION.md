# Paystack integration — test foundation

This branch adds an isolated test-mode client and mocked tests. It is deliberately not connected to the live bot, webhooks or customer balances.

Before a production rollout:
- Confirm the approved merchant activities, including the actual FRIDAY offering, with Paystack.
- Obtain a test secret key via the dashboard. Store credentials only in a separate test environment. Never paste them into chats or commits.
- Collect the payer's actual email with notice of its use; Paystack requires an email.
- Choose an explicit NGN to USD conversion rate and snapshot it per invoice. The current FRIDAY wallet is USD; test payments must never credit a production wallet.
- Implement persistent reference creation, authenticated verification and atomic one-time wallet credits in an isolated test database; verify expected amount in kobo, reference, currency, status and environment.
- Register a test-only webhook endpoint; verify signatures before processing, and use durable retry/reconciliation.
- Add tests for duplicates, wrong currencies, test/live separation, interrupted checkouts, reversals and failure recovery. Keep NOWPayments intact.
- Obtain review before merging and deploying.

Docs: https://paystack.com/docs/api/transaction/ and https://paystack.com/docs/payments/webhooks/
