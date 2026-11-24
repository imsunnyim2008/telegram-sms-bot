from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import (BigInteger, Boolean, CheckConstraint, DateTime, ForeignKey,
                        Index, Integer, Numeric, String, Text, UniqueConstraint)
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from .config import settings


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = 'users'
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    username: Mapped[str] = mapped_column(String(100), default='')
    balance: Mapped[Decimal] = mapped_column(Numeric(16, 4), default=Decimal('0'), nullable=False)
    accepted_terms: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_banned: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (CheckConstraint('balance >= 0', name='users_nonnegative_balance'),)


class LedgerEntry(Base):
    __tablename__ = 'ledger_entries'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id'), index=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(16, 4), nullable=False)
    type: Mapped[str] = mapped_column(String(24), nullable=False)
    ref_type: Mapped[str] = mapped_column(String(24), nullable=False)
    ref_id: Mapped[str] = mapped_column(String(100), nullable=False)
    balance_after: Mapped[Decimal] = mapped_column(Numeric(16, 4), nullable=False)
    reason: Mapped[str] = mapped_column(String(240), default='')
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (UniqueConstraint('user_id', 'type', 'ref_type', 'ref_id', name='uniq_ledger_reference'),)


class Activation(Base):
    __tablename__ = 'activations'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id'), index=True)
    provider: Mapped[str] = mapped_column(String(30), default='fivesim')
    provider_order_id: Mapped[str | None] = mapped_column(String(90), unique=True)
    service: Mapped[str] = mapped_column(String(50))
    country: Mapped[str] = mapped_column(String(50))
    phone: Mapped[str | None] = mapped_column(String(40))
    cost_provider: Mapped[Decimal] = mapped_column(Numeric(16, 4))
    price_charged: Mapped[Decimal] = mapped_column(Numeric(16, 4))
    status: Mapped[str] = mapped_column(String(24), default='ordering')
    code: Mapped[str | None] = mapped_column(String(100))
    sms_seen: Mapped[int] = mapped_column(Integer, default=0)
    sms_notified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_poll_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (Index('idx_activation_state_expiry', 'status', 'expires_at'),)


class Payment(Base):
    __tablename__ = 'payments'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id'), index=True)
    gateway: Mapped[str] = mapped_column(String(30), default='cryptobot')
    gateway_invoice_id: Mapped[str] = mapped_column(String(90))
    amount_fiat: Mapped[Decimal] = mapped_column(Numeric(16, 4))
    amount_credit: Mapped[Decimal] = mapped_column(Numeric(16, 4))
    status: Mapped[str] = mapped_column(String(24), default='created')
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (UniqueConstraint('gateway', 'gateway_invoice_id', name='uniq_invoice'),)


class TelegramUpdateReceipt(Base):
    """Durable idempotency guard for incoming Telegram webhook updates."""
    __tablename__ = 'telegram_update_receipts'
    update_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    processed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class TextVerifiedWebhookReceipt(Base):
    """HMAC-authenticated event deduplication without storing SMS or OTP."""
    __tablename__ = 'textverified_webhook_receipts'
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    processed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class MaintenanceNotice(Base):
    """Durable, private notification queue; contains no phone numbers or codes."""
    __tablename__ = 'maintenance_notices'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id'), index=True)
    state: Mapped[str] = mapped_column(String(8))
    status: Mapped[str] = mapped_column(String(12), default='pending', index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PaymentInbox(Base):
    """Minimal durable reference from a verified finished NOWPayments IPN."""
    __tablename__ = 'payment_inbox'
    payment_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    invoice_id: Mapped[str | None] = mapped_column(String(40))
    order_id: Mapped[str | None] = mapped_column(String(55))
    status: Mapped[str] = mapped_column(String(16), default='pending', index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AdminAudit(Base):
    __tablename__ = 'admin_audit'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    actor_id: Mapped[int] = mapped_column(BigInteger, index=True)
    action: Mapped[str] = mapped_column(String(40))
    reference: Mapped[str] = mapped_column(String(100), default='')
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SupportMessage(Base):
    __tablename__ = 'support_messages'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey('activations.id'), index=True)
    actor_id: Mapped[int] = mapped_column(BigInteger)
    from_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    body: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class OpsNotice(Base):
    __tablename__ = 'ops_notices'
    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    recipient: Mapped[int] = mapped_column(BigInteger)
    body: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(12), default='pending', index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class MoniepointEvent(Base):
    """Minimal authenticated event metadata for an admin-only, non-crediting pilot."""
    __tablename__ = 'moniepoint_events'
    event_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    event_type: Mapped[str] = mapped_column(String(40))
    transaction_key: Mapped[str] = mapped_column(String(64))
    amount_kobo: Mapped[int] = mapped_column(BigInteger)
    transaction_status: Mapped[str] = mapped_column(String(24))
    response_code: Mapped[str] = mapped_column(String(12))
    has_merchant_reference: Mapped[bool] = mapped_column(Boolean)
    has_narration: Mapped[bool] = mapped_column(Boolean)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class BankEventMatch(Base):
    """Hashed identifiers from an authenticated bank event, never raw bank data."""
    __tablename__ = 'bank_event_matches'
    event_key: Mapped[str] = mapped_column(ForeignKey('moniepoint_events.event_key'), primary_key=True)
    rrn_key: Mapped[str | None] = mapped_column(String(64), index=True)
    sender_key: Mapped[str | None] = mapped_column(String(64), index=True)


class BankDeposit(Base):
    __tablename__ = 'bank_deposits'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id'), index=True)
    amount_kobo: Mapped[int] = mapped_column(BigInteger)
    rate: Mapped[Decimal] = mapped_column(Numeric(16, 4))
    credit: Mapped[Decimal] = mapped_column(Numeric(16, 4))
    method: Mapped[str] = mapped_column(String(8))
    match_key: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), default='pending', index=True)
    verified_transaction: Mapped[str | None] = mapped_column(String(64), unique=True)
    payment_id: Mapped[int | None] = mapped_column(ForeignKey('payments.id'), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Setting(Base):
    __tablename__ = 'settings'
    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    value: Mapped[str] = mapped_column(Text)


# Render may supply an ordinary PostgreSQL (psycopg) URL, but our
# dependencies use the asynchronous asyncpg driver. Normalize the dialect.
def async_database_url(url: str) -> str:
    prefix, separator, remainder = url.partition('://')
    if separator and prefix in {
        'postgres', 'postgresql', 'postgresql+psycopg', 'postgresql+psycopg2'
    }:
        return 'postgresql+asyncpg://' + remainder
    return url


engine = create_async_engine(async_database_url(settings.database_url), pool_pre_ping=True)
Session = async_sessionmaker(engine, expire_on_commit=False)


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
