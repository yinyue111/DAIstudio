"""Model-gateway client — re-export facade.

All AI capability is collapsed into a small set of calls against your existing
gateway (same base_url + api_key, only the model id changes):

  reverse_prompt()  -> POST /v1/chat/completions   (vision model, image -> words)
  gen_image()       -> POST /v1/images/generations (image model, words -> image)
  submit_video()/poll_video() -> async per-vendor video task (submit -> poll)

Cross-cutting: timeout, retry, error normalisation, call logging (real cost).
When the gateway is not configured (or mock_mode) every call returns locally
generated placeholder media so the whole flow runs offline.
"""
from __future__ import annotations

import time  # noqa: F401 — kept for monkeypatch (gateway.time.monotonic)
from contextlib import contextmanager  # noqa: F401

import httpx  # noqa: F401 — kept for monkeypatch (gateway.httpx.Client)

from ..config import settings  # noqa: F401
from . import (  # noqa: F401
    gateway_download as _gateway_download_module,
)
from . import (
    gateway_image as _gateway_image_module,
)
from . import (
    gateway_prompt_opt as _gateway_prompt_opt_module,
)
from . import (
    gateway_reverse as _gateway_reverse_module,
)
from . import (
    gateway_transport as _gateway_transport_module,
)
from . import (
    gateway_video as _gateway_video_module,
)
from . import (
    video_prompt_compiler as _video_prompt_compiler_module,
)
from .compat_facade import install_assignment_forwarding as _install_assignment_forwarding
from .gateway_download import (  # noqa: F401
    _DOWNLOAD_HEADERS,
    _MAX_DOWNLOAD_BYTES,
    _check_low_speed_download,
    _content_length_exceeds_limit,
    _download,
    _download_httpx_timeout,
    _max_b64_len,
    _reject_compressed_download,
    _remaining_download_timeout,
    _url_origin,
    download_bytes,
    download_bytes_limited,
    download_to_path,
    download_to_storage,
)
from .gateway_image import (  # noqa: F401
    _DATA_URI_IMAGE_RE,
    _IMAGE_RESULT_URL_FIELDS,
    _RESERVED_OUTBOUND_PAYLOAD_FIELDS,
    _anthropic_image_source,
    _anthropic_message_text,
    _data_uri_file,
    _decode_image_response,
    _decode_image_response_with_diagnostics,
    _decode_message_data_uri_images,
    _gen_image_via_anthropic_messages,
    _grok_image_edit_payload,
    _grok_image_payload,
    _image_edit_multipart_parts,
    _image_edit_payload_format,
    _image_quality_for_size,
    _image_response_diagnostic,
    _image_result_urls,
    _image_submit_state_unknown,
    _image_subrequest_failure,
    _is_openai_images_edit_path,
    _mapped_multipart_parts,
    _post_single_image_multipart_repeated,
    _post_single_image_repeated,
    _raise_image_batch_empty,
    _retryable_image_error,
    gen_image,
    map_outbound_payload_fields,
)
from .gateway_mocks import mock_image as _mock_image  # noqa: F401
from .gateway_mocks import (
    mock_video,  # noqa: F401
    mock_video_preview_image,  # noqa: F401
)
from .gateway_prompt_opt import (  # noqa: F401
    _GENERIC_PRODUCT_SIGNAL_RE,
    _NON_PHYSICAL_PRODUCT_CONTEXT_RE,
    _PHYSICAL_PRODUCT_SIGNAL_RE,
    _PRODUCT_VIDEO_TEMPLATE_LABELS,
    _PRODUCT_VISUAL_SIGNAL_RE,
    _PROMPT_CONSTRAINT_RE,
    _PROMPT_CRITICAL_LITERAL_RE,
    _PROMPT_FORBIDDEN_RE,
    _PROMPT_OPTIMIZATION_DIRECTION_INSTRUCTIONS,
    _PROMPT_OPTIMIZATION_DIRECTION_LABELS,
    _assert_complete_video_optimizer_output,
    _dedupe_prompt_items,
    _effective_product_prompt_mode,
    _normalize_video_optimizer_output,
    _prompt_constraint_clauses,
    _prompt_critical_literals,
    _required_video_prompt_constraints,
    optimize_prompt,
    review_prompt_optimization,
)
from .gateway_prompting import ReverseResultValidationError  # noqa: F401
from .gateway_prompting import _decode_json_object as _decode_reverse_json_object  # noqa: F401
from .gateway_prompting import (
    clean_visual_generation_clause as _clean_visual_generation_clause,  # noqa: F401
)
from .gateway_prompting import compose_visual_final_text as _compose_visual_final_text  # noqa: F401
from .gateway_prompting import mock_reverse as _mock_reverse  # noqa: F401
from .gateway_prompting import (
    normalize_video_audio_feature_statuses as _normalize_video_audio_feature_statuses,  # noqa: F401, E501
)
from .gateway_prompting import normalize_video_shots as _normalize_video_shots  # noqa: F401
from .gateway_prompting import parse_structured as _parse_structured  # noqa: F401
from .gateway_prompting import reverse_repair_template as _reverse_repair_template  # noqa: F401
from .gateway_prompting import reverse_template as _reverse_template  # noqa: F401
from .gateway_prompting import (
    sanitize_video_audio_evidence as _sanitize_video_audio_evidence,  # noqa: F401
)
from .gateway_prompting import (
    validate_missing_video_frame_result as _validate_missing_video_frame_result,  # noqa: F401, E501
)
from .gateway_prompting import validate_reverse_result as _validate_reverse_result  # noqa: F401
from .gateway_prompting import video_analysis_gaps as _video_analysis_gaps  # noqa: F401
from .gateway_reverse import (  # noqa: F401
    _REPAIR_SHOT_FACT_TEXT_FIELDS,
    _REPAIR_SHOT_FACT_TIME_FIELDS,
    _REVERSE_AUDIT_CONTEXT_FIELDS,
    _REVERSE_REASONING_MODEL_RE,
    _VIDEO_TEMPORAL_EVIDENCE_FIELDS,
    _anthropic_reverse_content,
    _attach_image_to_video_analysis,
    _drop_unverified_video_timeline,
    _enforce_repair_shot_facts,
    _log_reverse_audit_event,
    _merge_gateway_usage,
    _missing_required_video_frame_indices,
    _repair_missing_video_frames_once,
    _repair_reverse_result_once,
    _repair_shot_fact_key,
    _repair_shot_sort_key,
    _reverse_audit_context,
    _reverse_completion_controls,
    _reverse_prompt_impl,
    _reverse_response_content,
    _reverse_usage,
    _video_analysis_context_text,
    _video_generation_duration_suggestion,
    _video_shots_timeline,
    _video_source_spec,
    _without_conflicting_video_durations,
    reverse_prompt,
)
from .gateway_transport import (  # noqa: F401
    _IMAGE_GATEWAY_SEMAPHORE_KEY,
    GatewayError,
    ImageBatchResult,
    ImageResponseDiagnostic,
    ImageSubrequestFailure,
    _auth,
    _base_url,
    _ensure_gateway_configured,
    _gateway_error_message,
    _gateway_mock,
    _get,
    _guarded_stream,
    _join_api_path,
    _pin_required,
    _post,
    _request,
    _request_json,
    _request_multipart_json,
    _trusted_configured_host,
    _trusted_request_options,
    _video_gateway_mock,
    list_models,
)
from .gateway_video import (  # noqa: F401
    _compact_text,
    _format_gateway_path,
    _normalise_video_result_url,
    _normalise_video_status,
    _poll_video_ark,
    _submit_video_ark,
    _video_auth,
    _video_base,
    _video_get,
    _video_post,
    _video_provider_error,
    _video_url,
    find_video_by_request_id,
    poll_video,
    submit_video,
    video_result_download_headers,
)
from .gateway_video_payloads import VIDEO_STATUS as _VIDEO_STATUS  # noqa: F401
from .gateway_video_payloads import ark_content as _ark_content  # noqa: F401
from .gateway_video_payloads import ark_payload as _ark_payload  # noqa: F401
from .gateway_video_payloads import ark_text as _ark_text  # noqa: F401
from .gateway_video_payloads import extract_by_path as _extract_by_path  # noqa: F401
from .gateway_video_payloads import (
    generic_video_payload_params as _generic_video_payload_params,  # noqa: F401, E501
)
from .gateway_video_payloads import nested_video_url as _nested_video_url  # noqa: F401
from .model_gateway_config import RuntimeGatewayConfig  # noqa: F401
from .safe_logging import redact_url_for_log  # noqa: F401
from .ssrf import (  # noqa: F401
    MAX_REDIRECTS,
    SsrfError,
    assert_safe_url,
    pinned_client,
)
from .video_prompt_compiler import (  # noqa: F401
    clean_video_prompt_section,
    compact_single_clip_prompt,
    infer_video_model_profile,
    merge_video_constraint_clauses,
    parse_structured_video_sections,
    parse_video_prompt,
    render_structured_video_prompt,
    split_video_post_production,
    video_action_requirements,
)

_install_assignment_forwarding(
    __name__,
    (
        _gateway_download_module,
        _gateway_image_module,
        _gateway_prompt_opt_module,
        _gateway_reverse_module,
        _gateway_transport_module,
        _gateway_video_module,
        _video_prompt_compiler_module,
    ),
)
