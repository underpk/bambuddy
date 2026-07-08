"""Shopee order tracking.

Orders are ingested from Shopee's seller notification emails (IMAP) —
the seller has no Open Platform API access, so the ship-reminder email
("ถึงเวลาจัดส่งสินค้า...") is the trigger that an order is paid and needs
production. Product mappings connect ordered items to library files so
matching orders can be queued for printing automatically.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.app.core.database import Base


class ShopeeOrder(Base):
    __tablename__ = "shopee_orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # Shopee order number, e.g. "2607096MHPE1YH" (no leading #)
    order_sn: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)

    buyer_username: Mapped[str | None] = mapped_column(String(150), nullable=True)
    order_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Deliver-by deadline as shown in the email (Thai format), plus parsed form
    deliver_by_raw: Mapped[str | None] = mapped_column(String(64), nullable=True)
    deliver_by: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    subtotal: Mapped[float | None] = mapped_column(Float, nullable=True)
    shipping_fee: Mapped[float | None] = mapped_column(Float, nullable=True)
    total: Mapped[float | None] = mapped_column(Float, nullable=True)

    # new | queued | manual | cancel_requested | shipped | done
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="new", index=True)

    # Deep link to the order in Seller Centre
    seller_center_url: Mapped[str | None] = mapped_column(Text, nullable=True)

    # RFC 822 Message-ID of the source email — dedupe across polls
    email_message_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)

    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    items: Mapped[list[ShopeeOrderItem]] = relationship(
        back_populates="order", cascade="all, delete-orphan", lazy="selectin"
    )


class ShopeeOrderItem(Base):
    __tablename__ = "shopee_order_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("shopee_orders.id", ondelete="CASCADE"), nullable=False, index=True
    )

    name: Mapped[str] = mapped_column(Text, nullable=False)
    variation: Mapped[str | None] = mapped_column(String(255), nullable=True)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    price: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Set when the auto-queue hook created print queue items for this line
    queued_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    mapping_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("shopee_product_mappings.id", ondelete="SET NULL"), nullable=True
    )

    order: Mapped[ShopeeOrder] = relationship(back_populates="items")


class ShopeeProductMapping(Base):
    """Connects a Shopee listing (by name/variation substring) to a library file."""

    __tablename__ = "shopee_product_mappings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # Case-insensitive substring matched against the item name; variation
    # match is optional (empty = any variation of that product)
    match_name: Mapped[str] = mapped_column(String(255), nullable=False)
    match_variation: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Print source: exactly one of library_file_id / archive_id is set —
    # the print queue accepts either kind
    library_file_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("library_files.id", ondelete="CASCADE"), nullable=True
    )
    archive_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("print_archives.id", ondelete="CASCADE"), nullable=True
    )

    # Print queue items to create per ordered unit (multi-part products > 1)
    copies_per_unit: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    # When false the mapping only annotates the order; nothing is queued
    auto_queue: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )
