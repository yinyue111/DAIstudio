"""Admin backoffice router aggregator."""
from __future__ import annotations

from fastapi import APIRouter, Depends

from ..config import settings as _settings
from . import admin_models as _admin_models
from . import admin_payment_config as _admin_payment_config
from . import admin_quota as _admin_quota
from . import admin_review as _admin_review
from . import admin_settings as _admin_settings
from . import admin_update as _admin_update
from . import admin_usage as _admin_usage
from . import admin_users as _admin_users
from . import admin_whitelist as _admin_whitelist
from .admin_helpers import require_admin_rate_limit as _require_admin_rate_limit

router = APIRouter(prefix="/api/admin", tags=["admin"])
router.dependencies.append(Depends(_require_admin_rate_limit))

# Backward-compatible module attribute for older tests/operator scripts that
# monkeypatch app.routers.admin.app_config.
app_config = _settings


router.include_router(_admin_whitelist.router)
router.include_router(_admin_review.router)
router.include_router(_admin_users.router)
router.include_router(_admin_quota.router)
router.include_router(_admin_usage.router)
router.include_router(_admin_models.router)
router.include_router(_admin_settings.router)
router.include_router(_admin_update.router)
router.include_router(_admin_payment_config.router)
