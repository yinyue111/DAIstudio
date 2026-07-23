"""Auto-generated from schemas.py split."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class RegisterIn(BaseModel):
    phone: str
    password: str = Field(max_length=128)
    sms_code: str | None = None
    nickname: str | None = None


class SmsCodeIn(BaseModel):
    phone: str


class LoginIn(BaseModel):
    phone: str
    password: str = Field(max_length=128)


class TokenOut(BaseModel):
    access_token: str | None = None
    token_type: str = "bearer"


class ChangePasswordIn(BaseModel):
    old_password: str = Field(max_length=128)
    new_password: str = Field(max_length=128)


class ResetPasswordIn(BaseModel):
    password: str = Field(max_length=128)


class AdminTaskRefundIn(BaseModel):
    note: str | None = Field(default=None, max_length=255)


class AdminTaskSettleIn(BaseModel):
    result_url: str | None = Field(default=None, max_length=2048)
    external_task_id: str | None = Field(default=None, max_length=256)
    note: str | None = Field(default=None, max_length=255)


class AdminAssetReportHandleIn(BaseModel):
    action: Literal["dismiss", "takedown"]
    note: str | None = Field(default=None, max_length=500)
