"""Compatibility contracts for services split behind legacy module facades."""
from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

from app.services import (
    fetcher,
    gateway,
    gateway_prompt_validation,
    gateway_prompt_visual,
    gateway_prompting,
    generation_quote_generation,
    generation_quotes,
    generation_video_polling,
    generation_video_submit,
    payments,
    reproduction_assessment,
    reproduction_correction,
    reverse_lifecycle,
    reverse_operation_records,
    reverse_operations,
    reverse_reaper,
    reverse_result_processing,
    reverse_revision_feedback,
    reverse_revision_reanalysis,
    reverse_revision_retry,
    reverse_revision_timeline,
    reverse_runner,
    reverse_settlement,
    tool_workflows,
    video_evidence_analysis,
    video_evidence_motion,
    video_evidence_semantic,
)

APP_DIR = Path(__file__).resolve().parents[1] / "app"
SERVICES_DIR = APP_DIR / "services"


def test_generation_service_modules_support_cold_imports():
    for module in (
        "app.services.generation_request",
        "app.services.generation_video_submit",
    ):
        completed = subprocess.run(
            [sys.executable, "-c", f"import {module}"],
            cwd=APP_DIR.parent,
            capture_output=True,
            text=True,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr

# Stable application entry points retained while implementation modules move.
# Private helpers remain covered by focused monkeypatch tests; this manifest is
# the supported service surface that must not disappear during decomposition.
FACADE_PUBLIC_CONTRACTS = (
    (
        fetcher,
        {
            "PlatformExtractor",
            "RenderedPage",
            "extract_assets",
            "extract_first_url",
            "parse_url",
        },
    ),
    (
        gateway,
        {
            "GatewayError",
            "ImageBatchResult",
            "ImageResponseDiagnostic",
            "ImageSubrequestFailure",
            "RuntimeGatewayConfig",
            "download_bytes",
            "download_bytes_limited",
            "download_to_path",
            "download_to_storage",
            "find_video_by_request_id",
            "gen_image",
            "list_models",
            "optimize_prompt",
            "poll_video",
            "reverse_prompt",
            "review_prompt_optimization",
            "submit_video",
            "video_result_download_headers",
        },
    ),
    (
        gateway_prompting,
        {
            "ReverseResultValidationError",
            "compose_final",
            "compose_visual_final_text",
            "constrain_video_shots_to_evidence",
            "normalize_video_shots",
            "parse_structured",
            "reverse_repair_template",
            "reverse_template",
            "sanitize_video_audio_evidence",
            "validate_reverse_result",
            "video_analysis_gaps",
        },
    ),
    (
        generation_quotes,
        {
            "consume_execution_quote",
            "consume_generation_quote",
            "create_asset_unlock_quote",
            "create_execution_quote",
            "create_generation_quote",
            "create_prompt_optimization_quote",
            "create_reverse_batch_quote",
            "create_reverse_quote",
            "create_workflow_quote",
            "find_idempotent_quote",
            "generation_quote_out",
            "lock_execution_quote",
            "lock_generation_quote",
            "normalized_price_breakdown",
            "quote_breakdown",
            "quote_item",
            "quoted_model_snapshot",
            "validate_asset_unlock_quote",
            "validate_generation_quote",
            "validate_prompt_optimization_quote",
            "validate_quote_snapshot_integrity",
            "validate_reverse_batch_quote",
            "validate_reverse_quote",
            "validate_workflow_quote",
            "workflow_request_fingerprint",
        },
    ),
    (
        reverse_operations,
        {
            "ReverseOperationConflict",
            "ReverseOperationInvalid",
            "ReverseOperationNotFound",
            "apply_result_revision",
            "attach_shot_reanalysis_context",
            "batch_operation_bodies",
            "batch_request_fingerprint",
            "batch_request_snapshot",
            "build_retry_operation_body",
            "confirm_cover",
            "create_batch",
            "create_legacy_operation",
            "create_operation",
            "create_quoted_batch",
            "create_quoted_operation",
            "create_result_revision",
            "edit_shot_timeline",
            "enqueue_operation",
            "fail_operation",
            "fail_queued_submission",
            "find_idempotent_batch",
            "find_idempotent_operation",
            "find_quoted_idempotent_batch",
            "find_quoted_idempotent_operation",
            "get_feedback",
            "get_owned_batch",
            "get_owned_operation",
            "list_owned_batches",
            "list_owned_operations",
            "list_result_revisions",
            "prepare_reverse_quote",
            "prepare_shot_generation",
            "quote_request_fingerprint",
            "reap_operations",
            "request_cancel",
            "request_fingerprint",
            "retry_operation",
            "reverse_pricing_snapshot",
            "reverse_request_snapshot",
            "reverse_template_snapshot",
            "run_operation",
            "sanitize_workspace_snapshot",
            "serialize_batch",
            "serialize_operation",
            "shot_reanalysis_body",
            "sync_batch_for_operation",
            "sync_batch_state",
            "transition_legacy_to_confirmation",
            "upsert_feedback",
            "validate_reverse_result",
        },
    ),
    (
        generation_video_submit,
        {
            "SourceVideoUrlUnavailable",
            "VideoSubmitVersionMismatch",
            "gateway_source_video_url",
            "hold_video_poll_for_reconciliation",
            "lineage_video_analysis_for_compile",
            "merge_lineage_video_analysis",
            "persist_video_submit_result",
            "poll_video_once",
            "recover_unknown_submit_by_request_id",
            "start_video_task",
            "submit_state_unknown",
            "video_persisted_params",
            "video_submit_params",
        },
    ),
    (
        video_evidence_analysis,
        {
            "CONTRACT_VERSION",
            "SEMANTIC_CONTRACT_VERSION",
            "analyze_video_evidence",
            "analyzer_health",
            "attach_audio_evidence_to_shots",
            "attach_evidence_to_shots",
            "build_cut_transition_evidence",
            "build_ocr_tracks",
            "cv2_motion_analysis",
            "http_semantic_provider",
        },
    ),
    (
        reproduction_assessment,
        {
            "ReproductionAssessmentConflict",
            "ReproductionAssessmentError",
            "ReproductionAssessmentNotFound",
            "create_assessment",
            "create_correction",
            "enqueue_assessment",
            "get_assessment",
            "list_assessments",
            "mark_enqueue_failed",
            "request_cancel",
            "run_assessment",
            "serialize_assessment",
            "serialize_correction",
        },
    ),
    (
        payments,
        {
            "PaymentError",
            "admin_search_orders",
            "admin_sync_order",
            "create_order",
            "get_order",
            "list_order_refunds",
            "list_packages",
            "list_user_orders",
            "mark_paid",
            "mock_payments_allowed",
            "public_config",
            "reconcile_pending_orders",
            "refund_order",
            "user_order",
            "verify_alipay_notify",
            "wechat_signature_valid",
        },
    ),
    (
        tool_workflows,
        {
            "NodeExecutionContext",
            "NodeExecutionResult",
            "WorkflowConflict",
            "WorkflowNotFound",
            "WorkflowServiceError",
            "admin_summary",
            "complete_external_node",
            "create_run",
            "enqueue_run",
            "ensure_orphaned_run_dispatches",
            "execute_claimed_node",
            "fail_active_node",
            "get_owned_run",
            "list_owned_runs",
            "register_compensation_handler",
            "register_node_handler",
            "request_cancel",
            "retry_node",
            "review_node",
            "run_workflow",
            "serialize_attempt",
            "serialize_node",
            "serialize_run",
            "unregister_compensation_handler",
            "unregister_node_handler",
        },
    ),
)

# Temporary dependency budgets. These are existing migration seams, not desired
# architecture: refactors may reduce the numbers but must not add new cycles.
FACADE_IMPORT_BUDGETS = {
    "fetcher": {
        "douyin.py": 1,
        "generic.py": 1,
        "jd.py": 1,
        "taobao.py": 1,
        "weixin.py": 1,
        "x_twitter.py": 1,
        "xiaohongshu.py": 1,
    },
    "gateway": {
        "gateway_download.py": 3,
        "gateway_image.py": 5,
        "gateway_reverse.py": 3,
        "gateway_transport.py": 2,
        "gateway_video.py": 7,
    },
    "gateway_prompting": {},
    "generation_quotes": {},
    "generation_video_submit": {},
    "payments": {
        "payment_notifications.py": 1,
        "payment_reconciliation.py": 1,
        "payment_refunds.py": 1,
        "payment_transport.py": 1,
    },
    "reproduction_assessment": {
        "reproduction_remediation.py": 1,
    },
    "reverse_operations": {
        "generation_quote_reverse.py": 5,
        "reverse_batches.py": 4,
        "reverse_quotes.py": 1,
        "reverse_runner.py": 7,
    },
    "tool_workflows": {
        "workflow_node_adapters.py": 1,
    },
    "video_evidence_analysis": {},
}
FACADE_CHILD_GLOBS = {
    "fetcher": ("fetcher/**/*.py",),
    "gateway": ("gateway_*.py",),
    "gateway_prompting": ("gateway_prompt_*.py",),
    "generation_quotes": ("generation_quote_*.py",),
    "generation_video_submit": ("generation_video_polling.py",),
    "payments": ("payment_*.py",),
    "reproduction_assessment": ("reproduction_*.py",),
    "reverse_operations": ("reverse_*.py", "generation_quote_reverse.py"),
    "tool_workflows": ("workflow_*.py",),
    "video_evidence_analysis": (
        "video_evidence_motion.py",
        "video_evidence_semantic.py",
    ),
}
FACADE_RELATIVE_LEVELS = {
    **{name: 1 for name in FACADE_CHILD_GLOBS},
    "fetcher": 2,
}
# Function spans are measured from the AST's first through last source line.
# Defaults are rounded ceilings above the current post-split maxima.
FOCUSED_FUNCTION_BUDGETS = {
    "reverse": {
        "patterns": ("services/reverse_operations.py", "services/reverse_*.py"),
        "default": 200,
        "overrides": {},
    },
    "gateway": {
        "patterns": ("services/gateway.py", "services/gateway_*.py"),
        "default": 200,
        "overrides": {},
    },
    "generation_quotes": {
        "patterns": (
            "services/generation_quotes.py",
            "services/generation_quote_*.py",
        ),
        "default": 140,
        "overrides": {},
    },
    "reproduction": {
        "patterns": (
            "services/reproduction_assessment.py",
            "services/reproduction_*.py",
        ),
        "default": 180,
        "overrides": {},
    },
    "fetcher": {
        "patterns": ("services/fetcher/**/*.py",),
        "default": 150,
        "overrides": {},
    },
    "workflows": {
        "patterns": ("services/tool_workflows.py", "services/workflow_*.py"),
        "default": 160,
        "overrides": {},
    },
    "prompt_optimization": {
        "patterns": (
            "services/prompt_optimization.py",
            "services/prompt_optimization_*.py",
        ),
        "default": 200,
        "overrides": {},
    },
    "payments": {
        "patterns": ("services/payments.py", "services/payment_*.py"),
        "default": 150,
        "overrides": {},
    },
    "generation_execution": {
        "patterns": ("services/generation_execution.py",),
        "default": 120,
        "overrides": {},
    },
    "video_services": {
        "patterns": (
            "services/generation_video_submit.py",
            "services/generation_video_polling.py",
            "services/video_evidence_analysis.py",
            "services/video_evidence_motion.py",
            "services/video_evidence_semantic.py",
            "services/video_prompt_compiler.py",
        ),
        "default": 180,
        "overrides": {},
    },
    "routers": {
        "patterns": (
            "routers/generate.py",
            "routers/recipes.py",
            "routers/prompt.py",
            "routers/prompt_*.py",
            "routers/admin_catalog.py",
            "routers/admin_catalog_*.py",
            "routers/admin_usage.py",
            "routers/projects.py",
        ),
        "default": 120,
        "overrides": {},
    },
}


def _service_trees():
    for path in SERVICES_DIR.rglob("*.py"):
        yield path, ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _facade_child_paths(facade_name: str) -> list[Path]:
    paths: set[Path] = set()
    for pattern in FACADE_CHILD_GLOBS[facade_name]:
        paths.update(path for path in SERVICES_DIR.glob(pattern) if path.is_file())
    facade_file = SERVICES_DIR / f"{facade_name}.py"
    package_facade = SERVICES_DIR / facade_name / "__init__.py"
    return sorted(paths - {facade_file, package_facade})


def _imports_facade(node: ast.AST, facade_name: str, relative_level: int) -> bool:
    absolute_name = f"app.services.{facade_name}"
    if isinstance(node, ast.Import):
        return any(
            alias.name == absolute_name or alias.name.startswith(f"{absolute_name}.")
            for alias in node.names
        )
    if not isinstance(node, ast.ImportFrom):
        return False
    module = node.module or ""
    if node.level == 0:
        return module == absolute_name or module.startswith(f"{absolute_name}.")
    if node.level != relative_level:
        return False
    if module == facade_name or module.startswith(f"{facade_name}."):
        return True
    return node.module is None and any(alias.name == facade_name for alias in node.names)


def _facade_import_counts(facade_name: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    relative_level = FACADE_RELATIVE_LEVELS[facade_name]
    for path in _facade_child_paths(facade_name):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        count = sum(
            1
            for node in ast.walk(tree)
            if _imports_facade(node, facade_name, relative_level)
        )
        if count:
            counts[path.name] = count
    return counts


def _router_import_weight(node: ast.AST) -> int:
    if isinstance(node, ast.Import):
        return sum(
            1
            for alias in node.names
            if alias.name == "app.routers" or alias.name.startswith("app.routers.")
        )
    if not isinstance(node, ast.ImportFrom):
        return 0
    module = node.module or ""
    relative_router = node.level >= 2 and (
        module == "routers" or module.startswith("routers.")
    )
    absolute_router = node.level == 0 and (
        module == "app.routers" or module.startswith("app.routers.")
    )
    return len(node.names) if relative_router or absolute_router else 0


def _service_router_import_counts() -> dict[str, int]:
    counts: dict[str, int] = {}
    for path, tree in _service_trees():
        count = sum(_router_import_weight(node) for node in ast.walk(tree))
        if count:
            counts[path.name] = count
    return counts


def _focused_paths(patterns: tuple[str, ...]) -> list[Path]:
    paths: set[Path] = set()
    for pattern in patterns:
        paths.update(path for path in APP_DIR.glob(pattern) if path.is_file())
    return sorted(paths)


def _function_spans(path: Path) -> list[tuple[int, str, int]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return [
        (
            (node.end_lineno or node.lineno) - node.lineno + 1,
            node.name,
            node.lineno,
        )
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]


def test_fetcher_facade_assignment_reaches_extractor_dependencies(monkeypatch):
    def replacement(*_args, **_kwargs):
        return "<html></html>"

    monkeypatch.setattr(fetcher, "_render_with_httpx", replacement)

    assert fetcher._fetch._render_with_httpx is replacement
    assert fetcher.generic._fetch._render_with_httpx is replacement


def test_quote_facade_assignment_reaches_split_quote_modules(monkeypatch):
    def replacement(*_args, **_kwargs):
        return None

    monkeypatch.setattr(generation_quotes, "create_execution_quote", replacement)

    assert generation_quote_generation.create_execution_quote is replacement


def test_reverse_facade_assignment_reaches_runner_dependencies(monkeypatch):
    replacement_gateway = object()
    replacement_credits = object()
    replacement_settings = object()
    replacement_safety_check = object()
    replacement_pricing = object()
    replacement_get_owned = object()
    replacement_create_quoted = object()
    monkeypatch.setattr(reverse_operations, "gateway", replacement_gateway)
    monkeypatch.setattr(reverse_operations, "credits", replacement_credits)
    monkeypatch.setattr(reverse_operations, "settings", replacement_settings)
    monkeypatch.setattr(reverse_operations, "assert_text_allowed", replacement_safety_check)
    monkeypatch.setattr(reverse_operations, "reverse_pricing_snapshot", replacement_pricing)
    monkeypatch.setattr(reverse_operations, "_get_owned", replacement_get_owned)
    monkeypatch.setattr(
        reverse_operations,
        "_create_quoted_operation",
        replacement_create_quoted,
    )

    assert reverse_runner.gateway is replacement_gateway
    assert reverse_result_processing.gateway is replacement_gateway
    assert reverse_runner.credits is replacement_credits
    assert reverse_settlement.credits is replacement_credits
    assert reverse_lifecycle.credits is replacement_credits
    assert reverse_operation_records.credits is replacement_credits
    assert reverse_runner.settings is replacement_settings
    assert reverse_reaper.settings is replacement_settings
    assert reverse_operation_records.assert_text_allowed is replacement_safety_check
    assert reverse_operation_records.reverse_pricing_snapshot is replacement_pricing
    assert reverse_revision_feedback._get_owned is replacement_get_owned
    assert reverse_revision_reanalysis._get_owned is replacement_get_owned
    assert reverse_revision_timeline._get_owned is replacement_get_owned
    assert reverse_revision_retry._create_quoted_operation is replacement_create_quoted


def test_video_submit_facade_assignment_reaches_polling_dependencies(monkeypatch):
    replacement_gateway = object()
    replacement_usage = object()
    replacement_mark_alive = object()
    replacement_persist = object()
    monkeypatch.setattr(generation_video_submit, "gateway", replacement_gateway)
    monkeypatch.setattr(generation_video_submit, "usage", replacement_usage)
    monkeypatch.setattr(generation_video_submit, "mark_poll_alive", replacement_mark_alive)
    monkeypatch.setattr(
        generation_video_submit,
        "persist_video_download_result",
        replacement_persist,
    )

    assert generation_video_polling.gateway is replacement_gateway
    assert generation_video_polling.usage is replacement_usage
    assert generation_video_polling.mark_poll_alive is replacement_mark_alive
    assert generation_video_polling.persist_video_download_result is replacement_persist


def test_video_evidence_facade_assignment_reaches_split_analyzers(monkeypatch):
    replacement_gateway = object()
    replacement_settings = object()
    replacement_time = object()
    replacement_cache = object()
    replacement_semantic = object()
    replacement_motion = object()
    monkeypatch.setattr(video_evidence_analysis, "gateway", replacement_gateway)
    monkeypatch.setattr(video_evidence_analysis, "settings", replacement_settings)
    monkeypatch.setattr(video_evidence_analysis, "time", replacement_time)
    monkeypatch.setattr(video_evidence_analysis, "_PROVIDER_HEALTH_CACHE", replacement_cache)
    monkeypatch.setattr(video_evidence_analysis, "http_semantic_provider", replacement_semantic)
    monkeypatch.setattr(video_evidence_analysis, "cv2_motion_analysis", replacement_motion)

    assert video_evidence_semantic.gateway is replacement_gateway
    assert video_evidence_semantic.settings is replacement_settings
    assert video_evidence_semantic.time is replacement_time
    assert video_evidence_semantic._PROVIDER_HEALTH_CACHE is replacement_cache
    assert video_evidence_semantic.http_semantic_provider is replacement_semantic
    assert video_evidence_motion.cv2_motion_analysis is replacement_motion


def test_prompting_facade_assignment_reaches_split_dependencies(monkeypatch):
    def replacement(*_args, **_kwargs):
        return "replacement"

    monkeypatch.setattr(
        gateway_prompting,
        "compose_visual_final_text",
        replacement,
    )

    assert gateway_prompt_visual.compose_visual_final_text is replacement
    assert gateway_prompt_validation.compose_visual_final_text is replacement


def test_reproduction_facade_keeps_correction_and_monkeypatch_contract(monkeypatch):
    assert reproduction_assessment.create_correction is reproduction_correction.create_correction
    assert (
        reproduction_assessment.serialize_correction
        is reproduction_correction.serialize_correction
    )

    replacement = object()
    monkeypatch.setattr(reproduction_assessment, "reverse_operations", replacement)
    assert reproduction_correction.reverse_operations is replacement


def test_split_facades_keep_legacy_symbols_available():
    assert gateway.RuntimeGatewayConfig
    assert gateway.MAX_REDIRECTS >= 0
    assert callable(gateway.parse_video_prompt)
    assert callable(gateway._is_openai_images_edit_path)
    assert callable(generation_quotes._normalized_client_request_id)
    assert callable(generation_quotes._active_tool_version)


def test_split_facades_keep_public_service_contracts_available():
    missing = {
        module.__name__: sorted(name for name in names if not hasattr(module, name))
        for module, names in FACADE_PUBLIC_CONTRACTS
        if any(not hasattr(module, name) for name in names)
    }

    assert not missing


def test_facade_child_import_debt_does_not_grow():
    for facade_name, budgets in FACADE_IMPORT_BUDGETS.items():
        actual = _facade_import_counts(facade_name)
        unexpected = sorted(set(actual) - set(budgets))
        over_budget = {
            filename: (actual[filename], budgets[filename])
            for filename in actual.keys() & budgets.keys()
            if actual[filename] > budgets[filename]
        }

        assert not unexpected, f"new {facade_name} facade imports: {unexpected}"
        assert not over_budget, f"{facade_name} facade import debt grew: {over_budget}"


def test_services_do_not_import_routers():
    actual = _service_router_import_counts()
    assert not actual, f"service modules must not import router modules: {actual}"


def test_focused_module_functions_stay_within_size_budgets():
    failures: dict[str, list[str]] = {}
    for group, config in FOCUSED_FUNCTION_BUDGETS.items():
        paths = _focused_paths(config["patterns"])
        assert paths, f"no files matched function budget group: {group}"
        default_budget = int(config["default"])
        overrides = config["overrides"]
        for path in paths:
            relative_path = path.relative_to(APP_DIR).as_posix()
            budget = int(overrides.get(relative_path, default_budget))
            oversized = [
                f"{name}@{line}={span}>{budget}"
                for span, name, line in _function_spans(path)
                if span > budget
            ]
            if oversized:
                failures[f"{group}:{relative_path}"] = sorted(oversized)

    assert not failures, f"focused function size budgets exceeded: {failures}"
