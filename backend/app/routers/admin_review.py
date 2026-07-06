"""Admin task review and asset moderation endpoints."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import get_client_ip, require_admin
from ..models import AssetReport, GenAsset, GenTask, User
from ..schemas import (
    AdminAssetReportHandleIn,
    AdminTaskRefundIn,
    AdminTaskSettleIn,
    AssetReportOut,
)
from ..services import audit, credits, generation, safe_logging
from ..services.task_output import build_task_out
from .admin_helpers import age_minutes as _age_minutes
from .admin_helpers import page as _page
from .admin_helpers import setting_int as _setting_int

router = APIRouter()


@router.post("/tasks/{task_id}/refund_review", response_model=object)
def refund_needs_review_task(
    task_id: int,
    body: AdminTaskRefundIn,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
    request: Request = None,
):
    task = db.get(GenTask, task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    claimed = db.execute(
        update(GenTask)
        .where(GenTask.id == task_id, GenTask.status == generation.NEEDS_REVIEW)
        .values(status="running", phase="reconciling", error=None)
    ).rowcount
    if (claimed or 0) != 1:
        db.rollback()
        db.refresh(task)
        return build_task_out(db, task)
    db.refresh(task)
    if task.cost_frozen and task.cost_settled == 0:
        try:
            credits.refund(db, task.user_id, task.cost_frozen, biz_ref=task.id, commit=False)
        except credits.InsufficientCredits as e:
            db.rollback()
            raise HTTPException(400, str(e)) from e
    note = (body.note or "").strip()
    discarded_assets = db.execute(
        update(GenAsset)
        .where(GenAsset.task_id == task.id, GenAsset.moderation_status == "active")
        .values(moderation_status="takedown")
    ).rowcount
    task.status = "failed"
    task.error = note or "管理员对账后已退款"
    task.phase = None
    task.finished_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(task)
    audit.log(
        db,
        user_id=admin.id,
        action="refund_review_task",
        biz_type="gen_task",
        biz_id=task.id,
        ip=get_client_ip(request) if request else None,
        detail={"target_user_id": task.user_id, "note": note, "discarded_assets": int(discarded_assets or 0)},
    )
    return build_task_out(db, task)


@router.post("/tasks/{task_id}/settle_review", response_model=object)
def settle_needs_review_task(
    task_id: int,
    body: AdminTaskSettleIn,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
    request: Request = None,
):
    task = db.get(GenTask, task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    claimed = db.execute(
        update(GenTask)
        .where(GenTask.id == task_id, GenTask.status == generation.NEEDS_REVIEW)
        .values(status="running", phase="reconciling", error=None, finished_at=None)
    ).rowcount
    if (claimed or 0) != 1:
        db.rollback()
        db.refresh(task)
        return build_task_out(db, task)
    db.refresh(task)
    try:
        generation.admin_settle_needs_review_task(
            db,
            task,
            result_url=(body.result_url or "").strip() or None,
            external_task_id=(body.external_task_id or "").strip() or None,
        )
    except ValueError as e:
        db.rollback()
        raise HTTPException(400, str(e)) from e
    except Exception as e:  # noqa: BLE001
        db.rollback()
        raise HTTPException(400, f"补结果结算失败:{e}") from e
    db.refresh(task)
    note = (body.note or "").strip()
    audit.log(
        db,
        user_id=admin.id,
        action="settle_review_task",
        biz_type="gen_task",
        biz_id=task.id,
        ip=get_client_ip(request) if request else None,
        detail={
            "target_user_id": task.user_id,
            "result_url": safe_logging.redact_url_for_log(body.result_url or ""),
            "external_task_id": body.external_task_id,
            "note": note,
        },
    )
    return build_task_out(db, task)


@router.get("/tasks/review")
def list_review_tasks(
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
    limit: int = 50,
    offset: int = 0,
):
    limit, offset = _page(limit, offset, 200)
    sla_minutes = max(1, _setting_int(db, "review_task_sla_minutes", 30))
    rows = list(
        db.execute(
            select(GenTask)
            .where(GenTask.status == generation.NEEDS_REVIEW)
            .order_by(GenTask.id.desc())
            .limit(limit)
            .offset(offset)
        ).scalars()
    )
    out = []
    for row in rows:
        item = build_task_out(db, row).model_dump()
        age_minutes = _age_minutes(row.created_at)
        item.update({
            "user_id": row.user_id,
            "phase": row.phase,
            "external_task_id": row.external_task_id,
            "external_submitted_at": row.external_submitted_at,
            "video_request_id": (row.params or {}).get("_video_request_id"),
            "has_local_results": (
                generation.image_review_has_local_results(row)
                or generation.video_review_has_local_results(row)
            ),
            "age_minutes": age_minutes,
            "review_sla_minutes": sla_minutes,
            "review_overdue": age_minutes is not None and age_minutes >= sla_minutes,
        })
        out.append(item)
    return out


@router.get("/asset-reports", response_model=list[AssetReportOut])
def list_asset_reports(
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
    status: str = "open",
    limit: int = 50,
    offset: int = 0,
):
    if status not in {"open", "dismissed", "takedown", "all"}:
        raise HTTPException(400, "举报状态非法")
    limit, offset = _page(limit, offset, 200)
    q = select(AssetReport).order_by(AssetReport.id.desc())
    if status != "all":
        q = q.where(AssetReport.status == status)
    return list(db.execute(q.limit(limit).offset(offset)).scalars())


@router.post("/asset-reports/{report_id}/handle", response_model=AssetReportOut)
def handle_asset_report(
    report_id: int,
    body: AdminAssetReportHandleIn,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
    request: Request = None,
):
    report = db.get(AssetReport, report_id)
    if not report:
        raise HTTPException(404, "举报不存在")
    if report.status != "open":
        return report
    note = (body.note or "").strip()
    if body.action == "takedown":
        asset = db.get(GenAsset, report.asset_id) if report.asset_id else None
        if asset:
            asset.moderation_status = "takedown"
        report.status = "takedown"
    else:
        report.status = "dismissed"
    report.handled_by = admin.id
    report.handle_note = note or None
    report.handled_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(report)
    audit.log(
        db,
        user_id=admin.id,
        action="handle_asset_report",
        biz_type="asset_report",
        biz_id=report.id,
        ip=get_client_ip(request) if request else None,
        detail={
            "asset_id": report.asset_id,
            "action": body.action,
            "status": report.status,
            "note": note,
        },
    )
    return report
