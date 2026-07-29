"""Constraint coverage and artifact validation for prompt optimization."""

from __future__ import annotations

import json
import re
from copy import deepcopy
from difflib import SequenceMatcher
from typing import Any

from ..models import ModelConfig, ReverseOperation, ReverseResultRevision
from .prompt_optimization_compiler import (
    _COMPILER_VERSION,
    _apply_compiler_contract,
    _get_path,
    _segments,
)


def _compiler_warnings(
    original: dict[str, Any],
    candidate: dict[str, Any],
    *,
    category: str,
    capabilities: dict[str, Any],
) -> list[dict[str, str]]:
    warnings: list[dict[str, str]] = []
    parameters = original.get("parameters") if isinstance(original.get("parameters"), dict) else {}
    candidate_parameters = (
        candidate.get("parameters") if isinstance(candidate.get("parameters"), dict) else {}
    )
    requested = {
        "aspect_ratio": parameters.get("aspect_ratio") or parameters.get("ratio"),
        "resolution": parameters.get("resolution") or parameters.get("vResolution"),
        "duration": parameters.get("duration") or parameters.get("vDuration"),
    }
    supported = {
        "aspect_ratio": capabilities.get("aspect_ratios"),
        "resolution": capabilities.get("resolutions"),
        "duration": capabilities.get("durations"),
    }
    for field, value in requested.items():
        choices = supported[field]
        if value in (None, ""):
            continue
        if not isinstance(choices, list):
            warnings.append(
                {
                    "code": "capability_field_undeclared",
                    "field": field,
                    "action": "unverified",
                    "message": f"目标模型能力版本未声明 {field} 支持范围，不能声称已兼容。",
                }
            )
        elif str(value) not in {str(item) for item in choices}:
            aliases = {
                "aspect_ratio": ("aspect_ratio", "ratio"),
                "resolution": ("resolution", "vResolution"),
                "duration": ("duration", "vDuration"),
            }[field]
            dropped = all(candidate_parameters.get(alias) in (None, "") for alias in aliases)
            warnings.append(
                {
                    "code": "unsupported_field_value",
                    "field": field,
                    "action": "dropped" if dropped else "unverified",
                    "message": (
                        f"目标模型能力版本不支持 {field}={value}，编译结果已移除该参数。"
                        if dropped
                        else f"目标模型能力版本不支持 {field}={value}，但候选结果仍保留该值。"
                    ),
                }
            )
    required_capability = "text_to_video" if category == "video" else "text_to_image"
    if capabilities.get(required_capability) is False:
        warnings.append(
            {
                "code": "unsupported_generation_mode",
                "field": required_capability,
                "action": "unverified",
                "message": f"目标模型能力版本明确不支持 {required_capability}。",
            }
        )
    profile = capabilities.get("prompt_profile")
    if isinstance(profile, dict):
        for field in profile.get("dropped_fields") or []:
            if isinstance(field, str) and _get_path(original, field) not in (None, "", [], {}):
                dropped = _get_path(candidate, field) in (None, "", [], {})
                warnings.append(
                    {
                        "code": "profile_dropped_field",
                        "field": field,
                        "action": "dropped" if dropped else "unverified",
                        "message": (
                            f"目标模型提示词配置文件已移除 {field}。"
                            if dropped
                            else f"目标模型提示词配置要求移除 {field}，但候选结果仍保留该字段。"
                        ),
                    }
                )
        transforms = profile.get("transforms")
        if isinstance(transforms, dict):
            for field, description in transforms.items():
                before = _get_path(original, field) if isinstance(field, str) else None
                after = _get_path(candidate, field) if isinstance(field, str) else None
                if isinstance(field, str) and before not in (None, "", [], {}):
                    transformed = before != after
                    warnings.append(
                        {
                            "code": "profile_transformed_field",
                            "field": field,
                            "action": "transformed" if transformed else "unverified",
                            "message": str(description or f"目标模型会转换 {field}。")[:500],
                        }
                    )
    return warnings


