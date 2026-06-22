"""Public (authenticated) runtime config for the frontend: platform defaults,
enabled models + their credit costs, and whether we're in mock mode."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..config import settings
from ..db import get_db
from ..deps import get_current_user
from ..models import User
from ..services.config_store import (
    DEFAULT_SETTINGS,
    get_all_model_configs,
    get_bool_setting,
    get_setting,
)
from ..services.image_options import IMAGE_SIZES
from ..services.model_gateway_config import runtime_config_for_model
from ..services.video_analysis import (
    DEFAULT_VIDEO_ANALYSIS_PRESET,
    max_frame_count,
    preset_options,
)

router = APIRouter(prefix="/api", tags=["config"])


@router.get("/config")
def get_config(db: Session = Depends(get_db), _: User = Depends(get_current_user)):
    defaults = {k: get_setting(db, k) for k in DEFAULT_SETTINGS}
    all_models = list(get_all_model_configs(db))
    models = {
        m.use: {
            "cost_credits": m.cost_credits,
            "unlock_cost": m.unlock_cost,
            "enabled": m.enabled,
            "preview_cost": int((m.extra or {}).get("preview_cost", max(1, int(m.cost_credits or 0) // 10)))
            if m.use == "video" else None,
            "final_cost": int(m.cost_credits or 0),
        }
        for m in all_models
    }
    vision_cost = int(models.get("vision", {}).get("cost_credits") or 0)
    gateway_modes = {}
    for m in all_models:
        cfg = runtime_config_for_model(m, m.use)
        gateway_modes[m.use] = {"mock_mode": settings.mock_mode or not cfg.configured}
    return {
        "defaults": defaults,
        "features": {
            "sms_auth_enabled": get_bool_setting(db, "sms_auth_enabled", False),
            "payment_enabled": get_bool_setting(db, "payment_enabled", False),
        },
        "models": models,
        "image_sizes": list(IMAGE_SIZES),
        "image_size_max_dim": settings.max_image_dim,
        "image_n_max": settings.max_image_n,
        "video_duration_max_seconds": settings.max_video_seconds,
        "reverse": {
            "image_cost": vision_cost,
            "video_default_preset": DEFAULT_VIDEO_ANALYSIS_PRESET,
            "video_frame_count": max_frame_count(DEFAULT_VIDEO_ANALYSIS_PRESET),
            "video_max_cost": vision_cost * max_frame_count(DEFAULT_VIDEO_ANALYSIS_PRESET),
            "video_presets": preset_options(vision_cost),
        },
        "mock_mode": settings.effective_mock_mode,
        "gateways": {
            "vision": gateway_modes.get("vision", {"mock_mode": settings.effective_mock_mode}),
            "image": gateway_modes.get("image", {"mock_mode": settings.effective_mock_mode}),
            "video": gateway_modes.get("video", {"mock_mode": settings.effective_video_mock}),
        },
    }
