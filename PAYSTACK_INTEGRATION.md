# Paystack live integration (staged, not deployed)

Existing NOWPayments and manual bank flows remain intact. The new code is disabled by default and uses the live Paystack environment only if explicitly enabled.

Required Render configuration:
- PAYSTACK_SECRET_KEY: live key, never commit or share.
- PAYSTACK_WEBHOOK_SECRET: dashboard webhook signing secret if using the modern signature header. Legacy HMAC verification uses the live API secret.
- PAYSTACK_NGN_PER_USD: administrator-selected positive conversion rate. No assumed exchange rate.
- PAYSTACK_ENABLED=false until reviewed and verified.
- PAYSTACK_ADMIN_ONLY=true for a controlled rollout before public access.

Callback: /paystack/callback (informational, never proof of payment).
Webhook: /webhook/paystack (raw-body signature plus authenticated transaction verification).
The wallet is USD. Persist original NGN amount, USD credit, and unique reference before checkout. Verify live domain, exact reference, amount, currency, and success; credit once in a database transaction. An owner can recheck a pending payment from My deposits.

Remaining requirements before production: complete CI, inspect migration compatibility, review chargebacks/reversals, add reconciliation/monitoring for abandoned or delayed payments, confirm merchant activity approval, and verify fee/currency reporting. Test with mocked requests; never initiate a real charge during review.

Docs: https://paystack.com/docs/api/transaction/ and https://paystack.com/docs/payments/webhooks/
