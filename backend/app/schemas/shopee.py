"""Pydantic schemas for the Shopee orders module."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class ShopeeOrderItemResponse(BaseModel):
    id: int
    name: str
    variation: str | None
    quantity: int
    price: float | None
    queued_count: int
    mapping_id: int | None

    class Config:
        from_attributes = True


class ShopeeOrderResponse(BaseModel):
    id: int
    order_sn: str
    buyer_username: str | None
    order_date: datetime | None
    deliver_by_raw: str | None
    deliver_by: datetime | None
    subtotal: float | None
    shipping_fee: float | None
    total: float | None
    status: str
    seller_center_url: str | None
    notes: str | None
    created_at: datetime
    items: list[ShopeeOrderItemResponse]

    class Config:
        from_attributes = True


class ShopeeOrderUpdate(BaseModel):
    status: str | None = Field(default=None, pattern="^(new|queued|manual|cancel_requested|shipped|done)$")
    notes: str | None = None


class ShopeeMappingCreate(BaseModel):
    match_name: str = Field(min_length=1, max_length=255)
    match_variation: str | None = Field(default=None, max_length=255)
    library_file_id: int
    copies_per_unit: int = Field(default=1, ge=1, le=50)
    auto_queue: bool = True


class ShopeeMappingResponse(ShopeeMappingCreate):
    id: int
    created_at: datetime
    library_file_name: str | None = None

    class Config:
        from_attributes = True


class ShopeeSettings(BaseModel):
    enabled: bool = False
    imap_host: str = "imap.gmail.com"
    imap_user: str = ""
    # Write-only: never echoed back; empty string on update = keep existing
    imap_password: str = ""
    poll_interval_minutes: int = Field(default=5, ge=1, le=1440)
    since_days: int = Field(default=7, ge=1, le=60)


class ShopeeSettingsResponse(BaseModel):
    enabled: bool
    imap_host: str
    imap_user: str
    password_set: bool
    poll_interval_minutes: int
    since_days: int


class ShopeeSyncResult(BaseModel):
    new_orders: int
    cancellations: int
    print_jobs_queued: int
    emails_scanned: int
