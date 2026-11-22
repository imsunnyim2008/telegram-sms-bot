"""All wallet changes are atomic and idempotent (requires PostgreSQL in production)."""
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .db import LedgerEntry, User


class InsufficientFunds(Exception):
    pass


async def apply(session: AsyncSession, user_id: int, amount: Decimal, entry_type: str,
                ref_type: str, ref_id: str, reason: str = '') -> bool:
    """Call inside session.begin(). Returns False if the exact entry already exists."""
    user = (await session.execute(select(User).where(User.id == user_id).with_for_update())).scalar_one()
    existing = (await session.execute(select(LedgerEntry.id).where(
        LedgerEntry.user_id == user_id,
        LedgerEntry.type == entry_type,
        LedgerEntry.ref_type == ref_type,
        LedgerEntry.ref_id == ref_id,
    ))).scalar_one_or_none()
    if existing is not None:
        return False
    amount = Decimal(str(amount)).quantize(Decimal('.0001'))
    after = Decimal(str(user.balance)) + amount
    if after < 0:
        raise InsufficientFunds('Your balance is too low')
    user.balance = after
    session.add(LedgerEntry(user_id=user_id, amount=amount, type=entry_type,
                            ref_type=ref_type, ref_id=ref_id,
                            balance_after=after, reason=reason[:240]))
    await session.flush()
    return True
