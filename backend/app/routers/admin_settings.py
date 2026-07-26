"""Admin platform settings, audit, and gateway status endpoints."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings as app_config
from ..db import get_db
from ..deps import get_client_ip, require_admin
from ..models import AuditLog, User
from ..schemas import AuditOut, SettingsIn
from ..services import audit, content_safety_providers, payment_config, payments, sms
from ..services.config_store import DEFAULT_SETTINGS, get_setting, set_settings
from .admin_helpers import IMAGE_SIZE_RE as _IMAGE_SIZE_RE
from .admin_helpers import page as _page

router = APIRouter()

_OPENAI_COMPAT_4K_MAX_PIXELS = 3840 * 2160


def _image_max_pixels() -> int:
    max_dim = max(1, int(app_config.max_image_dim or 1))
    return min(_OPENAI_COMPAT_4K_MAX_PIXELS, max_dim * max_dim)


@router.get("/settings")
def get_settings(db: Session = Depends(get_db), _: User = Depends(require_admin)):
    return {k: get_setting(db, k) for k in DEFAULT_SETTINGS}


@router.put("/settings")
def put_settings(
    body: SettingsIn,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
    request: Request = None,
):
    changed = {}
    for key in DEFAULT_SETTINGS:
        val = getattr(body, key, None)
        if val is not None:
            if key == "image_size":
                m = _IMAGE_SIZE_RE.match(str(val))
                if not m:
                    raise HTTPException(400, f"默认图片尺寸非法(最大 {app_config.max_image_dim}px)")
                width = int(m.group(1))
                height = int(m.group(2))
                ratio = width / height if height else 0
                if not (
                    0 < width <= app_config.max_image_dim
                    and 0 < height <= app_config.max_image_dim
                    and width % 16 == 0
                    and height % 16 == 0
                    and width * height <= _image_max_pixels()
                    and (1 / 3) <= ratio <= 3
                ):
                    raise HTTPException(
                        400,
                        f"默认图片尺寸非法:最大边 {app_config.max_image_dim}px，总像素不超过 {_image_max_pixels()}，宽高需为 16 的倍数",
                    )
            changed[key] = val
    if changed.get("sms_auth_enabled") is True:
        sms_issues = sms.readiness_issues()
        if sms_issues:
            raise HTTPException(400, "短信验证码注册未就绪: " + "; ".join(sms_issues))
    if changed.get("media_moderation_enabled") is True:
        moderation_issues = content_safety_providers.readiness_issues()
        if moderation_issues:
            raise HTTPException(400, "媒体机器审核未就绪: " + "; ".join(moderation_issues))
    if changed.get("payment_enabled") is True:
        payment_status = payment_config.export_public_status(db)
        ready = any(
            provider.get("enabled")
            and provider.get("ready")
            and (
                provider.get("mode") != "mock"
                or payments.mock_payments_allowed()
            )
            for provider in payment_status.get("providers", [])
        )
        if not ready:
            raise HTTPException(400, "支付充值功能未就绪: 请先配置至少一个可用支付渠道")
    if changed:
        set_settings(db, changed)
    audit.log(
        db,
        user_id=admin.id,
        action="update_settings",
        biz_type="admin",
        ip=get_client_ip(request) if request else None,
        detail=changed,
    )
    return {k: get_setting(db, k) for k in DEFAULT_SETTINGS}


@router.get("/audit", response_model=list[AuditOut])
def list_audit(
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
    action: str | None = None,
    user_id: int | None = None,
    limit: int = 50,
    offset: int = 0,
):
    q = select(AuditLog).order_by(AuditLog.id.desc())
    if action:
        q = q.where(AuditLog.action == action)
    if user_id:
        q = q.where(AuditLog.user_id == user_id)
    limit, offset = _page(limit, offset, 200)
    q = q.limit(limit).offset(offset)
    return list(db.execute(q).scalars())


@router.get("/gateway")
def gateway_status(_: User = Depends(require_admin)):
    """Read-only view of gateway config. The api_key is masked; the full key
    never leaves the server."""
    def _mask(k: str) -> str:
        return (k[:6] + "…" + k[-4:]) if len(k) > 12 else ("已配置" if k else "")

    return {
        "base_url": app_config.gateway_base_url,
        "api_key_masked": _mask(app_config.gateway_api_key or ""),
        "mock_mode": app_config.effective_mock_mode,
        "timeout_seconds": app_config.gateway_timeout_seconds,
        "max_retries": app_config.gateway_max_retries,
        "image": {
            "timeout_seconds": app_config.image_gateway_timeout_seconds,
            "download_timeout_seconds": app_config.image_download_timeout_seconds,
            "parallelism": app_config.image_gateway_parallelism,
        },
        "video": {
            "base_url": app_config.video_base,
            "api_key_masked": _mask(app_config.video_key or ""),
            "format": app_config.video_gateway_format,
            "mock_mode": app_config.effective_video_mock,
            "submit_timeout_seconds": app_config.video_submit_timeout_seconds,
            "poll_max_seconds": app_config.video_poll_max_seconds,
            "poll_interval_seconds": app_config.video_poll_interval_seconds,
        },
    }
