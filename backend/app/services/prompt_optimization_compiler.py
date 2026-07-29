"""Deterministic target-model compilation for prompt optimization."""

from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from types import SimpleNamespace
from typing import Any

from ..models import ModelConfig, ReverseResultRevision
from ..prompt_optimization_schemas import StudioPromptOptimizationIn
from .generation_model_runtime import FrozenAdapterConfigError, frozen_image_adapter
from .generation_prompts import (
    compile_image_prompt_profile,
    generation_prompt_for_model,
    product_fidelity_prompt,
)
from .prompt_optimization_read import PromptOptimizationError
from .video_prompt_compiler import (
    COMPILER_VERSION as VIDEO_PROMPT_COMPILER_VERSION,
)
from .video_prompt_compiler import (
    VIDEO_SUBMIT_CONTRACT_VERSION,
    build_video_prompt_references,
    compile_video_prompt,
)

_QUOTED_TEXT_RE = re.compile(r"[“「『\"']([^\"'”」』\n]{1,200})[”」』\"']")
_SEMANTIC_KEY_RE = re.compile(
    r"(约束|不可改|必须保持|一致性|保护|constraint|locked|preserve)",
    re.IGNORECASE,
)
_COMPILER_VERSION = "studio-prompt-compiler.v2"
_IMAGE_PROMPT_COMPILER_VERSION = "image-generation-runtime.v1"


def _stable_id(prefix: str, *parts: Any) -> str:
    raw = json.dumps(parts, ensure_ascii=False, sort_keys=True, default=str)
    return f"{prefix}_{hashlib.sha256(raw.encode()).hexdigest()[:20]}"


def _constraint(
    *,
    scope: str,
    constraint_type: str,
    source_field: str,
    value: Any,
) -> dict[str, Any]:
    return {
        "constraint_id": _stable_id("constraint", scope, constraint_type, source_field, value),
        "type": constraint_type,
        "source_field": source_field,
        "value": deepcopy(value),
    }


def _lineage_constraints(
    revision: ReverseResultRevision,
    payload: dict[str, Any],
) -> list[dict[str, Any]]:
    scope = f"revision:{int(revision.id)}"
    constraints: list[dict[str, Any]] = []
    parameters = payload.get("parameters") if isinstance(payload.get("parameters"), dict) else {}
    for key in ("aspect_ratio", "ratio"):
        if parameters.get(key):
            constraints.append(
                _constraint(
                    scope=scope,
                    constraint_type="aspect_ratio",
                    source_field=f"parameters.{key}",
                    value=str(parameters[key]),
                )
            )
            break
    for key in ("duration", "vDuration"):
        if parameters.get(key) is not None:
            constraints.append(
                _constraint(
                    scope=scope,
                    constraint_type="duration",
                    source_field=f"parameters.{key}",
                    value=parameters[key],
                )
            )
            break
    negative = payload.get("negative") or payload.get("negative_prompt")
    if negative:
        values = (
            [str(item).strip() for item in negative if str(item).strip()]
            if isinstance(negative, list)
            else [item.strip() for item in re.split(r"[,，;；\n]+", str(negative)) if item.strip()]
        )
        if values:
            constraints.append(
                _constraint(
                    scope=scope,
                    constraint_type="negative_list",
                    source_field="negative",
                    value=values,
                )
            )
    final_text = str(payload.get("final_text") or "")
    for index, match in enumerate(_QUOTED_TEXT_RE.finditer(final_text)):
        constraints.append(
            _constraint(
                scope=scope,
                constraint_type="exact_text",
                source_field=f"final_text.quote[{index}]",
                value=match.group(1),
            )
        )
    structured = payload.get("structured") if isinstance(payload.get("structured"), dict) else {}
    for key, value in structured.items():
        if _SEMANTIC_KEY_RE.search(str(key)) and value not in (None, "", [], {}):
            constraints.append(
                _constraint(
                    scope=scope,
                    constraint_type="semantic",
                    source_field=f"structured.{key}",
                    value=value,
                )
            )
    evidence = (
        payload.get("image_evidence") if isinstance(payload.get("image_evidence"), list) else []
    )
    for index, item in enumerate(evidence):
        if not isinstance(item, dict) or not item.get("protected"):
            continue
        constraints.append(
            _constraint(
                scope=scope,
                constraint_type="protected_evidence",
                source_field=f"image_evidence[{index}]",
                value={
                    "evidence_id": item.get("evidence_id") or f"index-{index}",
                    "field_key": item.get("field_key"),
                    "evidence_text": item.get("evidence_text"),
                },
            )
        )
    return constraints


def _request_constraints(
    body: StudioPromptOptimizationIn,
    *,
    scope: str,
) -> list[dict[str, Any]]:
    return [
        _constraint(
            scope=scope,
            constraint_type=item.type,
            source_field=f"request.protected_constraints[{index}]",
            value=item.value,
        )
        for index, item in enumerate(body.protected_constraints)
    ]


