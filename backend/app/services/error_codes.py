"""Shared task/API error classification.

The frontend should not have to regex arbitrary provider text to decide what a
user can do next. Persisted tasks may still only have a free-form error string,
so this helper keeps a conservative compatibility classifier in one place.
"""
from __future__ import annotations

from typing import Literal

ErrorType = Literal[
    "moderation",
    "user_input",
    "provider_timeout",
    "provider_error",
    "system_error",
]

STANDARD_ERROR_TYPES: set[str] = {
    "moderation",
    "user_input",
    "provider_timeout",
    "provider_error",
    "system_error",
}


def normalize_error_type(value: object) -> ErrorType | None:
    text = str(value or "").strip()
    if text in STANDARD_ERROR_TYPES:
        return text  # type: ignore[return-value]
    return None


def classify_error_type(message: object, *, status: str | None = None) -> ErrorType:
    text = str(message or "").strip().lower()
    if not text:
        return "system_error"
    if any(token in text for token in (
        "状态未知",
        "上游可能",
        "external_task_id",
        "request_id",
        "provider",
        "upstream",
    )):
        return "provider_error"
    if any(token in text for token in (
        "自动退款失败",
        "自动取消退款失败",
        "落账失败",
        "落盘失败",
        "补结果结算失败",
        "结算失败",
        "对账",
        "系统恢复",
        "reconcil",
        "reconcile",
    )):
        return "system_error"
    if any(token in text for token in (
        "needs_review",
        "review",
        "审核",
        "人工",
        "确认中",
        "moderation",
    )):
        return "moderation"
    if any(token in text for token in (
        "违规",
        "敏感",
        "policy",
        "safety",
        "unsafe",
        "blocked",
        "unsupported",
        "invalid image",
        "invalid video",
        "format",
        "格式",
        "不支持",
        "素材链接",
        "积分不足",
        "余额",
        "额度",
        "credits",
        "insufficient",
    )):
        return "user_input"
    if any(token in text for token in (
        "timeout",
        "timed out",
        "超时",
        "504",
        "deadline",
    )):
        return "provider_timeout"
    if any(token in text for token in (
        "gateway",
        "provider",
        "upstream",
        "bad gateway",
        "502",
        "503",
        "网关",
        "模型",
    )):
        return "provider_error"
    return "system_error"


def task_error_type(params: dict | None, message: object, *, status: str | None = None) -> ErrorType | None:
    params = params or {}
    explicit = normalize_error_type(params.get("_error_type"))
    if explicit:
        return explicit
    if status == "needs_review":
        if any(params.get(key) for key in (
            "_image_submit_state_unknown",
            "_video_submit_state_unknown",
            "_video_poll_state_unknown",
        )):
            return "provider_error"
        if any(params.get(key) for key in (
            "_image_result_keys",
            "_video_result_url",
            "_video_result_mock",
            "_video_download_state_unknown",
            "_video_poll_state_unknown",
            "_video_download_started_at",
        )):
            return "system_error"
    return classify_error_type(message, status=status)
