"""图像/视频机器审核 provider 抽象(可插拔接入层).

设计目标:
- ``noop``(默认):未配置任何供应商时零出网、零拦截,行为与历史版本完全一致,
  与 ``content_safety.assert_text_allowed`` 的 kill-switch 语义保持同一风格.
- ``http`` 骨架:一个"由配置描述请求/响应映射"的通用 HTTP 审核网关客户端,
  不绑定、不虚构任何具体供应商的 SDK 或 API 形状.国内主流供应商(内容安全
  类云服务)普遍使用私有签名协议,接入时需要在其之上再写一层适配器,或部署
  一个转发网关把本骨架的通用协议翻译成供应商协议——见服务内注释与集成说明.

接口约定(输入媒体引用/字节流 -> 输出 允许/拒绝/需人工复审 + 原因 + 置信度):
    provider.moderate(MediaModerationRequest) -> MediaModerationResult
provider 内部错误(网络、超时、配置缺失、响应无法解析)一律抛 MediaModerationError,
失败放行/失败拒绝策略由上层 ``content_safety.assert_media_allowed`` 统一决定.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Any

import httpx

from ..config import settings
from .ssrf import SsrfError, pinned_client

# 审核结论(先审后发三态)
DECISION_ALLOW = "allow"    # 机审通过,允许发布
DECISION_REJECT = "reject"  # 机审判定违规,拒绝
DECISION_REVIEW = "review"  # 机审不确定,需人工复审后才能发布

_DEFAULT_HTTP_TIMEOUT_SECONDS = 10
_DEFAULT_MAX_INLINE_BYTES = 10 * 1024 * 1024  # 超过则必须走 url 引用送审


def _cfg(name: str, default):
    """读取 settings 中的审核配置.

    这些键由集成阶段统一补进 config.py / .env.example(见 needsConfig);在键
    落地之前 getattr 回退默认值,保证默认 noop 行为不受影响.
    """
    return getattr(settings, name, default)


class MediaModerationError(Exception):
    """provider 调用失败(网络/超时/配置缺失/响应无法解析)."""


@dataclass(frozen=True)
class MediaModerationRequest:
    """一次机器审核请求:媒体引用(url)或字节流(data)至少提供其一."""

    media_type: str                 # "image" | "video"
    scene: str = ""                 # 业务场景,如 upload_image / generation_output
    data: bytes | None = None       # 原始媒体字节
    url: str | None = None          # 可访问的媒体地址(大视频建议只传 url)
    mime_type: str | None = None
    user_id: int | None = None


@dataclass(frozen=True)
class MediaModerationResult:
    decision: str                   # DECISION_ALLOW / DECISION_REJECT / DECISION_REVIEW
    reason: str = ""                # 供人工复核的原因/命中标签
    confidence: float | None = None  # 置信度(0~1),供应商未提供则为 None
    provider: str = "noop"
    raw: dict | None = None         # 供应商原始响应,落审计供复核


class MediaModerationProvider:
    """图像/视频机器审核 provider 接口."""

    name = "base"

    def moderate(self, request: MediaModerationRequest) -> MediaModerationResult:
        raise NotImplementedError


class NoopModerationProvider(MediaModerationProvider):
    """默认 provider:不做任何审核,始终放行(与未接入机审时的历史行为一致)."""

    name = "noop"

    def moderate(self, request: MediaModerationRequest) -> MediaModerationResult:
        del request
        return MediaModerationResult(
            decision=DECISION_ALLOW,
            reason="未接入机器审核供应商,默认放行",
            provider=self.name,
        )


def _dig(payload: Any, path: str) -> Any:
    """按点分路径(如 result.suggestion 或 items.0.label)取响应字段."""
    cur = payload
    for part in [p for p in str(path or "").split(".") if p]:
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return None
        else:
            return None
    return cur


def _csv_values(raw: Any, default: str) -> frozenset[str]:
    text = str(raw if raw is not None else default) or default
    return frozenset(v.strip().lower() for v in text.split(",") if v.strip())


class HttpModerationProvider(MediaModerationProvider):
    """通用 HTTP 审核网关骨架(待适配,不绑定具体供应商).

    请求协议(POST JSON,可选 Bearer 鉴权):
        {"media_type": "image", "scene": "...", "mime_type": "...",
         "url": "..." 或 "data_base64": "..."}
    响应映射完全由配置描述:结论字段取点分路径 ``decision_field``,其取值按
    allow/reject/review 三组集合归类,原因与置信度同样按路径提取.因此只要
    供应商(或自建转发网关)能返回 JSON,就可以零代码接入;使用私有签名协议
    的供应商需要另写适配器或经网关转发,本骨架不虚构其 API 形状.
    """

    name = "http"

    def __init__(
        self,
        *,
        url: str | None = None,
        api_key: str | None = None,
        timeout_seconds: float | None = None,
        max_inline_bytes: int | None = None,
        decision_field: str | None = None,
        reason_field: str | None = None,
        confidence_field: str | None = None,
        allow_values: str | None = None,
        reject_values: str | None = None,
        review_values: str | None = None,
    ) -> None:
        self.url = str(url if url is not None else _cfg("media_moderation_http_url", ""))
        self.api_key = str(api_key if api_key is not None else _cfg("media_moderation_http_api_key", ""))
        self.timeout_seconds = float(
            timeout_seconds
            if timeout_seconds is not None
            else _cfg("media_moderation_http_timeout_seconds", _DEFAULT_HTTP_TIMEOUT_SECONDS)
        )
        self.max_inline_bytes = int(
            max_inline_bytes
            if max_inline_bytes is not None
            else _cfg("media_moderation_http_max_inline_bytes", _DEFAULT_MAX_INLINE_BYTES)
        )
        self.decision_field = str(
            decision_field if decision_field is not None else _cfg("media_moderation_http_decision_field", "decision")
        )
        self.reason_field = str(
            reason_field if reason_field is not None else _cfg("media_moderation_http_reason_field", "reason")
        )
        self.confidence_field = str(
            confidence_field
            if confidence_field is not None
            else _cfg("media_moderation_http_confidence_field", "confidence")
        )
        self.allow_values = _csv_values(
            allow_values if allow_values is not None else _cfg("media_moderation_http_allow_values", None),
            "allow,pass,ok",
        )
        self.reject_values = _csv_values(
            reject_values if reject_values is not None else _cfg("media_moderation_http_reject_values", None),
            "reject,block,deny",
        )
        self.review_values = _csv_values(
            review_values if review_values is not None else _cfg("media_moderation_http_review_values", None),
            "review,suspect,manual",
        )

    def _build_payload(self, request: MediaModerationRequest) -> dict:
        payload: dict[str, Any] = {
            "media_type": request.media_type,
            "scene": request.scene,
            "mime_type": request.mime_type,
        }
        if request.url:
            payload["url"] = request.url
        if request.data is not None and len(request.data) <= self.max_inline_bytes:
            payload["data_base64"] = base64.b64encode(request.data).decode("ascii")
        if not payload.get("url") and "data_base64" not in payload:
            if request.data is not None:
                raise MediaModerationError("媒体内容超过内联审核上限且缺少可访问地址,无法送审")
            raise MediaModerationError("缺少媒体地址或字节流,无法送审")
        return payload

    def moderate(self, request: MediaModerationRequest) -> MediaModerationResult:
        gateway_url = (self.url or "").strip()
        if not gateway_url:
            raise MediaModerationError("MEDIA_MODERATION_HTTP_URL 未配置,无法执行机器审核")
        payload = self._build_payload(request)
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        try:
            with pinned_client(gateway_url, timeout=self.timeout_seconds) as client:
                res = client.post(gateway_url, headers=headers, json=payload)
        except (httpx.HTTPError, SsrfError) as e:
            raise MediaModerationError(f"审核网关调用失败:{e}") from e
        if res.status_code >= 400:
            raise MediaModerationError(f"审核网关返回 {res.status_code}")
        try:
            body = res.json()
        except ValueError as e:
            raise MediaModerationError("审核网关响应不是有效 JSON") from e

        raw_decision = _dig(body, self.decision_field)
        decision_text = str(raw_decision if raw_decision is not None else "").strip().lower()
        reason = str(_dig(body, self.reason_field) or "")
        if decision_text in self.allow_values:
            decision = DECISION_ALLOW
        elif decision_text in self.reject_values:
            decision = DECISION_REJECT
        elif decision_text in self.review_values:
            decision = DECISION_REVIEW
        else:
            # 未识别的结论按"需人工复审"处理:先审后发下宁可多审,不可漏放.
            decision = DECISION_REVIEW
            reason = f"未识别的审核结论[{decision_text or '空'}]" + (f":{reason}" if reason else "")
        confidence: float | None = None
        raw_confidence = _dig(body, self.confidence_field)
        if raw_confidence is not None:
            try:
                confidence = float(raw_confidence)
            except (TypeError, ValueError):
                confidence = None
        return MediaModerationResult(
            decision=decision,
            reason=reason,
            confidence=confidence,
            provider=self.name,
            raw=body if isinstance(body, dict) else None,
        )


def implemented_providers() -> set[str]:
    return {"noop", "http"}


def readiness_issues() -> list[str]:
    """开启媒体机审开关前的就绪检查(与 sms.readiness_issues 同一模式).

    noop 视为未就绪:它零拦截,开着开关却什么都不审会造成"已合规"的假象.
    """
    provider = str(_cfg("media_moderation_provider", "noop")).strip().lower() or "noop"
    if provider not in implemented_providers():
        return [f"MEDIA_MODERATION_PROVIDER={provider} 暂未实现"]
    if provider == "noop":
        return [
            "MEDIA_MODERATION_PROVIDER=noop 不会执行任何审核,"
            "请配置 http 审核网关后再开启"
        ]
    issues: list[str] = []
    url = str(_cfg("media_moderation_http_url", "")).strip()
    if not url:
        issues.append("MEDIA_MODERATION_HTTP_URL 未配置")
    elif not url.lower().startswith(("http://", "https://")):
        issues.append("MEDIA_MODERATION_HTTP_URL 必须是 http(s) 地址")
    return issues


def get_media_moderation_provider(name: str | None = None) -> MediaModerationProvider:
    provider = (name if name is not None else str(_cfg("media_moderation_provider", "noop"))).strip().lower() or "noop"
    if provider == "noop":
        return NoopModerationProvider()
    if provider == "http":
        return HttpModerationProvider()
    raise MediaModerationError(f"MEDIA_MODERATION_PROVIDER={provider} 暂未实现")
