"""Audit-log helpers for optional events and transaction-required governance."""
from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from ..db import SessionLocal
from ..models import AuditLog

logger = logging.getLogger("audit")


def log_required(
    db: Session,
    *,
    user_id: int | None,
    action: str,
    biz_type: str | None = None,
    biz_id: int | None = None,
    ip: str | None = None,
    detail: dict | None = None,
) -> AuditLog:
    """Append an audit row inside the caller's business transaction."""
    row = AuditLog(
        user_id=user_id,
        action=action,
        biz_type=biz_type,
        biz_id=biz_id,
        ip=ip,
        detail=detail,
    )
    db.add(row)
    db.flush()
    return row


def log(
    db: Session,
    *,
    user_id: int | None,
    action: str,
    biz_type: str | None = None,
    biz_id: int | None = None,
    ip: str | None = None,
    detail: dict | None = None,
) -> bool:
    del db  # audit logging must not commit or roll back caller transactions
    audit_db = SessionLocal()
    try:
        audit_db.add(
            AuditLog(
                user_id=user_id,
                action=action,
                biz_type=biz_type,
                biz_id=biz_id,
                ip=ip,
                detail=detail,
            )
        )
        audit_db.commit()
        return True
    except Exception as e:  # noqa: BLE001
        audit_db.rollback()
        logger.warning("audit log write failed action=%s biz_type=%s biz_id=%s: %s",
                       action, biz_type, biz_id, e)
        return False
    finally:
        audit_db.close()
