"""Stable HTTP route contract for router and service decomposition."""
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

from fastapi.routing import APIRoute, APIWebSocketRoute

from app.main import app

ROUTE_SNAPSHOT = Path(__file__).parent / "fixtures" / "api_routes.tsv"
WEBSOCKET_ROUTE_SNAPSHOT = Path(__file__).parent / "fixtures" / "api_websocket_routes.tsv"


def _effective_routes(routes: list[Any]) -> Iterator[Any]:
    """Expand FastAPI lazy included routers before inspecting route metadata."""
    for route in routes:
        effective_candidates = getattr(route, "effective_candidates", None)
        if callable(effective_candidates):
            yield from _effective_routes(effective_candidates())
        elif isinstance(route, APIRoute) or hasattr(route, "dependant"):
            yield route


def _route_contract() -> set[tuple[str, str, str]]:
    return {
        (method, route.path, route.name)
        for route in _effective_routes(app.routes)
        for method in (route.methods or set()) - {"HEAD", "OPTIONS"}
    }


def _snapshot_contract(path: Path) -> set[tuple[str, ...]]:
    return {
        tuple(line.split("\t"))
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    }


def _websocket_routes(routes: list[Any]) -> Iterator[APIWebSocketRoute]:
    """Traverse original routers because FastAPI excludes WS from HTTP candidates."""
    for route in routes:
        if isinstance(route, APIWebSocketRoute):
            yield route
            continue
        original_router = getattr(route, "original_router", None)
        nested_routes = getattr(original_router, "routes", None)
        if nested_routes is not None:
            yield from _websocket_routes(nested_routes)


def _websocket_route_contract() -> set[tuple[str, str]]:
    return {(route.path, route.name) for route in _websocket_routes(app.routes)}


def test_http_route_contract_matches_snapshot():
    expected = _snapshot_contract(ROUTE_SNAPSHOT)
    actual = _route_contract()

    missing = sorted(expected - actual)
    unexpected = sorted(actual - expected)

    assert not missing, f"HTTP routes disappeared or changed: {missing}"
    assert not unexpected, f"new HTTP routes need an intentional snapshot update: {unexpected}"


def test_websocket_route_contract_matches_snapshot():
    expected = _snapshot_contract(WEBSOCKET_ROUTE_SNAPSHOT)
    actual = _websocket_route_contract()

    missing = sorted(expected - actual)
    unexpected = sorted(actual - expected)

    assert not missing, f"WebSocket routes disappeared or changed: {missing}"
    assert not unexpected, f"new WebSocket routes need an intentional snapshot update: {unexpected}"
