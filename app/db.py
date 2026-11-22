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
