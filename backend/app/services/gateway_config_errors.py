"""Stable, non-secret HTTP mapping for generation gateway configuration failures."""
from __future__ import annotations

from fastapi import HTTPException

from .generation_model_runtime import ModelSnapshotMismatchError
from .model_gateway_config import ModelGatewayConfigError


def gateway_config_error_contract(exc: Exception) -> tuple[int, dict[str, str]]:
    if isinstance(exc, ModelSnapshotMismatchError):
        return 409, {
            "code": "MODEL_GATEWAY_SNAPSHOT_STALE",
            "message": "任务绑定的模型网关配置已失效,请重新报价后提交",
        }
    if isinstance(exc, ModelGatewayConfigError):
        message = str(exc)
        transient = any(
            marker in message
            for marker in (
                "SECRET 未配置",
                "解密失败",
                "连接不可用",
            )
        )
        if transient:
            return 503, {
                "code": "MODEL_GATEWAY_RUNTIME_UNAVAILABLE",
                "message": "模型网关运行配置暂不可用,请联系管理员或稍后重试",
            }
    return 409, {
        "code": "MODEL_GATEWAY_CONFIG_INVALID",
        "message": "模型网关配置无效,请联系管理员检查配置",
    }


def gateway_config_http_exception(exc: Exception) -> HTTPException:
    status_code, detail = gateway_config_error_contract(exc)
    return HTTPException(status_code=status_code, detail=detail)


def raise_gateway_config_http(exc: Exception) -> None:
    raise gateway_config_http_exception(exc) from exc
