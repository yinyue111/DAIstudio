"""Internal credit accounting.

Every change is written to credit_transactions, and the user row is updated
under a SELECT ... FOR UPDATE row lock so concurrent generations on the same
account stay consistent.

  grant   : admin tops up balance.                       balance += x
  freeze  : on task submit, reserve estimated cost.      balance -= x ; frozen += x
  settle  : on success, charge real cost, release rest.  frozen  -= reserved ; balance += (reserved - real)
  refund  : on failure, return the whole frozen amount.  balance += reserved ; frozen -= reserved
  unlock  : pay to unlock HD of a chosen asset.          balance -= y
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import CreditTransaction, User


class InsufficientCredits(Exception):
    pass


def _lock_user(db: Session, user_id: int) -> User:
    user = db.execute(
        select(User).where(User.id == user_id).with_for_update()
    ).scalar_one()
    return user


def _record(db: Session, user: User, type_: str, change: int, biz_type: str | None,
            biz_ref: int | None, note: str | None = None) -> None:
    db.add(
        CreditTransaction(
            user_id=user.id,
            type=type_,
            change=change,
            balance_after=user.balance_credits,
            biz_type=biz_type,
            biz_ref=biz_ref,
            note=note,
        )
    )


def _reject_negative(amount: int, name: str) -> None:
    if amount < 0:
        raise ValueError(f"{name} amount must be non-negative")


def _finish(db: Session, user: User, commit: bool) -> User:
    if commit:
        db.commit()
        db.refresh(user)
    else:
        db.flush()
    return user


def grant(db: Session, user_id: int, amount: int, note: str | None = None,
          *, biz_type: str = "admin", biz_ref: int | None = None,
          commit: bool = True) -> User:
    if amount <= 0:
        raise ValueError("grant amount must be positive")
    user = _lock_user(db, user_id)
    user.balance_credits += amount
    _record(db, user, "grant", amount, biz_type, biz_ref, note)
    return _finish(db, user, commit)


def freeze(db: Session, user_id: int, amount: int, biz_ref: int | None,
           *, commit: bool = True) -> User:
    _reject_negative(amount, "freeze")
    user = _lock_user(db, user_id)
    if amount == 0:
        return _finish(db, user, commit)
    if user.balance_credits < amount:
        db.rollback()
        raise InsufficientCredits(
            f"额度不足:需要 {amount},可用 {user.balance_credits}"
        )
    user.balance_credits -= amount
    user.frozen_credits += amount
    _record(db, user, "freeze", -amount, "gen_task", biz_ref)
    return _finish(db, user, commit)


def settle(db: Session, user_id: int, reserved: int, real_cost: int,
           biz_ref: int | None, *, commit: bool = True) -> User:
    """Charge real_cost out of the reserved (frozen) amount; return the rest."""
    _reject_negative(reserved, "settle reserved")
    _reject_negative(real_cost, "settle real_cost")
    real_cost = max(0, min(real_cost, reserved))
    refund_part = reserved - real_cost
    user = _lock_user(db, user_id)
    if reserved == 0:
        return _finish(db, user, commit)
    if reserved > user.frozen_credits:
        db.rollback()
        raise InsufficientCredits(
            f"冻结额度不足:需要释放 {reserved},当前冻结 {user.frozen_credits}"
        )
    user.frozen_credits -= reserved
    if refund_part:
        user.balance_credits += refund_part
    # `change` is the delta applied to *balance* in this op (the unused part
    # returned); the real cost stays spent (already removed at freeze). This
    # keeps sum(change) == balance. Real consumption is recorded in `note`.
    _record(db, user, "settle", refund_part, "gen_task", biz_ref,
            note=f"reserved={reserved} real={real_cost}")
    return _finish(db, user, commit)


def refund(db: Session, user_id: int, amount: int, biz_ref: int | None,
           *, commit: bool = True) -> User:
    _reject_negative(amount, "refund")
    user = _lock_user(db, user_id)
    if amount == 0:
        return _finish(db, user, commit)
    if amount > user.frozen_credits:
        db.rollback()
        raise InsufficientCredits(
            f"冻结额度不足:需要退回 {amount},当前冻结 {user.frozen_credits}"
        )
    user.frozen_credits -= amount
    user.balance_credits += amount
    _record(db, user, "refund", amount, "gen_task", biz_ref)
    return _finish(db, user, commit)


def unlock(db: Session, user_id: int, amount: int, biz_ref: int | None,
           *, commit: bool = True) -> User:
    _reject_negative(amount, "unlock")
    user = _lock_user(db, user_id)
    if amount == 0:
        return _finish(db, user, commit)
    if user.balance_credits < amount:
        db.rollback()
        raise InsufficientCredits(
            f"额度不足:解锁需要 {amount},可用 {user.balance_credits}"
        )
    user.balance_credits -= amount
    _record(db, user, "unlock", -amount, "unlock", biz_ref)
    return _finish(db, user, commit)


def consume(db: Session, user_id: int, amount: int, *,
            biz_type: str, biz_ref: int | None = None,
            note: str | None = None, commit: bool = True) -> User:
    """Charge a synchronous model call that has no GenTask reservation.

    Used for calls such as reverse prompt: the call still needs an up-front
    balance check, but there is no queued task to freeze/settle.
    """
    _reject_negative(amount, "consume")
    if amount == 0:
        return _lock_user(db, user_id)
    user = _lock_user(db, user_id)
    if user.balance_credits < amount:
        db.rollback()
        raise InsufficientCredits(
            f"额度不足:需要 {amount},可用 {user.balance_credits}"
        )
    user.balance_credits -= amount
    _record(db, user, "consume", -amount, biz_type, biz_ref, note)
    return _finish(db, user, commit)


def refund_consumed(db: Session, user_id: int, amount: int, *,
                    biz_type: str, biz_ref: int | None = None,
                    note: str | None = None, commit: bool = True) -> User:
    """Return a previously consumed synchronous model-call charge."""
    _reject_negative(amount, "refund_consumed")
    if amount == 0:
        return _lock_user(db, user_id)
    user = _lock_user(db, user_id)
    user.balance_credits += amount
    _record(db, user, "refund", amount, biz_type, biz_ref, note)
    return _finish(db, user, commit)
