"""WebAuthn (passkey) registration and login.

Flow mirrors the existing password login: a successful passkey assertion
issues the same JWT that ``POST /auth/login`` issues. Passkey logins skip
the 2FA step — user verification (biometric/PIN) on the authenticator
already provides the second factor, matching industry practice.

Challenges are stored in ``AuthEphemeralToken`` (single-use, 5 min TTL) so
the flow works across multiple workers, same as the 2FA/OIDC flows.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url
from webauthn.helpers.exceptions import InvalidAuthenticationResponse, InvalidRegistrationResponse
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from backend.app.api.routes.settings import get_setting
from backend.app.core.auth import (
    create_access_token,
    get_current_active_user,
    resolve_session_max_minutes,
)
from backend.app.core.database import get_db
from backend.app.models.auth_ephemeral import AuthEphemeralToken, EventType, TokenType
from backend.app.models.user import User
from backend.app.models.webauthn_credential import WebAuthnCredential
from backend.app.schemas.auth import LoginResponse
from backend.app.schemas.webauthn import (
    WebAuthnCompleteRegistrationRequest,
    WebAuthnCredentialResponse,
    WebAuthnLoginCompleteRequest,
    WebAuthnOptionsResponse,
    WebAuthnStatusResponse,
)

_logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth/webauthn", tags=["webauthn"])

CHALLENGE_TTL = timedelta(minutes=5)
RP_NAME = "Bambuddy"


async def _get_rp(db: AsyncSession, request: Request) -> tuple[str, list[str]]:
    """Resolve the relying-party ID and allowed origins.

    Prefers the configured ``external_url`` setting (e.g.
    ``https://app.modcables101.com``); falls back to the request origin so
    development setups on ``localhost`` still work.
    """
    origins: list[str] = []
    rp_id: str | None = None

    external_url = await get_setting(db, "external_url")
    if external_url:
        parsed = urlparse(external_url.rstrip("/"))
        if parsed.hostname:
            rp_id = parsed.hostname
            origins.append(f"{parsed.scheme}://{parsed.netloc}")

    req_origin = request.headers.get("origin")
    if req_origin:
        req_host = urlparse(req_origin).hostname
        if rp_id is None and req_host:
            rp_id = req_host
        if req_origin not in origins:
            # Accept the request origin only when it matches the rp_id —
            # WebAuthn itself enforces rp_id/origin consistency, this just
            # keeps localhost development working alongside external_url.
            if req_host == rp_id:
                origins.append(req_origin)

    if not rp_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot determine relying party — set External URL in Settings",
        )
    return rp_id, origins


async def _store_challenge(db: AsyncSession, challenge: bytes, token_type: str, username: str | None) -> None:
    # One outstanding challenge per user+type keeps the table tidy
    if username:
        await db.execute(
            delete(AuthEphemeralToken).where(
                AuthEphemeralToken.token_type == token_type,
                AuthEphemeralToken.username == username,
            )
        )
    db.add(
        AuthEphemeralToken(
            token=bytes_to_base64url(challenge),
            token_type=token_type,
            username=username,
            expires_at=datetime.now(timezone.utc) + CHALLENGE_TTL,
        )
    )
    await db.commit()


async def _consume_challenge(db: AsyncSession, challenge_b64url: str, token_type: str) -> AuthEphemeralToken:
    result = await db.execute(
        select(AuthEphemeralToken).where(
            AuthEphemeralToken.token == challenge_b64url,
            AuthEphemeralToken.token_type == token_type,
        )
    )
    record = result.scalar_one_or_none()
    if record is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Unknown or expired challenge")
    await db.execute(delete(AuthEphemeralToken).where(AuthEphemeralToken.id == record.id))
    await db.commit()
    expires = record.expires_at if record.expires_at.tzinfo else record.expires_at.replace(tzinfo=timezone.utc)
    if expires < datetime.now(timezone.utc):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Unknown or expired challenge")
    return record


def _credential_to_response(cred: WebAuthnCredential) -> WebAuthnCredentialResponse:
    return WebAuthnCredentialResponse(
        id=cred.id,
        device_name=cred.device_name,
        transports=json.loads(cred.transports) if cred.transports else [],
        created_at=cred.created_at,
        last_used_at=cred.last_used_at,
    )


@router.get("/status", response_model=WebAuthnStatusResponse)
async def webauthn_status(db: AsyncSession = Depends(get_db)):
    """Public: whether any passkeys exist (drives the login-page button)."""
    result = await db.execute(select(WebAuthnCredential.id).limit(1))
    return WebAuthnStatusResponse(credentials_exist=result.scalar_one_or_none() is not None)


@router.get("/credentials", response_model=list[WebAuthnCredentialResponse])
async def list_credentials(
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(WebAuthnCredential)
        .where(WebAuthnCredential.user_id == current_user.id)
        .order_by(WebAuthnCredential.created_at)
    )
    return [_credential_to_response(c) for c in result.scalars().all()]


@router.delete("/credentials/{credential_id}")
async def delete_credential(
    credential_id: int,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(WebAuthnCredential).where(
            WebAuthnCredential.id == credential_id,
            WebAuthnCredential.user_id == current_user.id,
        )
    )
    cred = result.scalar_one_or_none()
    if cred is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Passkey not found")
    await db.delete(cred)
    await db.commit()
    return {"message": "Passkey removed"}


@router.post("/register/begin", response_model=WebAuthnOptionsResponse)
async def register_begin(
    request: Request,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    rp_id, _origins = await _get_rp(db, request)

    result = await db.execute(select(WebAuthnCredential).where(WebAuthnCredential.user_id == current_user.id))
    existing = result.scalars().all()

    options = generate_registration_options(
        rp_id=rp_id,
        rp_name=RP_NAME,
        user_id=str(current_user.id).encode(),
        user_name=current_user.username,
        user_display_name=current_user.username,
        exclude_credentials=[
            PublicKeyCredentialDescriptor(id=base64url_to_bytes(c.credential_id)) for c in existing
        ],
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.PREFERRED,
            user_verification=UserVerificationRequirement.REQUIRED,
        ),
    )
    await _store_challenge(db, options.challenge, TokenType.WEBAUTHN_REGISTER, current_user.username)
    return WebAuthnOptionsResponse(options=json.loads(options_to_json(options)))


@router.post("/register/complete", response_model=WebAuthnCredentialResponse)
async def register_complete(
    request: Request,
    body: WebAuthnCompleteRegistrationRequest,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    rp_id, origins = await _get_rp(db, request)

    credential_json = json.dumps(body.credential)
    try:
        client_data = json.loads(
            base64url_to_bytes(body.credential["response"]["clientDataJSON"])
        )
        expected_challenge = client_data["challenge"]
    except (KeyError, ValueError, TypeError):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Malformed credential")

    record = await _consume_challenge(db, expected_challenge, TokenType.WEBAUTHN_REGISTER)
    if record.username != current_user.username:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Challenge does not belong to this user")

    try:
        verification = verify_registration_response(
            credential=credential_json,
            expected_challenge=base64url_to_bytes(expected_challenge),
            expected_origin=origins if len(origins) > 1 else origins[0],
            expected_rp_id=rp_id,
            require_user_verification=True,
        )
    except InvalidRegistrationResponse as e:
        _logger.warning("Passkey registration failed for %s: %s", current_user.username, e)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Passkey verification failed")

    transports = body.credential.get("response", {}).get("transports", [])
    cred = WebAuthnCredential(
        user_id=current_user.id,
        credential_id=bytes_to_base64url(verification.credential_id),
        public_key=bytes_to_base64url(verification.credential_public_key),
        sign_count=verification.sign_count,
        transports=json.dumps(transports) if transports else None,
        device_name=(body.device_name or "").strip()[:150] or None,
    )
    db.add(cred)
    await db.commit()
    await db.refresh(cred)
    _logger.info("User %s registered passkey '%s'", current_user.username, cred.device_name or "unnamed")
    return _credential_to_response(cred)


@router.post("/login/begin", response_model=WebAuthnOptionsResponse)
async def login_begin(request: Request, db: AsyncSession = Depends(get_db)):
    """Start a usernameless (discoverable credential) passkey login."""
    from backend.app.api.routes.auth import is_auth_enabled

    if not await is_auth_enabled(db):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Authentication is not enabled")

    rp_id, _origins = await _get_rp(db, request)
    options = generate_authentication_options(
        rp_id=rp_id,
        user_verification=UserVerificationRequirement.REQUIRED,
    )
    await _store_challenge(db, options.challenge, TokenType.WEBAUTHN_LOGIN, None)
    return WebAuthnOptionsResponse(options=json.loads(options_to_json(options)))


@router.post("/login/complete", response_model=LoginResponse)
async def login_complete(
    raw_request: Request,
    body: WebAuthnLoginCompleteRequest,
    db: AsyncSession = Depends(get_db),
):
    from backend.app.api.routes.auth import _get_client_ip, _user_to_response
    from backend.app.api.routes.mfa import check_rate_limit, record_failed_attempt

    client_ip = _get_client_ip(raw_request)
    await check_rate_limit(db, client_ip, event_type=EventType.LOGIN_IP, max_attempts=20)

    rp_id, origins = await _get_rp(db, raw_request)
    credential_json = json.dumps(body.credential)

    try:
        client_data = json.loads(
            base64url_to_bytes(body.credential["response"]["clientDataJSON"])
        )
        expected_challenge = client_data["challenge"]
        raw_id = body.credential["rawId"]
    except (KeyError, ValueError, TypeError):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Malformed credential")

    await _consume_challenge(db, expected_challenge, TokenType.WEBAUTHN_LOGIN)

    result = await db.execute(select(WebAuthnCredential).where(WebAuthnCredential.credential_id == raw_id))
    cred = result.scalar_one_or_none()
    if cred is None:
        await record_failed_attempt(db, client_ip, event_type=EventType.LOGIN_IP)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unknown passkey")

    try:
        verification = verify_authentication_response(
            credential=credential_json,
            expected_challenge=base64url_to_bytes(expected_challenge),
            expected_origin=origins if len(origins) > 1 else origins[0],
            expected_rp_id=rp_id,
            credential_public_key=base64url_to_bytes(cred.public_key),
            credential_current_sign_count=cred.sign_count,
            require_user_verification=True,
        )
    except InvalidAuthenticationResponse as e:
        _logger.warning("Passkey login failed (credential %s): %s", cred.id, e)
        await record_failed_attempt(db, client_ip, event_type=EventType.LOGIN_IP)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Passkey verification failed")

    cred.sign_count = verification.new_sign_count
    cred.last_used_at = datetime.now(timezone.utc)

    result = await db.execute(
        select(User).where(User.id == cred.user_id).options(selectinload(User.groups))
    )
    user = result.scalar_one_or_none()
    if user is None or not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User is not active")

    await db.commit()

    access_token_expires = timedelta(minutes=await resolve_session_max_minutes(db))
    access_token = create_access_token(data={"sub": user.username}, expires_delta=access_token_expires)
    _logger.info("User %s logged in with passkey '%s'", user.username, cred.device_name or "unnamed")

    return LoginResponse(
        access_token=access_token,
        token_type="bearer",
        user=_user_to_response(user),
    )
