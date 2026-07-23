"""Strict contracts for versioned executable tool workflows."""
from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MAX_WORKFLOW_NODES = 64
MAX_WORKFLOW_JSON_BYTES = 256 * 1024
MAX_NODE_JSON_BYTES = 64 * 1024
MAX_RUN_INPUT_BYTES = 256 * 1024

WorkflowNodeType = Literal[
    "parse",
    "reverse",
    "manual_review",
    "compile",
    "generate",
    "compose",
    "export",
]
WorkflowRunStatus = Literal[
    "queued",
    "running",
    "waiting_review",
    "succeeded",
    "failed",
    "canceled",
    "compensating",
]
StudioCreationMode = Literal["image", "image_edit", "video", "video_edit"]


def _json_size(value: Any) -> int:
    try:
        return len(
            json.dumps(
                value,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("工作流配置必须是可序列化 JSON") from exc


def _bounded_json(value: dict[str, Any], *, limit: int, label: str) -> dict[str, Any]:
    if _json_size(value) > limit:
        raise ValueError(f"{label}超过 {limit // 1024} KiB 限制")
    return value


class WorkflowCompensationSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_.-]*$")
    config: dict[str, Any] = Field(default_factory=dict)

    @field_validator("config")
    @classmethod
    def _config_size(cls, value: dict[str, Any]) -> dict[str, Any]:
        return _bounded_json(value, limit=MAX_NODE_JSON_BYTES, label="补偿配置")


class WorkflowNodeSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_-]*$")
    type: WorkflowNodeType
    depends_on: list[str] = Field(default_factory=list, max_length=MAX_WORKFLOW_NODES)
    config: dict[str, Any] = Field(default_factory=dict)
    max_attempts: int = Field(default=3, ge=1, le=10)
    side_effect: bool = False
    compensation: WorkflowCompensationSpec | None = None

    @field_validator("type", mode="before")
    @classmethod
    def _review_alias(cls, value: Any) -> Any:
        return "manual_review" if value == "human_review" else value

    @field_validator("depends_on")
    @classmethod
    def _unique_dependencies(cls, value: list[str]) -> list[str]:
        normalized = [str(item).strip() for item in value]
        if any(not item for item in normalized):
            raise ValueError("节点依赖不能为空")
        if len(normalized) != len(set(normalized)):
            raise ValueError("节点依赖不能重复")
        return normalized

    @field_validator("config")
    @classmethod
    def _config_size(cls, value: dict[str, Any]) -> dict[str, Any]:
        return _bounded_json(value, limit=MAX_NODE_JSON_BYTES, label="节点配置")

    @model_validator(mode="after")
    def _side_effect_has_compensation(self):
        if self.side_effect and self.compensation is None:
            raise ValueError("声明 side_effect 的节点必须配置 compensation")
        return self


class WorkflowStudioPreset(BaseModel):
    """Optional Studio entry metadata for an executable workflow."""

    model_config = ConfigDict(extra="forbid")

    creation_mode: StudioCreationMode
    analysis_focus: str | None = Field(default=None, min_length=1, max_length=64)
    output_purpose: str | None = Field(default=None, min_length=1, max_length=64)
    reference_roles: list[str] = Field(default_factory=list, max_length=20)
    message: str | None = Field(default=None, min_length=1, max_length=500)

    @field_validator("analysis_focus", "output_purpose", "message")
    @classmethod
    def _clean_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("Studio 预设文本不能为空")
        return normalized

    @field_validator("reference_roles")
    @classmethod
    def _clean_reference_roles(cls, value: list[str]) -> list[str]:
        normalized = [str(item).strip() for item in value]
        if any(not item or len(item) > 64 for item in normalized):
            raise ValueError("Studio 参考素材角色非法")
        if len(normalized) != len(set(normalized)):
            raise ValueError("Studio 参考素材角色不能重复")
        return normalized


class WorkflowSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["workflow.v1"] = "workflow.v1"
    nodes: list[WorkflowNodeSpec] = Field(min_length=1, max_length=MAX_WORKFLOW_NODES)
    output_node: str | None = Field(default=None, max_length=64)
    studio_preset: WorkflowStudioPreset | None = None

    @model_validator(mode="after")
    def _validate_dag(self):
        keys = [node.key for node in self.nodes]
        if len(keys) != len(set(keys)):
            raise ValueError("工作流节点 key 必须唯一")
        known = set(keys)
        for node in self.nodes:
            if node.key in node.depends_on:
                raise ValueError(f"节点 {node.key} 不能依赖自身")
            missing = sorted(set(node.depends_on) - known)
            if missing:
                raise ValueError(f"节点 {node.key} 依赖不存在的节点: {', '.join(missing)}")
        if self.output_node is not None and self.output_node not in known:
            raise ValueError("output_node 必须引用已存在的节点")
        self.topological_keys()
        _bounded_json(
            self.model_dump(mode="json"),
            limit=MAX_WORKFLOW_JSON_BYTES,
            label="工作流",
        )
        return self

    def topological_keys(self) -> list[str]:
        order = {node.key: index for index, node in enumerate(self.nodes)}
        indegree = {node.key: len(node.depends_on) for node in self.nodes}
        children: dict[str, list[str]] = {node.key: [] for node in self.nodes}
        for node in self.nodes:
            for dependency in node.depends_on:
                children.setdefault(dependency, []).append(node.key)
        ready = sorted(
            (key for key, degree in indegree.items() if degree == 0),
            key=order.__getitem__,
        )
        result: list[str] = []
        while ready:
            current = ready.pop(0)
            result.append(current)
            for child in sorted(children.get(current, []), key=order.__getitem__):
                indegree[child] -= 1
                if indegree[child] == 0:
                    ready.append(child)
                    ready.sort(key=order.__getitem__)
        if len(result) != len(self.nodes):
            raise ValueError("工作流依赖必须是无环 DAG")
        return result


class ToolRunCreateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool_slug: str = Field(min_length=2, max_length=64, pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    # POST /api/quotes validates the same intent without a quote id; the run
    # creation route rejects an execution request that omits it.
    quote_id: int | None = Field(default=None, gt=0)
    project_id: int | None = Field(default=None, gt=0)
    client_request_id: str = Field(min_length=8, max_length=128)
    input: dict[str, Any] = Field(default_factory=dict)

    @field_validator("client_request_id")
    @classmethod
    def _request_id(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("client_request_id 不能为空")
        return normalized

    @field_validator("input")
    @classmethod
    def _input_size(cls, value: dict[str, Any]) -> dict[str, Any]:
        return _bounded_json(value, limit=MAX_RUN_INPUT_BYTES, label="工作流输入")


class WorkflowReviewIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["approve", "reject"]
    output: dict[str, Any] = Field(default_factory=dict)
    note: str | None = Field(default=None, max_length=1000)

    @field_validator("output")
    @classmethod
    def _output_size(cls, value: dict[str, Any]) -> dict[str, Any]:
        return _bounded_json(value, limit=MAX_NODE_JSON_BYTES, label="审核输出")


class WorkflowExternalCompletionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["succeeded", "failed"]
    output: dict[str, Any] = Field(default_factory=dict)
    error_code: str | None = Field(default=None, max_length=64)
    error: str | None = Field(default=None, max_length=2000)
    external_kind: str = Field(min_length=1, max_length=32)
    external_id: str = Field(min_length=1, max_length=256)

    @field_validator("output")
    @classmethod
    def _output_size(cls, value: dict[str, Any]) -> dict[str, Any]:
        return _bounded_json(value, limit=MAX_NODE_JSON_BYTES, label="外部节点输出")


class WorkflowRetryIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str | None = Field(default=None, max_length=500)
