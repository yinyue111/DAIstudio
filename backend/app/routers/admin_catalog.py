"""Admin catalog compatibility facade and split-router composition."""
from __future__ import annotations

from fastapi import APIRouter

from ..services import audit, gateway  # noqa: F401 - legacy monkeypatch surface
from . import admin_catalog_model_routes as _model_routes
from . import admin_catalog_model_versions as _model_versions
from . import admin_catalog_tool_versions as _tool_versions
from . import admin_catalog_tools as _tools
from .admin_catalog_shared import (  # noqa: F401
    _admin_tool_detail,
    _apply_tool_metadata,
    _commit,
    _disable_active_tool_version,
    _lock_tool,
    _locked_tool_versions,
    _publish_tool_metadata_change,
    _publish_tool_version_draft,
    _record_route_probe_outcome,
    _rollback_tool_version_copy,
    _route_for_model,
    _route_probe_error_code,
    _route_probe_utc,
    _serialize_tool_version,
    _tool_version_row,
    _validated_tool_metadata,
)

router = APIRouter()
_SPLIT_ROUTE_MODULES = (_model_routes, _model_versions, _tools, _tool_versions)
for _module in _SPLIT_ROUTE_MODULES:
    router.include_router(_module.router)
    for _route in _module.router.routes:
        _endpoint = _route.endpoint
        _endpoint.__module__ = __name__
        globals().setdefault(_endpoint.__name__, _endpoint)
