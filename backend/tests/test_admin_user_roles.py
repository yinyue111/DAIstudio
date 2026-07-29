"""管理员生命周期管理（提权 / 降权）端点测试。"""
import pytest
from fastapi import HTTPException

from app.db import SessionLocal
from app.models import AuditLog, User
from app.routers.admin_users import _ensure_remaining_active_admin


def _latest_role_audit(target_id):
    db = SessionLocal()
    try:
        return (
            db.query(AuditLog)
            .filter(AuditLog.action == "set_user_role", AuditLog.biz_id == target_id)
            .order_by(AuditLog.id.desc())
            .first()
        )
    finally:
        db.close()


def _get_user(uid):
    db = SessionLocal()
    try:
        return db.get(User, uid)
    finally:
        db.close()


def test_promote_user_to_admin_writes_audit(client, make_user, auth):
    admin_id = make_user("13700000001", balance=0, admin=True)
    target = make_user("13700000002", balance=0)
    h = auth("13700000001")

    r = client.patch(f"/api/admin/users/{target}/role", json={"is_admin": True}, headers=h)
    assert r.status_code == 200, r.text
    assert _get_user(target).is_admin is True

    row = _latest_role_audit(target)
    assert row is not None
    assert row.user_id == admin_id
    assert row.biz_type == "admin"
    assert row.detail["is_admin_before"] is False
    assert row.detail["is_admin_after"] is True


def test_demote_other_admin_writes_audit_and_revokes_token(client, make_user, auth):
    make_user("13700000003", balance=0, admin=True)
    target = make_user("13700000004", balance=0, admin=True)
    h = auth("13700000003")
    target_h = auth("13700000004")

    r = client.patch(f"/api/admin/users/{target}/role", json={"is_admin": False}, headers=h)
    assert r.status_code == 200, r.text
    assert _get_user(target).is_admin is False
    # 角色变化会 bump token_version，旧令牌立即失效
    assert client.get("/api/me", headers=target_h).status_code == 401

    row = _latest_role_audit(target)
    assert row is not None
    assert row.detail["is_admin_before"] is True
    assert row.detail["is_admin_after"] is False


def test_admin_cannot_demote_self(client, make_user, auth):
    self_id = make_user("13700000005", balance=0, admin=True)
    make_user("13700000006", balance=0, admin=True)
    h = auth("13700000005")

    r = client.patch(f"/api/admin/users/{self_id}/role", json={"is_admin": False}, headers=h)
    assert r.status_code == 400
    assert "当前管理员" in r.text
    assert _get_user(self_id).is_admin is True


def test_sole_active_admin_cannot_demote_or_disable_self(client, make_user, auth):
    """最后一个可用管理员既不能给自己降权，也不能禁用自己，管理员数不会降为零。"""
    sole = make_user("13700000007", balance=0, admin=True)
    h = auth("13700000007")

    db = SessionLocal()
    try:
        others = list(
            db.query(User).filter(
                User.is_admin.is_(True), User.status == "active", User.id != sole,
            )
        )
        other_ids = [u.id for u in others]
        for u in others:
            u.status = "disabled"
        db.commit()
    finally:
        db.close()

    try:
        r1 = client.patch(f"/api/admin/users/{sole}/role", json={"is_admin": False}, headers=h)
        assert r1.status_code == 400
        assert "当前管理员" in r1.text

        r2 = client.patch(
            f"/api/admin/users/{sole}/status", json={"status": "disabled"}, headers=h,
        )
        assert r2.status_code == 400
        assert "当前管理员" in r2.text

        assert _get_user(sole).is_admin is True
        assert _get_user(sole).status == "active"
    finally:
        db = SessionLocal()
        try:
            for uid in other_ids:
                db.get(User, uid).status = "active"
            db.commit()
        finally:
            db.close()


def test_remaining_active_admin_invariant_rejects_zero_admins(make_user):
    """降权与停用共用的不变式：目标之外没有其他可用管理员时必须拒绝。"""
    sole = make_user("13700000008", balance=0, admin=True)

    db = SessionLocal()
    try:
        others = list(
            db.query(User).filter(
                User.is_admin.is_(True), User.status == "active", User.id != sole,
            )
        )
        other_ids = [u.id for u in others]
        for u in others:
            u.status = "disabled"
        db.commit()

        try:
            with pytest.raises(HTTPException) as ei:
                _ensure_remaining_active_admin(db, db.get(User, sole))
            assert "至少需要保留一个可用管理员账号" in str(ei.value.detail)
        finally:
            for uid in other_ids:
                db.get(User, uid).status = "active"
            db.commit()
    finally:
        db.close()


def test_non_admin_cannot_change_role(client, make_user, auth):
    make_user("13700000009", balance=0)
    target = make_user("13700000010", balance=0)
    h = auth("13700000009")

    r = client.patch(f"/api/admin/users/{target}/role", json={"is_admin": True}, headers=h)
    assert r.status_code == 403
    assert _get_user(target).is_admin is False


def test_cannot_promote_inactive_user(client, make_user, auth):
    make_user("13700000011", balance=0, admin=True)
    target = make_user("13700000012", balance=0)
    db = SessionLocal()
    try:
        db.get(User, target).status = "pending"
        db.commit()
    finally:
        db.close()
    h = auth("13700000011")

    r = client.patch(f"/api/admin/users/{target}/role", json={"is_admin": True}, headers=h)
    assert r.status_code == 400
    assert "启用状态" in r.text
    assert _get_user(target).is_admin is False


def test_role_change_missing_user_returns_404(client, make_user, auth):
    make_user("13700000013", balance=0, admin=True)
    h = auth("13700000013")
    r = client.patch("/api/admin/users/999999/role", json={"is_admin": True}, headers=h)
    assert r.status_code == 404
    assert "用户不存在" in r.text
