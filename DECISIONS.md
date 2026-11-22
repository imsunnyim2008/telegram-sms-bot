# Decisions and Deviations from Blueprint 1.0

1. **Deployment:** A single continuously running FastAPI web service also runs Telegram's long-polling bot and recovery workers, instead of separate web and worker services. Easier phone setup, NOT horizontally scalable.
2. **Database:** SQLite for isolated local tests; PostgreSQL required for real wallet safety. SQLAlchemy create_all at startup, migrations deferred. Avoid changing schema without proper Alembic migration.
3. **Payment:** Official Crypto Bot Crypto Pay supports fiat-denominated invoices, so a matching wallet fiat currency must be configured (default USD). The operator must confirm the conversion of 5SIM raw price units; purchasing disabled by default.
4. **Buy ambiguity:** Never repeat a 5SIM buy when the request could already have succeeded. Use history to establish when no order was made; any possible match requires human review before code delivery, because service/time matches alone are not ownership proof.
5. **Refund:** Refund only when cancellation/expiry is verified, or after history is reachable and shows no purchase. This avoids refunding an order that already delivered code; impossible provider cases require admin handling.
6. **Recovery:** Durable status and `next_poll_at` in activations are queried by worker on startup, rather than an additional jobs table. Functional equivalent for this MVP's activation and payment polling tasks.
7. **SMS:** Deliver first code only, and finish immediately. The provider can stop future delivery after finish; blueprint's extra SMS feature is not guaranteed.
8. **Safety:** Terms acceptance, banned categories, max active orders, no bulk registration/anti-detection tools.
9. **Retention:** Removes phone and SMS code after 30 days; no SMS text is persisted. INFO logs do not record codes or full phone numbers.
10. **Scope gaps:** no production migrations, automated reconciliation for multi-matches, rate-limited broadcast retries, detailed day/week stats, full 80% test coverage. These are listed to avoid representing the project as production-hardened.
