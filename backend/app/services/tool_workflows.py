"""Compatibility facade for the durable tool workflow engine."""
from __future__ import annotations

from . import workflow_compensation as _compensation
from . import workflow_contracts as _contracts
from . import workflow_lifecycle as _lifecycle
from . import workflow_node_execution as _node_execution
from . import workflow_run_factory as _run_factory
from . import workflow_serialization as _serialization
from .compat_facade import install_assignment_forwarding as _install_assignment_forwarding
from .workflow_compensation import (  # noqa: F401
    _compensation_context,
    _complete_compensation,
    _run_one_compensation,
)
from .workflow_contracts import (  # noqa: F401
    _COMPENSATION_HANDLERS,
    _NODE_HANDLERS,
    DEFAULT_NODE_LEASE_SECONDS,
    TERMINAL_NODE_STATUSES,
    TERMINAL_RUN_STATUSES,
    NodeExecutionContext,
    NodeExecutionResult,
    NodeHandler,
    WorkflowConflict,
    WorkflowNotFound,
    WorkflowServiceError,
    has_compensation_handler,
    has_node_handler,
    register_compensation_handler,
    register_node_handler,
    unregister_compensation_handler,
    unregister_node_handler,
    utcnow,
)
from .workflow_lifecycle import (  # noqa: F401
    _finish_success,
    _schedule_lease_wakeup,
    _terminalize_or_compensate,
    complete_external_node,
    enqueue_run,
    ensure_orphaned_run_dispatches,
    fail_active_node,
    request_cancel,
    retry_node,
    review_node,
    run_workflow,
)
from .workflow_node_execution import (  # noqa: F401
    _apply_execution_result,
    _apply_failed_result,
    _attempt_by_token,
    _claim_node,
    _deadline_reached,
    _execution_input,
    _lease_deadline,
    _recover_expired_claims,
    _refund_tool_run,
    _remaining_tool_reservation,
    _set_run_status,
    _settle_tool_run,
    _validated_compensation_result,
    _validated_handler_result,
    execute_claimed_node,
)
from .workflow_run_factory import (  # noqa: F401
    _REFERENCE_FIELDS,
    _canonical_json,
    _compiled_workflow_snapshot,
    _existing_idempotent_run,
    _fingerprint,
    _iso,
    _runtime_references,
    create_run,
)
from .workflow_serialization import (  # noqa: F401
    _node_attempts,
    _owned_run,
    _run_nodes,
    admin_summary,
    get_owned_run,
    list_owned_runs,
    serialize_attempt,
    serialize_node,
    serialize_run,
)

_CHILD_MODULES = (
    _contracts,
    _run_factory,
    _serialization,
    _node_execution,
    _compensation,
    _lifecycle,
)
_install_assignment_forwarding(__name__, _CHILD_MODULES)

__all__ = tuple(
    name
    for name in globals()
    if not name.startswith("__")
    and name not in {"_CHILD_MODULES", "_install_assignment_forwarding"}
)
