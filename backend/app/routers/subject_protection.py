"""Subject-protection preflight for product image edits."""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import get_current_user
from ..models import User
from ..services.generation_media import (
    EDIT_MASK_SEND_CONFIDENCE,
    EditMaskResult,
    SubjectProtectionBusy,
    gateway_image_edit_mask,
)

router = APIRouter(prefix="/api/subject-protection", tags=["subject-protection"])

EditMaskRequestMode = Literal["protect_subject", "center_box", "off"]


class SubjectProtectionPreviewIn(BaseModel):
    asset_url: str
    edit_mask_mode: EditMaskRequestMode = "protect_subject"


class SubjectProtectionPreviewOut(BaseModel):
    mode: str
    confidence: float
    bbox: list[int] | None
    width: int
    height: int
    mask_data_uri: str | None
    will_send_mask: bool
    pixel_lock_recommended: bool
    risk_level: Literal["low", "medium", "high"]
    title: str
    message: str
    recommendations: list[str]


def _will_send_mask(result: EditMaskResult | None, requested_mode: str) -> bool:
    if not result or not result.data_uri:
        return False
    if result.mode in {"alpha_subject", "auto_subject"}:
        return result.confidence >= EDIT_MASK_SEND_CONFIDENCE
    return requested_mode == "center_box" and result.mode == "center_box"


def _preview_message(
    result: EditMaskResult | None,
    *,
    requested_mode: str,
    will_send_mask: bool,
) -> tuple[str, str, str, list[str]]:
    if requested_mode == "off":
        return (
            "high",
            "整图编辑",
            "已关闭蒙版保护，模型会重绘整张图，产品文字和包装结构更容易漂移。",
            ["仅在需要整体风格化时使用整图编辑。", "需要保留文字/Logo 时建议切回主体保护或上传透明 PNG。"],
        )
    if not result or result.mode == "none":
        return (
            "high",
            "未识别到可靠主体",
            "当前图片无法生成可发送的主体蒙版，系统不会假装已保护产品主体。",
            ["换一张主体更清晰、边缘更完整的产品图。", "白色产品建议使用透明 PNG 或手动改用中心保护兼容模式。"],
        )
    if result.mode == "alpha_subject":
        return (
            "low",
            "透明 PNG 精确保护",
            "已使用真实透明通道生成主体蒙版，适合高保真保留产品轮廓和包装文字。",
            ["生成时仍建议避免水花、草叶或道具遮挡包装正面。"],
        )
    if result.mode == "center_box":
        return (
            "medium",
            "中心区域保护",
            "这是兼容兜底，只保护画面中心区域，不等于真正识别产品主体。",
            ["主体必须居中且占画面主体区域。", "如果背景也在中心区域，会被一起保护，场景变化会变少。"],
        )
    if result.mode == "auto_subject" and will_send_mask:
        if result.confidence >= 0.78:
            return (
                "low",
                "自动主体保护较稳",
                "已识别出产品主体并会发送蒙版，优先重绘背景和场景。",
                ["包装小字多时建议开启产品像素锁定或上传透明 PNG。"],
            )
        return (
            "medium",
            "自动主体保护可用",
            "已识别出产品主体并会发送蒙版，但边缘置信度一般，细小文字仍可能漂移。",
            ["产品边缘复杂或白底白产品时，建议检查生成结果或改用透明 PNG。"],
        )
    return (
        "high",
        "低置信主体识别",
        "自动识别置信度不足，生成时不会发送蒙版，避免误保护背景或切坏产品。",
        ["建议上传透明 PNG。", "或者在主体居中时手动选择中心保护兼容模式。"],
    )


def _preview_payload(result: EditMaskResult | None, requested_mode: str) -> SubjectProtectionPreviewOut:
    will_send = _will_send_mask(result, requested_mode)
    risk, title, message, recommendations = _preview_message(
        result,
        requested_mode=requested_mode,
        will_send_mask=will_send,
    )
    confidence = round(float(result.confidence), 3) if result else 0.0
    return SubjectProtectionPreviewOut(
        mode=result.mode if result else "none",
        confidence=confidence,
        bbox=list(result.bbox) if result and result.bbox else None,
        width=int(result.width) if result else 0,
        height=int(result.height) if result else 0,
        mask_data_uri=result.data_uri if result and will_send else None,
        will_send_mask=will_send,
        pixel_lock_recommended=bool(
            result
            and result.mode in {"alpha_subject", "auto_subject"}
            and result.confidence >= 0.78
            and will_send
        ),
        risk_level=risk,
        title=title,
        message=message,
        recommendations=recommendations,
    )


@router.post("/preview", response_model=SubjectProtectionPreviewOut)
def preview_subject_protection(
    body: SubjectProtectionPreviewIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    mode = body.edit_mask_mode
    if mode == "off":
        return _preview_payload(None, mode)
    try:
        result = gateway_image_edit_mask(
            db,
            int(user.id),
            body.asset_url,
            max_side=1024,
            edit_mask_mode=mode,
        )
    except SubjectProtectionBusy as e:
        raise HTTPException(429, str(e)) from e
    except RuntimeError as e:
        detail = str(e)
        status_code = 404 if "不存在" in detail else 400
        raise HTTPException(status_code, detail) from e
    return _preview_payload(result, mode)
