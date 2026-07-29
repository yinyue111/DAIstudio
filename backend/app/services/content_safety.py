"""Lightweight text safety gate for prompts and generation instructions.

除文本禁词外,本模块还提供图像/视频机器审核("先审后发")的统一入口
``assert_media_allowed``:kill-switch 语义与文本禁词一致——默认关闭、关闭时
零出网零拦截;开启后经可插拔 provider(见 content_safety_providers)机审,
审核结论落入 audit_logs(action=media_moderation)供人工复核.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from fastapi import HTTPException
from sqlalchemy.orm import Session

from . import audit
from .config_store import get_bool_setting, get_setting
from .content_safety_providers import (
    DECISION_ALLOW,
    DECISION_REJECT,
    DECISION_REVIEW,
    MediaModerationRequest,
    MediaModerationResult,
    get_media_moderation_provider,
)

_SPLIT_RE = re.compile(r"[\n,，、;；]+")
_LATIN_WORD_RE = re.compile(r"[a-z0-9_]", re.IGNORECASE)


def _terms(raw: str | None) -> list[str]:
    return [term.strip().lower() for term in _SPLIT_RE.split(str(raw or "")) if term.strip()]


def _flatten_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except TypeError:
        return str(value)


def _term_matches(text: str, term: str) -> bool:
    if not term:
        return False
    if not _LATIN_WORD_RE.search(term):
        return term in text
    pattern = rf"(?<![a-z0-9_]){re.escape(term)}(?![a-z0-9_])"
    return re.search(pattern, text, flags=re.IGNORECASE) is not None


def assert_text_allowed(db: Session, *values: Any) -> None:
    """Block configured banned terms when content safety is enabled.

    This is intentionally a local deterministic guard. It does not replace a
    model/provider moderation service for generated images/videos, but it gives
    admins an immediate kill-switch for terms they do not want sent upstream.
    """
    if not get_bool_setting(db, "content_safety_enabled", False):
        return
    terms = _terms(get_setting(db, "content_safety_banned_terms", ""))
    if not terms:
        return
    text = "\n".join(_flatten_text(v) for v in values).lower()
    for term in terms:
        if _term_matches(text, term):
            raise HTTPException(400, "内容安全拦截:提示词包含平台禁止内容")


# ---------------------------------------------------------------------------
# 图像/视频机器审核("先审后发"接入层)
# ---------------------------------------------------------------------------


class MediaModerationRejected(HTTPException):
    """媒体内容被机审拒绝或判为需人工复审.

    携带结构化结论,便于生成链路把 decision == "review" 的任务转入
    NEEDS_REVIEW 而不是直接失败.
    """

    def __init__(
        self,
        status_code: int,
        detail: str,
        *,
        decision: str,
        reason: str = "",
        confidence: float | None = None,
    ) -> None:
        super().__init__(status_code, detail)
        self.decision = decision
        self.reason = reason
        self.confidence = confidence


class MediaModerationUnavailable(HTTPException):
    """审核服务不可用且失败策略为 fail-closed(默认)时抛出."""

    def __init__(self) -> None:
        super().__init__(503, "内容安全审核服务暂不可用,请稍后重试")


def _log_media_moderation(
    db: Session,
    *,
    user_id: int | None,
    biz_type: str | None,
    biz_id: int | None,
    ip: str | None,
    detail: dict,
) -> None:
    # audit.log 用独立会话写入,不会污染调用方事务;写失败只降级为日志.
    audit.log(
        db,
        user_id=user_id,
        action="media_moderation",
        biz_type=biz_type,
        biz_id=biz_id,
        ip=ip,
        detail=detail,
    )


def assert_media_allowed(
    db: Session,
    *,
    media_type: str,
    scene: str,
    data: bytes | None = None,
    url: str | None = None,
    mime_type: str | None = None,
    user_id: int | None = None,
    biz_type: str | None = None,
    biz_id: int | None = None,
    ip: str | None = None,
) -> MediaModerationResult:
    """图像/视频机器审核统一入口(先审后发).

    kill-switch 语义与文本禁词一致:``media_moderation_enabled`` 默认 False,
    关闭时不构造 provider、不出网、不写审计,行为与未接入机审时完全一致.

    开启后:
    - 机审 reject  -> 抛 400 MediaModerationRejected(拒绝发布);
    - 机审 review  -> 抛 400 MediaModerationRejected(需人工复审后才能发布);
    - 机审 allow   -> 返回结果;
    - provider 异常/超时 -> 默认 fail-closed 抛 503 MediaModerationUnavailable.
      合规理由:先审后发要求内容未经审核不得发布,若审核服务故障时默认放行
      (fail-open),恰恰会在最薄弱的时刻漏放违规内容.可用性通过运行时开关
      ``media_moderation_fail_open`` 兜底——供应商故障期间管理员可显式降级
      放行,该决策本身也会落审计,属于有记录的人工决定而非静默默认.
    所有结论(含降级放行与失败)均落 audit_logs(action=media_moderation),
    detail 内含 provider、结论、原因、置信度与媒体指纹(sha256/url)供复核.
    """
    if not get_bool_setting(db, "media_moderation_enabled", False):
        return MediaModerationResult(
            decision=DECISION_ALLOW,
            reason="机器审核未启用",
            provider="disabled",
        )

    if media_type == "audio":
        # 显式降级语义:当前 provider 协议只定义了 image/video 两种媒体审核
        # (见 content_safety_providers.MediaModerationRequest),对 audio 没有
        # 明确的送审语义。若把 audio 送进 provider,HTTP 骨架会把未识别类型归
        # review,导致启用审核后音频上传全量被卡。因此音频暂不送审、直接放行,
        # 但结论照常落 audit_logs(decision=allow, provider=audio_unsupported)
        # 供人工抽查;待供应商支持音频审核后移除本分支即可自动接入。
        base_detail: dict[str, Any] = {
            "media_type": media_type,
            "scene": scene,
            "mime_type": mime_type,
        }
        if url:
            base_detail["url"] = str(url)[:500]
        if data is not None:
            base_detail["bytes"] = len(data)
            base_detail["sha256"] = hashlib.sha256(data).hexdigest()
        reason = "音频暂不支持机器审核,按显式降级策略放行并记录"
        _log_media_moderation(
            db,
            user_id=user_id,
            biz_type=biz_type,
            biz_id=biz_id,
            ip=ip,
            detail={**base_detail, "provider": "audio_unsupported", "decision": DECISION_ALLOW, "reason": reason},
        )
        return MediaModerationResult(
            decision=DECISION_ALLOW,
            reason=reason,
            provider="audio_unsupported",
        )

    request = MediaModerationRequest(
        media_type=media_type,
        scene=scene,
        data=data,
        url=url,
        mime_type=mime_type,
        user_id=user_id,
    )
    base_detail: dict[str, Any] = {
        "media_type": media_type,
        "scene": scene,
        "mime_type": mime_type,
    }
    if url:
        base_detail["url"] = str(url)[:500]
    if data is not None:
        base_detail["bytes"] = len(data)
        base_detail["sha256"] = hashlib.sha256(data).hexdigest()

    try:
        provider = get_media_moderation_provider()
        result = provider.moderate(request)
    except Exception as e:  # noqa: BLE001 — provider 故障统一走失败策略
        fail_open = get_bool_setting(db, "media_moderation_fail_open", False)
        error_text = str(e)[:300] or type(e).__name__
        _log_media_moderation(
            db,
            user_id=user_id,
            biz_type=biz_type,
            biz_id=biz_id,
            ip=ip,
            detail={**base_detail, "decision": "error", "error": error_text, "fail_open": fail_open},
        )
        if fail_open:
            return MediaModerationResult(
                decision=DECISION_ALLOW,
                reason=f"审核服务异常,已按 fail-open 策略降级放行:{error_text}",
                provider="degraded",
            )
        raise MediaModerationUnavailable() from e

    _log_media_moderation(
        db,
        user_id=user_id,
        biz_type=biz_type,
        biz_id=biz_id,
        ip=ip,
        detail={
            **base_detail,
            "provider": result.provider,
            "decision": result.decision,
            "reason": result.reason[:500],
            "confidence": result.confidence,
        },
    )
    if result.decision == DECISION_REJECT:
        raise MediaModerationRejected(
            400,
            "内容安全拦截:媒体内容涉嫌违规,已被平台拒绝",
            decision=result.decision,
            reason=result.reason,
            confidence=result.confidence,
        )
    if result.decision != DECISION_ALLOW:
        # review 及任何未知结论都按需人工复审拦下,先审后发不允许漏放.
        raise MediaModerationRejected(
            400,
            "内容安全拦截:媒体内容需人工复审后才能使用",
            decision=DECISION_REVIEW,
            reason=result.reason,
            confidence=result.confidence,
        )
    return result


def assert_upload_media_allowed(
    db: Session,
    *,
    user_id: int,
    media_type: str,
    data: bytes | None = None,
    url: str | None = None,
    mime_type: str | None = None,
    ip: str | None = None,
) -> MediaModerationResult:
    """挂载点一:用户上传入口(uploads.py 参考图/参考视频落库前调用)."""
    return assert_media_allowed(
        db,
        media_type=media_type,
        scene=f"upload_{media_type}",
        data=data,
        url=url,
        mime_type=mime_type,
        user_id=user_id,
        biz_type="upload",
        ip=ip,
    )


def assert_generated_media_allowed(
    db: Session,
    *,
    task_id: int,
    user_id: int | None,
    media_type: str,
    data: bytes | None = None,
    url: str | None = None,
    mime_type: str | None = None,
) -> MediaModerationResult:
    """挂载点二:生成产出入口(GenAsset 落库/对用户可见前调用)."""
    return assert_media_allowed(
        db,
        media_type=media_type,
        scene="generation_output",
        data=data,
        url=url,
        mime_type=mime_type,
        user_id=user_id,
        biz_type="gen_task",
        biz_id=task_id,
    )
