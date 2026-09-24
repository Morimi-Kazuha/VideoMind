"""Pydantic HTTP schemas for the original DOVideo public contract.

The Java service exposes a small ``{code, message, data}`` envelope and
camelCase JSON records.  Request models accept the original field names while
also accepting Python spellings for tests and migration tooling.
"""

from __future__ import annotations

import re
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ApiModel(BaseModel):
    """Strict HTTP model with migration-friendly camelCase aliases."""

    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
        serialize_by_alias=True,
    )


class RegisterRequest(ApiModel):
    username: str
    password: str
    nickname: str | None = None

    @field_validator("username")
    @classmethod
    def validate_username(cls, value: str) -> str:
        value = value.strip()
        if not re.fullmatch(r"[A-Za-z0-9_]{3,32}", value):
            raise ValueError("账号需为 3-32 位字母、数字或下划线")
        return value

    @field_validator("password")
    @classmethod
    def validate_password(cls, value: str) -> str:
        if not isinstance(value, str) or not 8 <= len(value) <= 128:
            raise ValueError("密码需为 8-128 位")
        return value

    @field_validator("nickname")
    @classmethod
    def validate_nickname(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if len(value) > 50:
            raise ValueError("昵称不能超过 50 个字符")
        return value


class LoginRequest(ApiModel):
    username: str
    password: str
    nickname: str | None = None

    @field_validator("username")
    @classmethod
    def validate_username(cls, value: str) -> str:
        value = value.strip()
        if not re.fullmatch(r"[A-Za-z0-9_]{3,32}", value):
            raise ValueError("请输入有效账号")
        return value

    @field_validator("password")
    @classmethod
    def validate_password(cls, value: str) -> str:
        if not isinstance(value, str) or not value.strip() or len(value) > 128:
            raise ValueError("请输入有效密码")
        return value


class RouteRequest(ApiModel):
    goal: Annotated[str, Field(min_length=1, max_length=500)]

    @field_validator("goal")
    @classmethod
    def normalize_goal(cls, value: str) -> str:
        return value.strip()


class UserInfo(ApiModel):
    id: int
    username: str
    nickname: str
    avatar: str | None = None
    role: str = "USER"


class AuthData(ApiModel):
    user_info: Annotated[UserInfo, Field(alias="userInfo")]
    token: str | None = None


class MediaSummary(ApiModel):
    id: int
    filename: str
    status: str
    cover_url: Annotated[str | None, Field(alias="coverUrl")] = None
    upload_time: Annotated[str, Field(alias="uploadTime")]


class UploadInitData(ApiModel):
    upload_id: Annotated[str, Field(alias="uploadId")]


class UploadStatusData(ApiModel):
    upload_id: Annotated[str, Field(alias="uploadId")]
    filename: str
    total_chunks: Annotated[int, Field(alias="totalChunks")]
    uploaded_chunks: Annotated[tuple[int, ...], Field(alias="uploadedChunks")]
    completed_media_id: Annotated[int | None, Field(alias="completedMediaId")] = None


class RouteDecision(ApiModel):
    mode: str
    reason: str


class ErrorEnvelope(ApiModel):
    code: int
    message: str
    data: Any = None


__all__ = [
    "ApiModel",
    "AuthData",
    "ErrorEnvelope",
    "LoginRequest",
    "MediaSummary",
    "RegisterRequest",
    "RouteDecision",
    "RouteRequest",
    "UploadInitData",
    "UploadStatusData",
    "UserInfo",
]
