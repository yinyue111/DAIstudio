"""Permission boundary tests for the catalog-management admin surface."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute
from sqlalchemy import event

from app.db import engine
from app.deps import require_admin
from app.main import app
from app.routers import admin, admin_catalog, admin_models, admin_recipes
from app.services import gateway

_SCOPED_MODULES = {
    admin_models.__name__,
    admin_catalog.__name__,
    admin_recipes.__name__,
}


def _effective_routes(routes) -> Iterator[Any]:
    """Flatten both legacy APIRoutes and FastAPI's lazy included routers."""
    for route in routes:
        effective_candidates = getattr(route, "effective_candidates", None)
        if callable(effective_candidates):
            yield from _effective_routes(effective_candidates())
        elif isinstance(route, APIRoute) or hasattr(route, "dependant"):
            yield route


def _scoped_routes() -> tuple[Any, ...]:
    return tuple(
        route
        for route in _effective_routes(app.routes)
        if route.path.startswith("/api/admin")
        and route.endpoint.__module__ in _SCOPED_MODULES
    )


def _route_cases() -> tuple[tuple[Any, str], ...]:
    return tuple(
        (route, method)
        for route in _scoped_routes()
        for method in sorted(route.methods - {"HEAD", "OPTIONS"})
    )


_ROUTE_CASES = _route_cases()
_ROUTE_CASE_IDS = tuple(f"{method} {route.path}" for route, method in _ROUTE_CASES)


def _dependency_graph(root: Dependant) -> Iterator[Dependant]:
    yield root
    for dependency in root.dependencies:
        yield from _dependency_graph(dependency)


def _request_path(route: Any) -> str:
    values = {
        name: "capability" if name == "kind" else "1"
        for name in route.param_convertors
    }
    return route.path_format.format(**values)


def _is_dml(context, statement: str) -> bool:
    compiled = getattr(context, "compiled", None)
    sql_statement = getattr(compiled, "statement", None)
    if sql_statement is not None and any(
        bool(getattr(sql_statement, flag, False))
        for flag in ("is_insert", "is_update", "is_delete")
    ):
        return True
    return statement.lstrip().split(None, 1)[0].upper() in {
        "INSERT",
        "UPDATE",
        "DELETE",
        "REPLACE",
    }


def test_scoped_admin_route_discovery_covers_each_router_module():
    discovered_modules = {route.endpoint.__module__ for route in _scoped_routes()}
    assert discovered_modules == _SCOPED_MODULES
    assert _ROUTE_CASES


def test_admin_aggregator_declares_one_explicit_admin_boundary():
    dependency_calls = {
        dependency.dependency
        for dependency in admin.router.dependencies
    }
    assert require_admin in dependency_calls


@pytest.mark.parametrize("route,method", _ROUTE_CASES, ids=_ROUTE_CASE_IDS)
def test_every_catalog_admin_route_recursively_requires_admin(route, method):
    del method
    dependency_calls = {
        dependency.call for dependency in _dependency_graph(route.dependant)
    }
    assert require_admin in dependency_calls


@pytest.mark.parametrize("route,method", _ROUTE_CASES, ids=_ROUTE_CASE_IDS)
def test_regular_user_is_denied_without_database_or_gateway_side_effects(
    route,
    method,
    client,
    make_user,
    auth,
    monkeypatch,
):
    make_user("13888888120", admin=False)
    headers = auth("13888888120")
    dml_statements: list[str] = []
    gateway_calls: list[tuple[tuple, dict]] = []

    def capture_dml(_conn, _cursor, statement, _parameters, context, _executemany):
        if _is_dml(context, statement):
            dml_statements.append(statement)

    def unexpected_gateway_call(*args, **kwargs):
        gateway_calls.append((args, kwargs))
        raise AssertionError("a rejected admin request reached the model gateway")

    monkeypatch.setattr(gateway, "list_models", unexpected_gateway_call)
    event.listen(engine, "before_cursor_execute", capture_dml)
    try:
        request_kwargs = {"headers": headers}
        if method not in {"GET", "HEAD"}:
            request_kwargs["json"] = {}
        response = client.request(method, _request_path(route), **request_kwargs)
    finally:
        event.remove(engine, "before_cursor_execute", capture_dml)

    assert response.status_code == 403, response.text
    assert dml_statements == []
    assert gateway_calls == []
