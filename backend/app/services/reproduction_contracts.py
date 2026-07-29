"""Shared contracts for reproduction assessment lifecycle services."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from .reproduction_comparators import ReproductionAssessmentError

TERMINAL_STATUSES = frozenset({"succeeded", "partial", "failed", "canceled"})
CORRECTABLE_STATUSES = frozenset({"succeeded", "partial"})


class ReproductionAssessmentNotFound(ReproductionAssessmentError):
    status_code = 404
    code = "REPRODUCTION_ASSESSMENT_NOT_FOUND"


class ReproductionAssessmentConflict(ReproductionAssessmentError):
    status_code = 409
    code = "REPRODUCTION_ASSESSMENT_CONFLICT"


def canonical_hash(value: Any) -> str:
    raw = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def request_fingerprint(body: Any) -> str:
    payload = body.model_dump(mode="json", exclude={"idempotency_key"})
    return canonical_hash(payload)