def _get_path(payload: dict[str, Any], field_path: str) -> Any:
    current: Any = payload
    for part in field_path.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def _set_path(payload: dict[str, Any], field_path: str, value: Any) -> None:
    parts = field_path.split(".")
    current = payload
    for part in parts[:-1]:
        child = current.get(part)
        if not isinstance(child, dict):
            child = {}
            current[part] = child
        current = child
    current[parts[-1]] = deepcopy(value)


def _delete_path(payload: dict[str, Any], field_path: str) -> bool:
    parts = field_path.split(".")
    current: Any = payload
    for part in parts[:-1]:
        if not isinstance(current, dict):
            return False
        current = current.get(part)
    if not isinstance(current, dict) or parts[-1] not in current:
        return False
    del current[parts[-1]]
    return True


def _apply_compiler_contract(
    payload: dict[str, Any],
    *,
    capabilities: dict[str, Any],
) -> dict[str, Any]:
    compiled = deepcopy(payload)
    parameters = compiled.get("parameters")
    if isinstance(parameters, dict):
        fields = {
            "aspect_ratios": ("aspect_ratio", "ratio"),
            "resolutions": ("resolution", "vResolution"),
            "durations": ("duration", "vDuration"),
        }
        for capability_key, aliases in fields.items():
            choices = capabilities.get(capability_key)
            if not isinstance(choices, list):
                continue
            supported = {str(item) for item in choices}
            for alias in aliases:
                value = parameters.get(alias)
                if value not in (None, "") and str(value) not in supported:
                    parameters.pop(alias, None)
    profile = capabilities.get("prompt_profile")
    if isinstance(profile, dict):
        for raw_path in profile.get("dropped_fields") or []:
            path = str(raw_path or "").strip()
            if path in {"negative", "negative_prompt"} or re.fullmatch(
                r"(?:structured|parameters)\.[^\.]{1,128}", path
            ):
                _delete_path(compiled, path)
    return compiled


