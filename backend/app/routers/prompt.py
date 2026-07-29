"""Compatibility facade and route assembly for reverse-prompt APIs."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from ..deps import get_current_user
from ..models import User
from ..schemas import PromptOptimizeIn
from ..services import image_evidence_analysis, video_audio, video_evidence_analysis
from ..services.compat_facade import (
    install_assignment_forwarding as _install_assignment_forwarding,
)
from . import prompt_reverse_batches as _batches
from . import prompt_reverse_feedback as _feedback
from . import prompt_reverse_operations as _operations
from . import prompt_reverse_shots as _shots
from . import prompt_shared as _shared

router = APIRouter(prefix="/api/prompt", tags=["prompt"])


@router.get("/reverse-analyzers/status")
def reverse_analyzer_status(
    _user: User = Depends(get_current_user),
):
    return {
        "image": image_evidence_analysis.analyzer_health(),
        "video": video_evidence_analysis.analyzer_health(),
        "audio": video_audio.analyzer_health(),
    }


@router.post("/optimize", status_code=410)
def optimize_prompt_text(
    _body: PromptOptimizeIn,
    _user: User = Depends(get_current_user),
):
    raise HTTPException(
        status_code=410,
        detail={
            "code": "PROMPT_OPTIMIZATION_MOVED",
            "message": (
                "旧提示词优化端点已退役，请先创建统一报价，"
                "再通过 Studio 提示词优化建议流程执行。"
            ),
            "quote_endpoint": "/api/quotes",
            "quote_kind": "prompt_optimization",
            "proposal_endpoint": "/api/studio/prompt-optimizations",
        },
    )


# Preserve the original route registration order while implementation lives in
# domain routers. The order matters for predictable FastAPI route resolution.
router.include_router(_batches.router)
router.include_router(_operations.router)
router.include_router(_shots.router)
router.include_router(_feedback.router)
router.include_router(_operations.lifecycle_router)
router.include_router(_operations.legacy_router)

_CHILD_MODULES = (_shared, _batches, _operations, _shots, _feedback)
_REEXPORT_EXCLUDED = {"router", "lifecycle_router", "legacy_router"}
for _module in _CHILD_MODULES:
    for _name, _value in vars(_module).items():
        if _name.startswith("__") or _name in _REEXPORT_EXCLUDED:
            continue
        globals().setdefault(_name, _value)

_install_assignment_forwarding(__name__, _CHILD_MODULES)

__all__ = tuple(
    name
    for name in globals()
    if not name.startswith("__")
    and name
    not in {
        "_CHILD_MODULES",
        "_REEXPORT_EXCLUDED",
        "_install_assignment_forwarding",
        "_module",
        "_name",
        "_value",
    }
)
