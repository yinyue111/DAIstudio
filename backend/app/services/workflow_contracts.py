"""Shared contracts for the tool workflow engine.

Error types, node execution value objects, terminal-status sets and the
handler registries that adapters register into. Kept dependency-free so both
the engine (``tool_workflows``) and the run factory can import it.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

TERMINAL_RUN_STATUSES = {"succeeded", "failed", "canceled"}
TERMINAL_NODE_STATUSES = {"succeeded", "failed", "canceled"}
DEFAULT_NODE_LEASE_SECONDS = 15 * 60


class WorkflowServiceError(Exception):
    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code


class WorkflowNotFound(WorkflowServiceError):
    def __init__(self, message: str = "工作流运行不存在"):
        super().__init__(404, message)


class WorkflowConflict(WorkflowServiceError):
    def __init__(self, message: str):
        super().__init__(409, message)


@dataclass(frozen=True)
class NodeExecutionContext:
    workflow_run_id: int
    tool_run_id: int
    node_run_id: int
    node_key: str
    node_type: str
    attempt_number: int
    workflow_input: dict[str, Any]
    dependency_outputs: dict[str, Any]
    node_input: dict[str, Any]
    config: dict[str, Any]
    previous_output: dict[str, Any] | None = None
    external_kind: str | None = None
    external_id: str | None = None


@dataclass(frozen=True)
class NodeExecutionResult:
    status: str
    output: dict[str, Any] = field(default_factory=dict)
    error_code: str | None = None
    error: str | None = None
    external_kind: str | None = None
    external_id: str | None = None

    @classmethod
    def succeeded(cls, output: dict[str, Any] | None = None):
        return cls(status="succeeded", output=dict(output or {}))

    @classmethod
    def failed(
        cls,
        error: str,
        *,
        code: str = "NODE_FAILED",
        output: dict[str, Any] | None = None,
    ):
        return cls(
            status="failed",
            output=dict(output or {}),
            error_code=code,
            error=str(error),
        )

    @classmethod
    def unsupported(cls, capability: str, *, reason: str):
        return cls.failed(
            reason,
            code="NODE_CAPABILITY_UNSUPPORTED",
            output={
                "capability": str(capability),
                "capability_status": "unsupported",
                "reason": str(reason),
            },
        )

    @classmethod
    def waiting_external(
        cls,
        *,
        external_kind: str,
        external_id: str,
        output: dict[str, Any] | None = None,
    ):
        return cls(
            status="waiting_external",
            output=dict(output or {}),
            external_kind=external_kind,
            external_id=external_id,
        )


NodeHandler = Callable[[NodeExecutionContext], NodeExecutionResult]
_NODE_HANDLERS: dict[str, NodeHandler] = {}
_COMPENSATION_HANDLERS: dict[str, NodeHandler] = {}


def register_node_handler(node_type: str, handler: NodeHandler) -> None:
    if node_type not in {"parse", "reverse", "compile", "generate", "compose", "export"}:
        raise ValueError("不支持的工作流节点处理器")
    _NODE_HANDLERS[node_type] = handler


def unregister_node_handler(node_type: str) -> None:
    _NODE_HANDLERS.pop(node_type, None)


def register_compensation_handler(compensation_type: str, handler: NodeHandler) -> None:
    _COMPENSATION_HANDLERS[compensation_type] = handler


def unregister_compensation_handler(compensation_type: str) -> None:
    _COMPENSATION_HANDLERS.pop(compensation_type, None)


def has_node_handler(node_type: str) -> bool:
    return node_type in _NODE_HANDLERS


def has_compensation_handler(compensation_type: str) -> bool:
    return compensation_type in _COMPENSATION_HANDLERS


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
