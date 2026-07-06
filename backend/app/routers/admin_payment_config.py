"""Admin payment package and provider configuration endpoints."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from ..config import settings as app_config
from ..db import get_db
from ..deps import get_client_ip, require_admin
from ..models import User
from ..schemas import (
    PaymentPackageDisableIn,
    PaymentPackageIn,
    PaymentPackageOut,
    PaymentProviderConfigIn,
    PaymentProviderConfigOut,
)
from ..services import audit, payment_config
from .admin_helpers import payment_provider_audit_snapshot as _payment_provider_audit_snapshot

router = APIRouter()


@router.get("/payments/packages", response_model=list[PaymentPackageOut])
def admin_payment_packages(db: Session = Depends(get_db), _: User = Depends(require_admin)):
    payment_config.seed_defaults(db)
    return [
        payment_config.package_to_dict(p)
        for p in payment_config.list_packages(db, enabled_only=False)
    ]


@router.post("/payments/packages", response_model=PaymentPackageOut)
def admin_upsert_payment_package(
    body: PaymentPackageIn,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
    request: Request = None,
):
    row = payment_config.upsert_package(db, body.model_dump())
    audit.log(
        db,
        user_id=admin.id,
        action="upsert_payment_package",
        biz_type="payment",
        ip=get_client_ip(request) if request else None,
        detail=payment_config.package_to_dict(row),
    )
    return payment_config.package_to_dict(row)


@router.delete("/payments/packages/{package_id}")
def admin_disable_payment_package(
    package_id: str,
    body: PaymentPackageDisableIn,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
    request: Request = None,
):
    try:
        payment_config.delete_package(db, package_id)
    except payment_config.PaymentConfigError as e:
        raise HTTPException(404, str(e)) from e
    audit.log(
        db,
        user_id=admin.id,
        action="disable_payment_package",
        biz_type="payment",
        ip=get_client_ip(request) if request else None,
        detail={"package_id": package_id},
    )
    return {"ok": True}


@router.get("/payments/providers", response_model=list[PaymentProviderConfigOut])
def admin_payment_providers(db: Session = Depends(get_db), _: User = Depends(require_admin)):
    return payment_config.list_providers_with_runtime(db)


@router.put("/payments/providers/{provider}", response_model=PaymentProviderConfigOut)
def admin_save_payment_provider(
    provider: str,
    body: PaymentProviderConfigIn,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
    request: Request = None,
):
    if body.provider != provider:
        raise HTTPException(400, "路径支付渠道与请求体不一致")
    if body.mode == "mock" and not app_config.debug and body.enabled:
        raise HTTPException(400, "生产环境不允许启用 mock 支付")
    before = _payment_provider_audit_snapshot(payment_config.get_provider(db, provider))
    try:
        row = payment_config.save_provider(
            db,
            provider=provider,
            enabled=body.enabled,
            mode=body.mode,
            public_config=body.public_config,
            secret_config=body.secret_config,
        )
    except payment_config.PaymentConfigError as e:
        raise HTTPException(400, str(e)) from e
    audit.log(
        db,
        user_id=admin.id,
        action="update_payment_provider",
        biz_type="payment",
        ip=get_client_ip(request) if request else None,
        detail={
            "provider": provider,
            "before": before,
            "after": _payment_provider_audit_snapshot(row),
            "secret_keys_changed": sorted(
                key
                for key, value in (body.secret_config or {}).items()
                if key in payment_config.SECRET_FIELDS.get(provider, set())
                and str(value or "").strip()
                and str(value).strip() not in {"__keep__", "已配置"}
            ),
        },
    )
    return payment_config.provider_out(row)


@router.get("/payments/config")
def admin_payment_config(db: Session = Depends(get_db), _: User = Depends(require_admin)):
    return payment_config.export_public_status(db)
