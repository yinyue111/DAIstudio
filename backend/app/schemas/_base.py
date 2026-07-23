"""Shared base for Pydantic schemas: imports, constants, type aliases, helpers.
Everything imported here is re-exported for domain submodules to use.
"""

from __future__ import annotations  # noqa: F401

import json  # noqa: F401
from datetime import datetime, timezone  # noqa: F401
from math import isfinite  # noqa: F401
from typing import Annotated, Any, Literal  # noqa: F401

from pydantic import (  # noqa: F401
    BaseModel,
    ConfigDict,
    Field,
    PlainSerializer,
    field_validator,
    model_validator,
)

MAX_MODEL_COST_CREDITS = 1_000_000
MAX_PAYMENT_AMOUNT_CENTS = 1_000_000_00  # 1,000,000 CNY
MAX_PAYMENT_PACKAGE_CREDITS = 100_000_000
MAX_PAYMENT_CREDITS_PER_CENT = 10_000
MAX_REVERSE_SOURCE_RANGES = 8
MAX_REVERSE_SELECTED_DURATION_SECONDS = 300

ModelUse = Literal["vision", "image", "video", "prompt"]
ModelProvider = Literal[
    "openai",
    "volcengine_ark",
    "openrouter",
    "siliconflow",
    "deepseek",
    "moonshot",
    "zhipu",
    "dashscope",
    "baidu_qianfan",
    "tencent_hunyuan",
    "yinyue",
    "anthropic",
    "antigravity",
    "grok",
    "gemini",
    "custom_openai",
]
ModelGatewayFormat = Literal["openai", "ark", "anthropic"]
PromptOptimizationDirection = Literal[
    "faithful",
    "concise",
    "expand",
    "commercial",
    "cinematic",
    "model_adaptation",
    "constraints",
    "translate",
]


def _serialize_utc_datetime(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    else:
        value = value.astimezone(timezone.utc)
    return value.isoformat()


UtcDateTime = Annotated[
    datetime,
    PlainSerializer(_serialize_utc_datetime, return_type=str, when_used="json"),
]


_DANGEROUS_JSON_KEYS = frozenset({"__proto__", "prototype", "constructor"})
_ROUTE_SECRET_JSON_KEYS = frozenset(
    {
        "api_key",
        "apikey",
        "authorization",
        "proxy_authorization",
        "access_token",
        "refresh_token",
        "secret",
        "client_secret",
        "password",
        "cookie",
        "set_cookie",
    }
)


def _reject_dangerous_json_keys(value: Any, path: str = "image_evidence") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = str(key).strip().lower()
            if normalized in _DANGEROUS_JSON_KEYS:
                raise ValueError(f"{path} 包含危险字段 {key}")
            _reject_dangerous_json_keys(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_dangerous_json_keys(item, f"{path}[{index}]")


def _reject_route_secret_json_keys(value: Any, path: str = "model_route.extra") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = str(key).strip().lower().replace("-", "_")
            if normalized in _ROUTE_SECRET_JSON_KEYS:
                raise ValueError(f"{path} 不能包含密钥字段 {key},请使用专用 API Key 字段")
            _reject_route_secret_json_keys(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_route_secret_json_keys(item, f"{path}[{index}]")


def _normalize_asset_tags(values: list[str]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for raw in values:
        value = " ".join(str(raw or "").split()).strip("# ")
        if not value or len(value) > 32 or any(ord(char) < 32 for char in value):
            raise ValueError("素材标签应为 1-32 个可见字符")
        key = value.casefold()
        if key in seen:
            continue
        seen.add(key)
        normalized.append(value)
    return normalized


_SNAPSHOT_SECRET_KEY_PARTS = (
    "api_key",
    "apikey",
    "token",
    "secret",
    "password",
    "authorization",
    "cookie",
    "credential",
    "private_key",
    "auth_key",
)
_SNAPSHOT_BINARY_KEY_PARTS = ("base64", "binary", "blob", "file_content", "raw_bytes")


def _validate_workspace_snapshot(value: dict[str, Any]) -> None:
    """Reject secrets and embedded media while keeping Studio metadata extensible."""

    def walk(node: Any, *, path: str, depth: int) -> None:
        if depth > 12:
            raise ValueError("工作区快照嵌套过深")
        if isinstance(node, dict):
            for raw_key, child in node.items():
                if not isinstance(raw_key, str):
                    raise ValueError("工作区快照的字段名必须是字符串")
                key = raw_key.strip().lower().replace("-", "_")
                child_path = f"{path}.{raw_key}" if path else raw_key
                if any(part in key for part in _SNAPSHOT_SECRET_KEY_PARTS):
                    raise ValueError(f"工作区快照不得包含密钥或凭据字段: {child_path}")
                if any(part in key for part in _SNAPSHOT_BINARY_KEY_PARTS):
                    raise ValueError(f"工作区快照不得包含二进制内容字段: {child_path}")
                walk(child, path=child_path, depth=depth + 1)
            return
        if isinstance(node, list):
            for index, child in enumerate(node):
                walk(child, path=f"{path}[{index}]", depth=depth + 1)
            return
        if isinstance(node, str) and node.lstrip().lower().startswith("data:"):
            raise ValueError(f"工作区快照不得内嵌 data URI: {path}")

    walk(value, path="", depth=0)


def re_match_package_id(value: str) -> bool:
    import re

    return bool(re.fullmatch(r"[A-Za-z0-9_-]+", value))


def _validate_json_payload_size(value: dict[str, Any], max_bytes: int, label: str) -> None:
    try:
        raw = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError) as e:
        raise ValueError(f"{label} 必须是可序列化 JSON") from e
    if len(raw) > max_bytes:
        raise ValueError(f"{label} 不能超过 {max_bytes // 1024}KB")
