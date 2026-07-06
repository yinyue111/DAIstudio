"""Low-risk API v2 endpoints.

This router is intentionally small while the v2 contract is being introduced.
Existing /api routes remain the production surface.
"""
from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/api/v2", tags=["v2"])


@router.get("/health")
def health_v2():
    return {"ok": True, "version": "v2"}


@router.get("/manifest")
def manifest_v2():
    return {
        "version": "v2",
        "status": "experimental",
        "endpoints": [
            {
                "path": "/api/v2/health",
                "method": "GET",
                "stability": "stable",
                "description": "API v2 liveness probe.",
            },
            {
                "path": "/api/v2/manifest",
                "method": "GET",
                "stability": "experimental",
                "description": "Machine-readable v2 capability manifest.",
            },
        ],
        "notes": [
            "Production generation APIs remain under /api until a v2 contract is explicitly published.",
        ],
    }
