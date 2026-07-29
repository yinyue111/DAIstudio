"""Compatibility facade for shared quote infrastructure."""
from __future__ import annotations

from . import generation_quote_contracts as _contracts
from . import generation_quote_lifecycle as _lifecycle
from . import generation_quote_output as _output
from . import generation_quote_snapshot as _snapshot
from .compat_facade import install_assignment_forwarding as _install_assignment_forwarding
from .generation_quote_contracts import (  # noqa: F401
    QUOTE_KINDS,
    _balance_warning,
    _expected_action_breakdown,
    _expired_quote_needs_reissue,
    _invalid_quote_snapshot,
    _json_fingerprint,
    _normalized_client_request_id,
    _quote_mismatch,
    _runtime_identity,
    _utc,
    _versioned_runtime_model_snapshot,
    _without_action_fingerprints,
    normalized_price_breakdown,
    quote_item,
)
from .generation_quote_lifecycle import (  # noqa: F401
    consume_execution_quote,
    create_execution_quote,
    find_idempotent_quote,
    lock_execution_quote,
)
from .generation_quote_output import (  # noqa: F401
    _public_quote_subject,
    generation_quote_out,
)
from .generation_quote_snapshot import validate_quote_snapshot_integrity  # noqa: F401

_install_assignment_forwarding(
    __name__,
    (_contracts, _lifecycle, _output, _snapshot),
)