def _contains_exact_phrase(text: str, phrase: Any) -> bool:
    normalized_phrase = " ".join(str(phrase or "").casefold().split())
    if not normalized_phrase:
        return False
    phrase_pattern = r"\s+".join(re.escape(part) for part in normalized_phrase.split(" "))
    return (
        re.search(
            rf"(?<!\w){phrase_pattern}(?!\w)",
            str(text or "").casefold(),
        )
        is not None
    )


def _coverage(
    constraints: list[dict[str, Any]],
    *,
    original: dict[str, Any],
    suggestion: dict[str, Any],
    model_coverage: Any,
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    mapped = {
        str(item.get("constraint_id")): item
        for item in (model_coverage if isinstance(model_coverage, list) else [])
        if isinstance(item, dict) and item.get("constraint_id")
    }
    rows: list[dict[str, Any]] = []
    warnings: list[dict[str, str]] = []
    for constraint in constraints:
        kind = constraint["type"]
        value = constraint["value"]
        source_field = constraint["source_field"]
        evidence: list[dict[str, Any]] = []
        verified = False
        if kind == "aspect_ratio":
            candidate_value = (
                _get_path(suggestion, source_field)
                if source_field.startswith("parameters.")
                else _get_path(suggestion, "parameters.aspect_ratio")
                or _get_path(suggestion, "parameters.ratio")
            )
            verified = str(candidate_value) == str(value)
            if verified:
                evidence.append(
                    {
                        "validator": "exact_field",
                        "field": "parameters.aspect_ratio",
                        "value": candidate_value,
                    }
                )
        elif kind == "duration":
            candidate_value = (
                _get_path(suggestion, source_field)
                if source_field.startswith("parameters.")
                else _get_path(suggestion, "parameters.duration")
                or _get_path(suggestion, "parameters.vDuration")
            )
            verified = str(candidate_value) == str(value)
            if verified:
                evidence.append(
                    {
                        "validator": "exact_field",
                        "field": "parameters.duration",
                        "value": candidate_value,
                    }
                )
        elif kind == "negative_list":
            candidate = suggestion.get("negative") or suggestion.get("negative_prompt")
            candidate_values = candidate if isinstance(candidate, list) else [candidate]
            items = value if isinstance(value, list) else [value]
            verified = all(
                any(
                    _contains_exact_phrase(str(candidate_value or ""), item)
                    for candidate_value in candidate_values
                )
                for item in items
            )
            if verified:
                evidence.append(
                    {"validator": "exact_negative_items", "field": source_field, "items": items}
                )
        elif kind == "exact_text":
            prompt_fields = [
                suggestion.get(field)
                for field in ("final_text", "compiled_prompt", "prompt")
                if isinstance(suggestion.get(field), str)
            ]
            verified = any(str(value) in field for field in prompt_fields)
            if verified:
                evidence.append({"validator": "exact_quoted_text", "quote": value})
            else:
                post_production = _get_path(
                    suggestion,
                    "model_compiled.post_production",
                )
                if isinstance(post_production, dict):
                    post_production_text = json.dumps(
                        post_production,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                    verified = str(value) in post_production_text
                    if verified:
                        evidence.append(
                            {
                                "validator": "exact_post_production_text",
                                "field": "model_compiled.post_production",
                                "quote": value,
                            }
                        )
        elif kind == "protected_evidence":
            verified = original.get("image_evidence") == suggestion.get("image_evidence")
            if verified:
                evidence.append({"validator": "immutable_evidence_row", "field": source_field})
        if verified:
            rows.append(
                {**constraint, "status": "verified", "confidence": 1.0, "evidence": evidence}
            )
            continue
        claim = mapped.get(constraint["constraint_id"])
        if isinstance(claim, dict):
            field = str(claim.get("suggestion_field") or "").strip()
            quote = str(claim.get("evidence_quote") or "").strip()
            field_value = _get_path(suggestion, field) if field else None
            if field and quote and quote in str(field_value or ""):
                try:
                    confidence = min(max(float(claim.get("confidence") or 0), 0.0), 1.0)
                except (TypeError, ValueError):
                    confidence = 0.0
                rows.append(
                    {
                        **constraint,
                        "status": "mapped",
                        "confidence": confidence,
                        "evidence": [
                            {"field": field, "quote": quote, "source": "optimizer_mapping"}
                        ],
                        "warning": "语义覆盖仅有模型映射证据，未通过确定性验证。",
                    }
                )
                warnings.append(
                    {
                        "code": "semantic_coverage_unverified",
                        "field": source_field,
                        "action": "unverified",
                        "message": f"约束 {constraint['constraint_id']} 只有语义映射证据，需人工确认。",
                    }
                )
                continue
        rows.append(
            {
                **constraint,
                "status": "unverified",
                "confidence": 0.0,
                "evidence": [],
                "warning": "未找到可验证的覆盖证据。",
            }
        )
        warnings.append(
            {
                "code": "constraint_unverified",
                "field": source_field,
                "action": "unverified",
                "message": f"约束 {constraint['constraint_id']} 未验证，不得视为已保留。",
            }
        )
    return rows, warnings


def _proposal_artifacts(
    *,
    original: dict[str, Any],
    candidate: dict[str, Any],
    result: dict[str, Any],
    constraints: list[dict[str, Any]],
    profile: dict[str, Any],
    category: str,
    source: str,
    operation: ReverseOperation | None,
    revision: ReverseResultRevision | None,
    resolved_parent: ReverseResultRevision | None,
    optimizer: ModelConfig | None,
    compile_only: bool,
    request_hash: str,
) -> dict[str, Any]:
    candidate = _apply_compiler_contract(
        candidate,
        capabilities=profile["capabilities"],
    )
    segments = _segments(original, candidate)
    warnings = _compiler_warnings(
        original,
        candidate,
        category=category,
        capabilities=profile["capabilities"],
    )
    warnings.extend(deepcopy(result.get("compiler_warnings") or []))
    coverage, coverage_warnings = _coverage(
        constraints,
        original=original,
        suggestion=candidate,
        model_coverage=result.get("constraint_coverage"),
    )
    warnings.extend(coverage_warnings)
    provenance = {
        "reverse_operation_id": int(operation.id) if operation is not None else None,
        "reverse_revision_id": int(revision.id) if revision is not None else None,
        "reverse_revision_version": int(revision.version) if revision is not None else None,
        "reverse_revision_hash": revision.payload_hash if revision is not None else None,
        "selected_revision_id": int(revision.id) if revision is not None else None,
        "selected_revision_source": revision.source if revision is not None else None,
        "resolved_parent_revision_id": (
            int(resolved_parent.id) if resolved_parent is not None else None
        ),
        "resolved_parent_revision_source": (
            resolved_parent.source if resolved_parent is not None else None
        ),
        "optimizer_model_config_id": (
            int(optimizer.id) if not compile_only and optimizer is not None else None
        ),
        "optimizer_model_id": (
            optimizer.model_id if not compile_only and optimizer is not None else None
        ),
        "compiler_version": str(
            (
                (candidate.get("compiler_metadata") or {}).get("version")
                if isinstance(candidate.get("compiler_metadata"), dict)
                else None
            )
            or _COMPILER_VERSION
        ),
        "catalog_capability_version_id": profile["capability_version_id"],
    }
    metrics = {
        "request_hash": request_hash,
        "execution_status": "succeeded",
        "source_chars": len(source),
        "suggestion_chars": len(str(candidate.get("final_text") or "")),
        "edit_distance_ratio": 1.0
        - SequenceMatcher(
            None,
            source,
            str(candidate.get("final_text") or ""),
        ).ratio(),
    }
    return {
        "candidate": candidate,
        "segments": segments,
        "warnings": warnings,
        "coverage": coverage,
        "provenance": provenance,
        "metrics": metrics,
    }
