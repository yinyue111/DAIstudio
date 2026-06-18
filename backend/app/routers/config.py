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
    get_setting,
)
from ..services.image_options import IMAGE_SIZES

router = APIRouter(prefix="/api", tags=["config"])


@router.get("/config")
def get_config(db: Session = Depends(get_db), _: User = Depends(get_current_user)):
    defaults = {k: get_setting(db, k) for k in DEFAULT_SETTINGS}
    models = {
        m.use: {
            "model_id": m.model_id,
            "cost_credits": m.cost_credits,
            "unlock_cost": m.unlock_cost,
            "enabled": m.enabled,
            "preview_cost": int((m.extra or {}).get("preview_cost", max(1, int(m.cost_credits or 0) // 10)))
            if m.use == "video" else None,
            "final_cost": int(m.cost_credits or 0),
        }
        for m in get_all_model_configs(db)
    }
    return {
        "defaults": defaults,
        "models": models,
        "image_sizes": list(IMAGE_SIZES),
        "image_size_max_dim": settings.max_image_dim,
        "image_n_max": settings.max_image_n,
        "mock_mode": settings.effective_mock_mode,
    }
