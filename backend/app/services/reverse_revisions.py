"""Compatibility facade for reverse-result revision services."""
from __future__ import annotations

from . import reverse_revision_feedback as _feedback
from . import reverse_revision_lifecycle as _lifecycle
from . import reverse_revision_reanalysis as _reanalysis
from . import reverse_revision_retry as _retry
from . import reverse_revision_timeline as _timeline
from .compat_facade import install_assignment_forwarding as _install_assignment_forwarding
from .reverse_operation_records import (  # noqa: F401
    _create_quoted_operation,
    _get_owned,
)
from .reverse_revision_feedback import (  # noqa: F401
    get_feedback,
    upsert_feedback,
)
from .reverse_revision_lifecycle import (  # noqa: F401
    apply_result_revision,
    create_result_revision,
    list_result_revisions,
)
from .reverse_revision_reanalysis import (  # noqa: F401
    attach_shot_reanalysis_context,
    prepare_shot_generation,
    shot_reanalysis_body,
)
from .reverse_revision_retry import (  # noqa: F401
    build_retry_operation_body,
    retry_operation,
)
from .reverse_revision_timeline import (  # noqa: F401
    _TIMED_SHOT_REFERENCE_SPECS,
    _finite_float,
    _invalidate_timeline_compilation,
    _latest_timeline_revision,
    _rebind_shot_timed_evidence,
    _reference_id,
    _revision_shots,
    _shot_by_id,
    _timed_evidence_sources,
    _timed_row_overlaps_shot,
    edit_shot_timeline,
    revision_shots,
)

_install_assignment_forwarding(
    __name__,
    (_feedback, _lifecycle, _reanalysis, _retry, _timeline),
)
