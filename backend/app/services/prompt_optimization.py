"""Compatibility facade for Studio prompt optimization services."""

from __future__ import annotations

from ..config import settings  # noqa: F401 - preserved monkeypatch surface
from . import audit, credits, gateway, reverse_operations, usage  # noqa: F401
from . import prompt_optimization_compiler as _compiler_module
from . import prompt_optimization_decision as _decision_module
from . import prompt_optimization_lifecycle as _lifecycle_module
from . import prompt_optimization_read as _read_module
from . import prompt_optimization_request as _request_module
from . import prompt_optimization_validation as _validation_module
from .compat_facade import install_assignment_forwarding as _install_assignment_forwarding
from .prompt_optimization_compiler import (  # noqa: F401
    _COMPILER_VERSION,
    _IMAGE_PROMPT_COMPILER_VERSION,
    _QUOTED_TEXT_RE,
    _SEMANTIC_KEY_RE,
    _apply_compiler_contract,
    _candidate_payload,
    _constraint,
    _delete_path,
    _get_path,
    _lineage_constraints,
    _model_compiled_preview,
    _positive_duration,
    _request_constraints,
    _segments,
    _set_path,
    _stable_id,
)
from .prompt_optimization_decision import (  # noqa: F401
    _load_owned_proposal,
    decide_proposal,
)
from .prompt_optimization_lifecycle import (  # noqa: F401
    _PROPOSAL_TTL,
    _artifacts_for_execution,
    _audit_proposed,
    _complete_reserved_proposal,
    _execute_proposal,
    _existing_proposal_replay,
    _invoke_proposal_optimizer,
    _new_proposal_record,
    _persist_proposal_record,
    _proposal_record_for_execution,
    _ProposalExecution,
    _refund_execution,
    _refund_reserved_proposal,
    _reserve_paid_proposal,
    create_proposal,
    reap_stale_running_proposals,
)
from .prompt_optimization_read import (  # noqa: F401
    _COMPILER_CAPABILITY_KEYS,
    PromptOptimizationError,
    _active_compiler_profile,
    _as_dict,
    _aware_datetime,
    _load_lineage_source,
    _preview_text,
    _prompt_from_payload,
    _proposal_history_state,
    _resolve_user_edit_parent,
    _safe_compiler_capabilities,
    _serialize_proposal,
    _serialize_proposal_detail,
    _serialize_proposal_summary,
    _server_context,
    get_proposal,
    list_proposals,
)
from .prompt_optimization_request import (  # noqa: F401
    _invoke_optimizer,
    prepare_proposal_request,
    request_fingerprint,
)
from .prompt_optimization_validation import (  # noqa: F401
    _compiler_warnings,
    _contains_exact_phrase,
    _coverage,
    _proposal_artifacts,
)

_install_assignment_forwarding(
    __name__,
    (
        _compiler_module,
        _decision_module,
        _lifecycle_module,
        _read_module,
        _request_module,
        _validation_module,
    ),
)
