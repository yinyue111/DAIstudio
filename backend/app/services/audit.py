"""Tiny audit-log helper.

Audit is important, but it must never turn an already-committed business action
into a client-visible failure. Callers often log after changing balances,
enqueueing jobs, or updating admin state.
"""
from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from ..models import AuditLog

logger = logging.getLogger("audit")


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
    try:
        db.add(
            AuditLog(
                user_id=user_id,
                action=action,
                biz_type=biz_type,
                biz_id=biz_id,
                ip=ip,
                detail=detail,
            )
        )
        db.commit()
        return True
    except Exception as e:  # noqa: BLE001
        db.rollback()
        logger.warning("audit log write failed action=%s biz_type=%s biz_id=%s: %s",
                       action, biz_type, biz_id, e)
        return False