def _positive_duration(value: Any, *, default: float = 10.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _model_compiled_preview(
    source_payload: dict[str, Any],
    source: str,
    *,
    category: str,
    target_model: ModelConfig,
    context: dict[str, Any],
    capability_profile: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Run the same deterministic compiler used by the generation path."""
    candidate = deepcopy(source_payload)
    structured = (
        deepcopy(source_payload.get("structured"))
        if isinstance(source_payload.get("structured"), dict)
        else {}
    )
    generation_prompt_payload = {**structured, **deepcopy(source_payload)}
    parameters = (
        deepcopy(source_payload.get("parameters"))
        if isinstance(source_payload.get("parameters"), dict)
        else {}
    )
    subject_mode = (
        str(
            context.get("subject_mode")
            or parameters.get("subject_mode")
            or ("product" if context.get("product_mode") else "")
        )
        .strip()
        .lower()
    )
    source_asset_url = str(context.get("source_asset_url") or "").strip() or None
    source_type = str(context.get("source_type") or "").strip() or None
    compiler_warnings: list[dict[str, str]] = []

    if category == "video":
        references = build_video_prompt_references(
            source_asset_url=source_asset_url,
            source_type=source_type,
            params=parameters,
        )
        extra = deepcopy(target_model.extra) if isinstance(target_model.extra, dict) else {}
        capabilities = extra.get("capabilities")
        if not isinstance(capabilities, dict):
            capabilities = {}
            extra["capabilities"] = capabilities
        # The optimization preview exposes audiovisual instructions as separate
        # post-production tracks. The generation path can embed them later when
        # the selected runtime model supports native audio and text overlays.
        capabilities["embedded_av_requirements"] = False
        model_profiles = extra.get("video_prompt_profiles") or extra.get("prompt_profiles")
        if not isinstance(model_profiles, dict):
            model_profiles = None
        duration = _positive_duration(
            parameters.get("duration") or parameters.get("vDuration") or context.get("duration")
        )
        compiled = compile_video_prompt(
            generation_prompt_payload,
            duration=duration,
            model_id=str(target_model.model_id or ""),
            provider=str(target_model.provider or ""),
            extra=extra,
            references=references,
            product_reference=any(item.get("role") == "product" for item in references),
            portrait_reference=any(item.get("role") == "character" for item in references),
            product_lock_mode=str(
                parameters.get("product_lock_mode") or context.get("product_lock_mode") or "locked"
            ),
            product_video_template=str(
                parameters.get("product_video_template")
                or context.get("product_video_template")
                or "prompt_driven"
            ),
            model_profiles=model_profiles,
            fit_mode="single_clip",
        )
        compiled_prompt = str(compiled.get("prompt") or "").strip()
        if not compiled_prompt:
            raise PromptOptimizationError(502, "视频提示词编译器未返回有效结果")
        plan = compiled.get("plan") if isinstance(compiled.get("plan"), dict) else {}
        post_production = {
            "subtitles": list(plan.get("post_overlays") or []),
            "voiceover": str(plan.get("voiceover") or "").strip(),
            "sfx": list(plan.get("sfx") or []),
        }
        compiler_metadata = {
            "version": str(compiled.get("compiler_version") or VIDEO_PROMPT_COMPILER_VERSION),
            "orchestrator_version": _COMPILER_VERSION,
            "submit_contract_version": VIDEO_SUBMIT_CONTRACT_VERSION,
            "category": "video",
            "target_model_config_id": int(target_model.id),
            "target_model_id": target_model.model_id,
            "capability_version_id": int(capability_profile["capability_version_id"]),
            "capability_version": int(capability_profile["capability_version"]),
            "profile": deepcopy(compiled.get("profile") or {}),
            "metadata": deepcopy(compiled.get("metadata") or {}),
            "sequence_required": bool(compiled.get("sequence_required")),
            "post_production": post_production,
        }
        for message in plan.get("warnings") or []:
            if str(message or "").strip():
                compiler_warnings.append(
                    {
                        "code": "video_compiler_warning",
                        "field": "final_text",
                        "action": "info",
                        "message": str(message).strip()[:500],
                    }
                )
        reference_roles = deepcopy(references)
    else:
        task_parameters = deepcopy(parameters)
        if subject_mode:
            task_parameters["subject_mode"] = subject_mode
        if source_type == "image" and source_asset_url:
            task_parameters.setdefault("reference_image_url", source_asset_url)
        elif subject_mode in {"product", "portrait"}:
            task_parameters.setdefault(
                "reference_image_url",
                "studio://declared-reference",
            )
        compile_task = SimpleNamespace(
            category="image",
            prompt=generation_prompt_payload,
            params=task_parameters,
            source_type=source_type,
            source_asset_url=source_asset_url,
        )
        compiled_prompt = generation_prompt_for_model(source, compile_task)
        compiled_prompt = product_fidelity_prompt(compiled_prompt, compile_task)
        try:
            adapter = frozen_image_adapter(target_model.extra)
        except FrozenAdapterConfigError as exc:
            raise PromptOptimizationError(409, f"目标图片模型编译配置无效：{exc}") from exc
        compiled_prompt = compile_image_prompt_profile(
            compiled_prompt,
            adapter.prompt_profile,
        ).strip()
        if not compiled_prompt:
            raise PromptOptimizationError(502, "图片提示词编译器未返回有效结果")
        post_production = {"subtitles": [], "voiceover": "", "sfx": []}
        compiler_metadata = {
            "version": _IMAGE_PROMPT_COMPILER_VERSION,
            "orchestrator_version": _COMPILER_VERSION,
            "category": "image",
            "target_model_config_id": int(target_model.id),
            "target_model_id": target_model.model_id,
            "capability_version_id": int(capability_profile["capability_version_id"]),
            "capability_version": int(capability_profile["capability_version"]),
            "prompt_profile": deepcopy(adapter.prompt_profile),
            "post_production": post_production,
        }
        reference_roles = []

    candidate["final_text"] = compiled_prompt
    candidate["compiler_metadata"] = compiler_metadata
    candidate["model_compiled"] = {
        "schema_version": "model-compiled-preview.v1",
        "category": category,
        "source_prompt": source,
        "prompt": compiled_prompt,
        "target_model": {
            "model_config_id": int(target_model.id),
            "model_id": target_model.model_id,
            "provider": target_model.provider,
        },
        "compiler": deepcopy(compiler_metadata),
        "reference_roles": reference_roles,
        "post_production": post_production,
    }
    return candidate, {
        "prompt": compiled_prompt,
        "compiled_prompt": compiled_prompt,
        "constraint_coverage": [],
        "optimizer_model_id": None,
        "compiler_warnings": compiler_warnings,
    }


def _candidate_payload(
    source_payload: dict[str, Any],
    result: dict[str, Any],
) -> dict[str, Any]:
    candidate = deepcopy(source_payload)
    optimized = str(result.get("prompt") or result.get("compiled_prompt") or "").strip()
    if not optimized:
        raise PromptOptimizationError(502, "优化模型未返回有效提示词")
    candidate["final_text"] = optimized
    suggestions = result.get("field_suggestions")
    if isinstance(suggestions, dict):
        for path, value in suggestions.items():
            normalized = str(path or "").strip()
            if (
                normalized == "final_text"
                or normalized == "negative"
                or re.fullmatch(r"(?:structured|parameters)\.[^\.]{1,128}", normalized)
            ):
                _set_path(candidate, normalized, value)
    return candidate


def _segments(
    original: dict[str, Any],
    suggestion: dict[str, Any],
) -> list[dict[str, Any]]:
    paths = {"final_text", "negative", "compiler_metadata"}
    for root in ("structured", "parameters"):
        before = original.get(root) if isinstance(original.get(root), dict) else {}
        after = suggestion.get(root) if isinstance(suggestion.get(root), dict) else {}
        paths.update(f"{root}.{key}" for key in set(before).union(after))
    segments: list[dict[str, Any]] = []
    for path in sorted(paths, key=lambda item: (item != "final_text", item)):
        before = _get_path(original, path)
        after = _get_path(suggestion, path)
        if before == after and path != "final_text":
            continue
        segments.append(
            {
                "id": _stable_id("segment", path, before, after),
                "field_path": path,
                "label": path.replace("structured.", "结构化字段 ").replace("parameters.", "参数 "),
                "original": deepcopy(before),
                "suggestion": deepcopy(after),
                "changed": before != after,
            }
        )
    return segments
