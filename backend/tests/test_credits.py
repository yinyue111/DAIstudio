"""Credit accounting: freeze/settle/refund/unlock + ledger reconciliation.
Runs on an isolated SQLite file (models use portable column types)."""
import tempfile

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

import app.models  # noqa: F401  (register tables on Base)
from app.db import Base
from app.models import CreditTransaction, User
from app.services import credits


def make_session():
    _, path = tempfile.mkstemp(suffix=".db")
    eng = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(eng)
    return sessionmaker(bind=eng)()


def test_credit_lifecycle():
    db = make_session()
    u = User(phone="13800000000", status="active", balance_credits=0, frozen_credits=0)
    db.add(u)
    db.commit()
    db.refresh(u)

    credits.grant(db, u.id, 100)
    db.refresh(u)
    assert u.balance_credits == 100

    credits.freeze(db, u.id, 30, biz_ref=1)
    db.refresh(u)
    assert (u.balance_credits, u.frozen_credits) == (70, 30)

    # real cost 20 of 30 reserved -> 10 returned
    credits.settle(db, u.id, reserved=30, real_cost=20, biz_ref=1)
    db.refresh(u)
    assert (u.balance_credits, u.frozen_credits) == (80, 0)

    credits.unlock(db, u.id, 5, biz_ref=2)
    db.refresh(u)
    assert u.balance_credits == 75

    credits.consume(db, u.id, 3, biz_type="reverse", biz_ref=3)
    db.refresh(u)
    assert u.balance_credits == 72

    # ledger sum reconciles with balance
    total = db.execute(select(func.coalesce(func.sum(CreditTransaction.change), 0))).scalar()
    assert total == u.balance_credits == 72


def test_refund_and_insufficient():
    db = make_session()
    u = User(phone="13900000000", status="active", balance_credits=50, frozen_credits=0)
    db.add(u)
    db.commit()
    db.refresh(u)

    credits.freeze(db, u.id, 40, biz_ref=1)
    db.refresh(u)
    assert (u.balance_credits, u.frozen_credits) == (10, 40)

    credits.refund(db, u.id, 40, biz_ref=1)
    db.refresh(u)
    assert (u.balance_credits, u.frozen_credits) == (50, 0)

    with pytest.raises(credits.InsufficientCredits):
        credits.freeze(db, u.id, 999, biz_ref=2)
    db.refresh(u)
    assert (u.balance_credits, u.frozen_credits) == (50, 0)  # unchanged on failure

    credits.consume(db, u.id, 10, biz_type="reverse", biz_ref=3)
    db.refresh(u)
    assert (u.balance_credits, u.frozen_credits) == (40, 0)

    credits.refund_consumed(db, u.id, 10, biz_type="reverse", biz_ref=3)
    db.refresh(u)
    assert (u.balance_credits, u.frozen_credits) == (50, 0)


def test_settle_and_refund_cannot_exceed_frozen_balance():
    db = make_session()
    u = User(phone="13900000002", status="active", balance_credits=50, frozen_credits=0)
    db.add(u)
    db.commit()
    db.refresh(u)

    credits.freeze(db, u.id, 20, biz_ref=1)
    db.refresh(u)
    assert (u.balance_credits, u.frozen_credits) == (30, 20)

    with pytest.raises(credits.InsufficientCredits):
        credits.settle(db, u.id, reserved=21, real_cost=1, biz_ref=1)
    db.refresh(u)
    assert (u.balance_credits, u.frozen_credits) == (30, 20)

    with pytest.raises(credits.InsufficientCredits):
        credits.refund(db, u.id, 21, biz_ref=1)
    db.refresh(u)
    assert (u.balance_credits, u.frozen_credits) == (30, 20)


def test_refund_cannot_spend_another_task_reservation():
    db = make_session()
    u = User(phone="13900000003", status="active", balance_credits=100, frozen_credits=0)
    db.add(u)
    db.commit()
    db.refresh(u)

    credits.freeze(db, u.id, 20, biz_ref=1)
    db.refresh(u)
    assert (u.balance_credits, u.frozen_credits) == (80, 20)

    with pytest.raises(credits.InsufficientCredits):
        credits.refund(db, u.id, 20, biz_ref=2)
    db.refresh(u)
    assert (u.balance_credits, u.frozen_credits) == (80, 20)

    credits.refund(db, u.id, 20, biz_ref=1)
    db.refresh(u)
    assert (u.balance_credits, u.frozen_credits) == (100, 0)


def test_negative_credit_amounts_are_rejected():
    db = make_session()
    u = User(phone="13900000001", status="active", balance_credits=50, frozen_credits=10)
    db.add(u)
    db.commit()
    db.refresh(u)

    for fn, args in (
        (credits.freeze, (db, u.id, -1, 1)),
        (credits.settle, (db, u.id, -1, 0, 1)),
        (credits.settle, (db, u.id, 1, -1, 1)),
        (credits.refund, (db, u.id, -1, 1)),
        (credits.unlock, (db, u.id, -1, 1)),
    ):
        with pytest.raises(ValueError):
            fn(*args)

    with pytest.raises(ValueError):
        credits.consume(db, u.id, -1, biz_type="reverse")
    with pytest.raises(ValueError):
        credits.refund_consumed(db, u.id, -1, biz_type="reverse")

    db.refresh(u)
    assert (u.balance_credits, u.frozen_credits) == (50, 10)
