"""Pydantic schemas for WebAuthn (passkey) endpoints."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class WebAuthnOptionsResponse(BaseModel):
    """PublicKeyCredential creation/request options, in WebAuthn JSON form."""

    options: dict[str, Any]


class WebAuthnCompleteRegistrationRequest(BaseModel):
    credential: dict[str, Any]
    device_name: str | None = Field(default=None, max_length=150)


class WebAuthnLoginCompleteRequest(BaseModel):
    credential: dict[str, Any]


class WebAuthnCredentialResponse(BaseModel):
    id: int
    device_name: str | None
    transports: list[str]
    created_at: datetime
    last_used_at: datetime | None


class WebAuthnStatusResponse(BaseModel):
    credentials_exist: bool
