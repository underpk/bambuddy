"""Shopee orders — REST routes.

Read access needs a logged-in user; settings/mappings/sync need
``settings:update`` (the module drives the print queue, so configuring it
is an admin concern). The IMAP password is stored with ``mfa_encrypt`` and
never returned to clients.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import delete, desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.api.routes.settings import get_setting, set_setting
from backend.app.core.auth import Permission, RequirePermissionIfAuthEnabled, get_current_active_user
from backend.app.core.database import get_db
from backend.app.core.encryption import is_encryption_active, mfa_decrypt, mfa_encrypt
from backend.app.models.library import LibraryFile
from backend.app.models.shopee import ShopeeOrder, ShopeeProductMapping
from backend.app.models.user import User
from backend.app.schemas.shopee import (
    ShopeeMappingCreate,
    ShopeeMappingResponse,
    ShopeeOrderResponse,
    ShopeeOrderUpdate,
    ShopeeSettings,
    ShopeeSettingsResponse,
    ShopeeSyncResult,
)
from backend.app.services.shopee_service import fetch_shopee_emails, sync_orders

_logger = logging.getLogger(__name__)

router = APIRouter(prefix="/shopee", tags=["shopee"])

_SETTINGS_KEYS = {
    "enabled": "shopee_poll_enabled",
    "imap_host": "shopee_imap_host",
    "imap_user": "shopee_imap_user",
    "imap_password": "shopee_imap_password",
    "poll_interval_minutes": "shopee_poll_interval_minutes",
    "since_days": "shopee_since_days",
}


async def load_shopee_settings(db: AsyncSession) -> ShopeeSettings:
    """Read module settings; the password comes back decrypted for IMAP use."""
    s = ShopeeSettings()
    s.enabled = (await get_setting(db, _SETTINGS_KEYS["enabled"])) == "true"
    s.imap_host = (await get_setting(db, _SETTINGS_KEYS["imap_host"])) or "imap.gmail.com"
    s.imap_user = (await get_setting(db, _SETTINGS_KEYS["imap_user"])) or ""
    stored_pw = await get_setting(db, _SETTINGS_KEYS["imap_password"])
    if stored_pw:
        try:
            s.imap_password = mfa_decrypt(stored_pw) if is_encryption_active() else stored_pw
        except Exception:
            s.imap_password = stored_pw
    try:
        s.poll_interval_minutes = int(await get_setting(db, _SETTINGS_KEYS["poll_interval_minutes"]) or "5")
        s.since_days = int(await get_setting(db, _SETTINGS_KEYS["since_days"]) or "7")
    except ValueError:
        pass
    return s


@router.get("/settings", response_model=ShopeeSettingsResponse)
async def get_shopee_settings(
    db: AsyncSession = Depends(get_db),
    current_user: User | None = RequirePermissionIfAuthEnabled(Permission.SETTINGS_READ),
):
    s = await load_shopee_settings(db)
    return ShopeeSettingsResponse(
        enabled=s.enabled,
        imap_host=s.imap_host,
        imap_user=s.imap_user,
        password_set=bool(s.imap_password),
        poll_interval_minutes=s.poll_interval_minutes,
        since_days=s.since_days,
    )


@router.put("/settings", response_model=ShopeeSettingsResponse)
async def save_shopee_settings(
    body: ShopeeSettings,
    db: AsyncSession = Depends(get_db),
    current_user: User | None = RequirePermissionIfAuthEnabled(Permission.SETTINGS_UPDATE),
):
    await set_setting(db, _SETTINGS_KEYS["enabled"], "true" if body.enabled else "false")
    await set_setting(db, _SETTINGS_KEYS["imap_host"], body.imap_host.strip())
    await set_setting(db, _SETTINGS_KEYS["imap_user"], body.imap_user.strip())
    if body.imap_password:  # empty = keep the stored one
        value = mfa_encrypt(body.imap_password) if is_encryption_active() else body.imap_password
        await set_setting(db, _SETTINGS_KEYS["imap_password"], value)
    await set_setting(db, _SETTINGS_KEYS["poll_interval_minutes"], str(body.poll_interval_minutes))
    await set_setting(db, _SETTINGS_KEYS["since_days"], str(body.since_days))
    return await get_shopee_settings(db=db, current_user=current_user)


@router.post("/sync", response_model=ShopeeSyncResult)
async def sync_now(
    db: AsyncSession = Depends(get_db),
    current_user: User | None = RequirePermissionIfAuthEnabled(Permission.SETTINGS_UPDATE),
):
    """Poll the inbox immediately (also used as the settings 'test' button)."""
    s = await load_shopee_settings(db)
    if not s.imap_user or not s.imap_password:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="IMAP username and app password must be configured first",
        )
    try:
        parsed = await asyncio.to_thread(
            fetch_shopee_emails, s.imap_host, s.imap_user, s.imap_password, s.since_days
        )
    except Exception as e:
        _logger.warning("Shopee manual sync failed: %s", e)
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"IMAP fetch failed: {e}")

    counters = await sync_orders(db, parsed)
    return ShopeeSyncResult(**counters, emails_scanned=len(parsed))


@router.get("/orders", response_model=list[ShopeeOrderResponse])
async def list_orders(
    status_filter: str | None = None,
    limit: int = 200,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    query = select(ShopeeOrder).order_by(desc(ShopeeOrder.created_at)).limit(min(limit, 1000))
    if status_filter:
        query = query.where(ShopeeOrder.status == status_filter)
    result = await db.execute(query)
    return result.scalars().all()


@router.patch("/orders/{order_id}", response_model=ShopeeOrderResponse)
async def update_order(
    order_id: int,
    body: ShopeeOrderUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    order = (await db.execute(select(ShopeeOrder).where(ShopeeOrder.id == order_id))).scalar_one_or_none()
    if order is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found")
    if body.status is not None:
        order.status = body.status
    if body.notes is not None:
        order.notes = body.notes
    await db.commit()
    await db.refresh(order)
    return order


@router.get("/mappings", response_model=list[ShopeeMappingResponse])
async def list_mappings(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    result = await db.execute(
        select(ShopeeProductMapping, LibraryFile.filename)
        .outerjoin(LibraryFile, LibraryFile.id == ShopeeProductMapping.library_file_id)
        .order_by(ShopeeProductMapping.id)
    )
    out = []
    for mapping, file_name in result.all():
        resp = ShopeeMappingResponse.model_validate(mapping)
        resp.library_file_name = file_name
        out.append(resp)
    return out


@router.post("/mappings", response_model=ShopeeMappingResponse)
async def create_mapping(
    body: ShopeeMappingCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User | None = RequirePermissionIfAuthEnabled(Permission.SETTINGS_UPDATE),
):
    lib_file = (
        await db.execute(select(LibraryFile).where(LibraryFile.id == body.library_file_id))
    ).scalar_one_or_none()
    if lib_file is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Library file not found")

    mapping = ShopeeProductMapping(**body.model_dump())
    db.add(mapping)
    await db.commit()
    await db.refresh(mapping)
    resp = ShopeeMappingResponse.model_validate(mapping)
    resp.library_file_name = lib_file.filename
    return resp


@router.delete("/mappings/{mapping_id}")
async def delete_mapping(
    mapping_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User | None = RequirePermissionIfAuthEnabled(Permission.SETTINGS_UPDATE),
):
    result = await db.execute(delete(ShopeeProductMapping).where(ShopeeProductMapping.id == mapping_id))
    await db.commit()
    if result.rowcount == 0:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Mapping not found")
    return {"message": "Mapping deleted"}
