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

    rows = db.execute(
        select(CreditTransaction).where(CreditTransaction.user_id == u.id).order_by(CreditTransaction.id)
    ).scalars().all()
    assert [(r.type, r.balance_delta, r.frozen_delta, r.balance_after, r.frozen_after) for r in rows] == [
        ("grant", 100, 0, 100, 0),
        ("freeze", -30, 30, 70, 30),
        ("settle", 10, -30, 80, 0),
        ("unlock", -5, 0, 75, 0),
        ("consume", -3, 0, 72, 0),
    ]
    settle = next(r for r in rows if r.type == "settle")
    assert settle.reserved_amount == 30
    assert settle.real_cost == 20


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

    with pytest.raises(credits.InsufficientCredits):
        credits.refund_consumed(db, u.id, 1, biz_type="reverse", biz_ref=3)
    db.refresh(u)
    assert (u.balance_credits, u.frozen_credits) == (50, 0)


def test_refund_consumed_cannot_exceed_same_biz_consumption():
    db = make_session()
    u = User(phone="13900000004", status="active", balance_credits=50, frozen_credits=0)
    db.add(u)
    db.commit()
    db.refresh(u)

    credits.consume(db, u.id, 10, biz_type="reverse", biz_ref=7)
    credits.consume(db, u.id, 5, biz_type="reverse", biz_ref=8)
    db.refresh(u)
    assert u.balance_credits == 35

    with pytest.raises(credits.InsufficientCredits):
        credits.refund_consumed(db, u.id, 11, biz_type="reverse", biz_ref=7)
    db.refresh(u)
    assert u.balance_credits == 35

    credits.refund_consumed(db, u.id, 10, biz_type="reverse", biz_ref=7)
    db.refresh(u)
    assert u.balance_credits == 45


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


def test_legacy_settle_note_still_counts_reserved_amount():
    db = make_session()
    u = User(phone="13900000005", status="active", balance_credits=85, frozen_credits=0)
    db.add(u)
    db.commit()
    db.refresh(u)
    db.add_all([
        CreditTransaction(
            user_id=u.id,
            type="freeze",
            change=-20,
            balance_after=80,
            biz_type="gen_task",
            biz_ref=11,
        ),
        CreditTransaction(
            user_id=u.id,
            type="settle",
            change=5,
            balance_after=85,
            biz_type="gen_task",
            biz_ref=11,
            note="reserved=20 real=15",
        ),
    ])
    db.commit()

    with pytest.raises(credits.InsufficientCredits):
        credits.refund(db, u.id, 1, biz_ref=11)
    db.refresh(u)
    assert (u.balance_credits, u.frozen_credits) == (85, 0)


def test_admin_deduct_writes_ledger_and_rejects_overdraft():
    db = make_session()
    u = User(phone="13900000006", status="active", balance_credits=0, frozen_credits=0)
    db.add(u)
    db.commit()
    db.refresh(u)
    credits.grant(db, u.id, 100, note="init")

    # 正常扣减:走 consume 流水,余额同步下降
    _, deducted = credits.deduct(db, u.id, 30, note="批量发放金额填错冲正")
    db.refresh(u)
    assert deducted == 30
    assert (u.balance_credits, u.frozen_credits) == (70, 0)

    # 余额不足默认拒绝(users.balance_credits 带非负约束,不允许负余额)
    with pytest.raises(credits.InsufficientCredits):
        credits.deduct(db, u.id, 999, note="超额冲正")
    db.refresh(u)
    assert u.balance_credits == 70

    # allow_partial 扣到 0 为止,并返回实际扣减值
    _, deducted = credits.deduct(db, u.id, 999, note="允许部分冲正", allow_partial=True)
    db.refresh(u)
    assert deducted == 70
    assert u.balance_credits == 0

    # 余额为 0 再部分扣减:实际扣减 0,不写多余流水
    _, deducted = credits.deduct(db, u.id, 5, note="再次冲正", allow_partial=True)
    assert deducted == 0

    # 非法金额
    with pytest.raises(ValueError):
        credits.deduct(db, u.id, 0, note="零金额")
    with pytest.raises(ValueError):
        credits.deduct(db, u.id, -1, note="负金额")

    rows = db.execute(
        select(CreditTransaction).where(CreditTransaction.user_id == u.id).order_by(CreditTransaction.id)
    ).scalars().all()
    assert [(r.type, r.balance_delta, r.biz_type) for r in rows] == [
        ("grant", 100, "admin"),
        ("consume", -30, "admin"),
        ("consume", -70, "admin"),
    ]
    # 流水恒等:sum(change) == balance
    total = db.execute(select(func.coalesce(func.sum(CreditTransaction.change), 0))).scalar()
    assert total == u.balance_credits == 0


def test_deduct_then_refund_consumed_reverses_cleanly():
    """支付退款场景:先按 payment biz 扣回,渠道失败后 refund_consumed 原样补回。"""
    db = make_session()
    u = User(phone="13900000007", status="active", balance_credits=200, frozen_credits=0)
    db.add(u)
    db.commit()
    db.refresh(u)

    credits.deduct(db, u.id, 120, note="payment_refund alipay R1", biz_type="payment", biz_ref=42)
    db.refresh(u)
    assert u.balance_credits == 80

    credits.refund_consumed(db, u.id, 120, biz_type="payment", biz_ref=42,
                            note="payment_refund_rollback R1")
    db.refresh(u)
    assert u.balance_credits == 200

    # 补回受同 biz 消费守卫约束,不能超额补回
    with pytest.raises(credits.InsufficientCredits):
        credits.refund_consumed(db, u.id, 1, biz_type="payment", biz_ref=42)


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
