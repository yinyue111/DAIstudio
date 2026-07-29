"""Compatibility contracts for the admin, recipe, reporting, and payment splits."""
from __future__ import annotations

from app.routers import (
    admin_catalog,
    admin_catalog_model_routes,
    admin_catalog_model_versions,
    admin_catalog_tool_versions,
    admin_catalog_tools,
    admin_usage,
    recipes,
)
from app.services import (
    payment_notifications,
    payment_reconciliation,
    payment_refunds,
    payment_transport,
    payments,
    recipe_lifecycle,
    reverse_usage_read_model,
)


def test_recipe_and_usage_helpers_remain_available_from_legacy_routers():
    assert recipes._public_recipe_payload is recipe_lifecycle._public_recipe_payload
    assert recipes._serialize is recipe_lifecycle._serialize
    assert recipes._usage_stats_map is recipe_lifecycle._usage_stats_map
    assert admin_usage._evidence_coverage is reverse_usage_read_model._evidence_coverage


def test_admin_catalog_facade_reexports_split_route_endpoints():
    assert admin_catalog.model_routes is admin_catalog_model_routes.model_routes
    assert admin_catalog.model_versions is admin_catalog_model_versions.model_versions
    assert admin_catalog.admin_tools is admin_catalog_tools.admin_tools
    assert admin_catalog.tool_versions is admin_catalog_tool_versions.tool_versions
    route_modules = (
        admin_catalog_model_routes,
        admin_catalog_model_versions,
        admin_catalog_tools,
        admin_catalog_tool_versions,
    )
    assert all(
        route.endpoint.__module__ == admin_catalog.__name__
        for module in route_modules
        for route in module.router.routes
    )


def test_payment_facade_reexports_domain_implementations():
    assert payments._provider_code_url is payment_transport._provider_code_url
    assert payments._query_provider_order is payment_transport._query_provider_order
    assert payments.reconcile_pending_orders is payment_reconciliation.reconcile_pending_orders
    assert payments.verify_alipay_notify is payment_notifications.verify_alipay_notify
    assert payments.refund_order is payment_refunds.refund_order


def test_payment_facade_assignment_forwarding_preserves_monkeypatch_surface(monkeypatch):
    def replacement(*_args, **_kwargs):
        return {"status": "unknown"}

    monkeypatch.setattr(payments, "_query_provider_order", replacement)
    assert payment_transport._query_provider_order is replacement
    assert payment_reconciliation._query_provider_order is replacement

    monkeypatch.setattr(payments, "_provider_refund", replacement)
    assert payment_refunds._provider_refund is replacement

    monkeypatch.setattr(payments, "_rsa_sha256_verify", replacement)
    assert payment_transport._rsa_sha256_verify is replacement
    assert payment_notifications._rsa_sha256_verify is replacement
