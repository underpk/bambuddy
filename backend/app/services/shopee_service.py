"""Shopee order ingestion from seller notification emails.

Polls a Gmail inbox over IMAP for Shopee Thailand seller notifications and
turns them into ShopeeOrder rows:

- "ถึงเวลาจัดส่งสินค้าหมายเลข #X แล้ว!"  → paid order, needs shipping (status: new)
- "...ได้ส่งคำร้องขอยกเลิกคำสั่งซื้อหมายเลข #X" → cancellation request

Matched product mappings auto-create print-queue entries (manual start) so
production is queued the moment an order arrives.

All IMAP work is synchronous (imaplib) and runs in a thread via
``asyncio.to_thread`` from the async callers.
"""

from __future__ import annotations

import email
import email.header
import imaplib
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.library import LibraryFile
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.shopee import ShopeeOrder, ShopeeOrderItem, ShopeeProductMapping

_logger = logging.getLogger(__name__)

SHOPEE_SENDER = "info@mail.shopee.co.th"
PROCESSED_LABEL = "Bambuddy/Shopee"

# Thai month abbreviations → month number (Shopee uses CE years)
_TH_MONTHS = {
    "ม.ค.": 1, "ก.พ.": 2, "มี.ค.": 3, "เม.ย.": 4, "พ.ค.": 5, "มิ.ย.": 6,
    "ก.ค.": 7, "ส.ค.": 8, "ก.ย.": 9, "ต.ค.": 10, "พ.ย.": 11, "ธ.ค.": 12,
}

_RE_ORDER_SN_SHIP = re.compile(r"ถึงเวลาจัดส่งสินค้าหมายเลข\s*#([A-Z0-9]+)")
_RE_ORDER_SN_CANCEL = re.compile(r"ยกเลิกคำสั่งซื้อหมายเลข\s*#([A-Z0-9]+)")
_RE_BUYER = re.compile(r"จัดส่งสินค้าไปยังผู้ซื้อ\s*(\S+)")
_RE_DELIVER_BY = re.compile(r"ควรได้รับสินค้าภายในวันที่\s*(\d{1,2}\s*\S+\s*\d{4})")
_RE_ORDER_DATE = re.compile(r"วันที่สั่งซื้อ:\s*(\d{1,2}\s*\S+\s*\d{4}\s*[\d:]+)")
_RE_SELLER_URL = re.compile(r"(https?://seller\.shopee\.co\.th[^\s\"'<>]*?/order/\d+)")
_RE_SELLER_URL_ESCAPED = re.compile(r"redir=(https?%3A%2F%2Fseller\.shopee\.co\.th[^&\"'<>]+)")
_RE_MONEY = re.compile(r"฿\s*([\d,]+(?:\.\d+)?)")
# One item block: "1. <name> ตัวเลือกสินค้า: <var> จำนวน: <qty> ราคา: ฿<price>"
_RE_ITEM = re.compile(
    r"\d+\.\s*(?P<name>.+?)\s*"
    r"(?:ตัวเลือกสินค้า:\s*(?P<variation>.+?)\s*)?"
    r"จำนวน:\s*(?P<qty>\d+)\s*"
    r"ราคา:\s*฿\s*(?P<price>[\d,]+(?:\.\d+)?)",
    re.DOTALL,
)


@dataclass
class ParsedOrderItem:
    name: str
    variation: str | None
    quantity: int
    price: float | None


@dataclass
class ParsedOrder:
    order_sn: str
    kind: str  # 'ship' | 'cancel'
    buyer_username: str | None = None
    order_date: datetime | None = None
    deliver_by_raw: str | None = None
    deliver_by: datetime | None = None
    subtotal: float | None = None
    shipping_fee: float | None = None
    total: float | None = None
    seller_center_url: str | None = None
    email_message_id: str | None = None
    items: list[ParsedOrderItem] = field(default_factory=list)


def _parse_thai_date(text: str) -> datetime | None:
    """Parse '09 ก.ค. 2026' or '09 ก.ค. 2026 00:39:45' (CE year, Bangkok time)."""
    m = re.match(r"(\d{1,2})\s*(\S+?)\s*(\d{4})(?:\s+(\d{1,2}):(\d{2})(?::(\d{2}))?)?", text.strip())
    if not m:
        return None
    day, month_th, year = int(m.group(1)), m.group(2), int(m.group(3))
    month = _TH_MONTHS.get(month_th)
    if not month:
        return None
    if year > 2400:  # Buddhist Era safety net
        year -= 543
    hour = int(m.group(4) or 0)
    minute = int(m.group(5) or 0)
    second = int(m.group(6) or 0)
    tz_bkk = timezone(timedelta(hours=7))
    try:
        return datetime(year, month, day, hour, minute, second, tzinfo=tz_bkk)
    except ValueError:
        return None


def _html_to_text(html: str) -> str:
    """Strip tags and collapse whitespace — Shopee emails are table soup."""
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = (
        text.replace("&nbsp;", " ")
        .replace("&amp;", "&")
        .replace("&#39;", "'")
        .replace("&quot;", '"')
        .replace("&lt;", "<")
        .replace("&gt;", ">")
    )
    return re.sub(r"\s+", " ", text)


def parse_shopee_email(subject: str, html: str, message_id: str | None) -> ParsedOrder | None:
    """Parse one Shopee notification email; None when it's not order-related."""
    ship = _RE_ORDER_SN_SHIP.search(subject)
    cancel = _RE_ORDER_SN_CANCEL.search(subject)
    if not ship and not cancel:
        return None

    if cancel:
        return ParsedOrder(order_sn=cancel.group(1), kind="cancel", email_message_id=message_id)

    text = _html_to_text(html)
    order = ParsedOrder(order_sn=ship.group(1), kind="ship", email_message_id=message_id)

    if m := _RE_BUYER.search(text):
        order.buyer_username = m.group(1)
    if m := _RE_DELIVER_BY.search(text):
        order.deliver_by_raw = m.group(1).strip()
        order.deliver_by = _parse_thai_date(order.deliver_by_raw)
    if m := _RE_ORDER_DATE.search(text):
        order.order_date = _parse_thai_date(m.group(1))

    if m := _RE_SELLER_URL.search(html):
        order.seller_center_url = m.group(1)
    elif m := _RE_SELLER_URL_ESCAPED.search(html):
        from urllib.parse import unquote

        order.seller_center_url = unquote(m.group(1)).split("?")[0]

    # Totals — labels appear after the item list
    if m := re.search(r"ยอดรวมค่าสินค้า:\s*฿\s*([\d,]+(?:\.\d+)?)", text):
        order.subtotal = float(m.group(1).replace(",", ""))
    if m := re.search(r"ค่าจัดส่งสินค้า:\s*฿\s*([\d,]+(?:\.\d+)?)", text):
        order.shipping_fee = float(m.group(1).replace(",", ""))
    if m := re.search(r"ยอดที่ต้องชำระทั้งหมด:\s*฿\s*([\d,]+(?:\.\d+)?)", text):
        order.total = float(m.group(1).replace(",", ""))

    # Item blocks live between "รายละเอียดคำสั่งซื้อ" and "ยอดรวมค่าสินค้า"
    start = text.find("รายละเอียดคำสั่งซื้อ")
    end = text.find("ยอดรวมค่าสินค้า")
    items_text = text[start:end] if start >= 0 and end > start else text
    for m in _RE_ITEM.finditer(items_text):
        name = m.group("name").strip()
        # The numbered-item regex can swallow leading boilerplate on odd
        # templates; cap the name at something sane.
        if len(name) > 300:
            name = name[-300:]
        order.items.append(
            ParsedOrderItem(
                name=name,
                variation=(m.group("variation") or "").strip() or None,
                quantity=int(m.group("qty")),
                price=float(m.group("price").replace(",", "")),
            )
        )
    return order


# ─── IMAP fetch (sync — call via asyncio.to_thread) ──────────────────────────


def fetch_shopee_emails(
    host: str,
    username: str,
    password: str,
    since_days: int = 7,
    label_processed: bool = True,
) -> list[ParsedOrder]:
    """Fetch and parse Shopee notification emails; returns parsed orders.

    Uses the ``Seen``-independent approach: searches the last ``since_days``
    days and lets the caller dedupe by order_sn/message-id, so a lost DB or
    re-poll never misses orders. Processed mails get a Gmail label (visible
    in the Gmail UI as Bambuddy/Shopee) when ``label_processed`` is set.
    """
    parsed: list[ParsedOrder] = []
    imap = imaplib.IMAP4_SSL(host, 993)
    try:
        imap.login(username, password)
        imap.select("INBOX")
        since = (datetime.now(timezone.utc) - timedelta(days=since_days)).strftime("%d-%b-%Y")
        status, data = imap.search(None, f'(FROM "{SHOPEE_SENDER}" SINCE {since})')
        if status != "OK":
            raise RuntimeError(f"IMAP search failed: {status}")
        msg_nums = data[0].split()
        _logger.info("Shopee poll: %d candidate emails since %s", len(msg_nums), since)

        for num in msg_nums:
            status, msg_data = imap.fetch(num, "(RFC822)")
            if status != "OK" or not msg_data or msg_data[0] is None:
                continue
            msg = email.message_from_bytes(msg_data[0][1])

            raw_subject = msg.get("Subject", "")
            subject = ""
            for part, enc in email.header.decode_header(raw_subject):
                subject += part.decode(enc or "utf-8", errors="replace") if isinstance(part, bytes) else part

            html = ""
            for part in msg.walk():
                if part.get_content_type() == "text/html":
                    payload = part.get_payload(decode=True)
                    if payload:
                        charset = part.get_content_charset() or "utf-8"
                        html += payload.decode(charset, errors="replace")

            order = parse_shopee_email(subject, html, msg.get("Message-ID"))
            if order:
                parsed.append(order)
                if label_processed:
                    try:
                        imap.store(num, "+X-GM-LABELS", f'"{PROCESSED_LABEL}"')
                    except Exception:  # label failure must never break ingestion
                        pass
    finally:
        try:
            imap.logout()
        except Exception:
            pass
    return parsed


# ─── DB sync + auto-queue ─────────────────────────────────────────────────────


async def _auto_queue_item(
    db: AsyncSession, order: ShopeeOrder, item: ShopeeOrderItem, mappings: list[ShopeeProductMapping]
) -> int:
    """Create print-queue entries for a matched order item; returns count queued."""
    name_lower = item.name.lower()
    variation_lower = (item.variation or "").lower()
    mapping = next(
        (
            m
            for m in mappings
            if m.match_name.lower() in name_lower
            and (not m.match_variation or m.match_variation.lower() in variation_lower)
        ),
        None,
    )
    if mapping is None:
        return 0

    item.mapping_id = mapping.id
    if not mapping.auto_queue:
        return 0

    lib_file = (
        await db.execute(select(LibraryFile).where(LibraryFile.id == mapping.library_file_id))
    ).scalar_one_or_none()
    if lib_file is None:
        _logger.warning("Shopee mapping %d points to missing library file %d", mapping.id, mapping.library_file_id)
        return 0

    max_pos = (await db.execute(select(func.max(PrintQueueItem.position)))).scalar() or 0
    count = item.quantity * max(1, mapping.copies_per_unit)
    for i in range(count):
        db.add(
            PrintQueueItem(
                library_file_id=mapping.library_file_id,
                position=max_pos + 1 + i,
                # Queued jobs wait for a person to press start — a printer with
                # yesterday's print still on the plate must not fire on its own.
                manual_start=True,
            )
        )
    _logger.info(
        "Shopee order %s: queued %d × library file %d (%s)",
        order.order_sn,
        count,
        mapping.library_file_id,
        item.name[:60],
    )
    return count


async def sync_orders(db: AsyncSession, parsed_orders: list[ParsedOrder]) -> dict:
    """Upsert parsed orders; auto-queue newly seen items. Returns counters."""
    new_orders = 0
    cancellations = 0
    queued = 0

    mappings = list((await db.execute(select(ShopeeProductMapping))).scalars().all())

    for parsed in parsed_orders:
        existing = (
            await db.execute(select(ShopeeOrder).where(ShopeeOrder.order_sn == parsed.order_sn))
        ).scalar_one_or_none()

        if parsed.kind == "cancel":
            if existing and existing.status not in ("done", "shipped"):
                existing.status = "cancel_requested"
                cancellations += 1
            elif not existing:
                db.add(
                    ShopeeOrder(
                        order_sn=parsed.order_sn,
                        status="cancel_requested",
                        email_message_id=parsed.email_message_id,
                    )
                )
                cancellations += 1
            continue

        if existing:
            continue  # already ingested

        order = ShopeeOrder(
            order_sn=parsed.order_sn,
            buyer_username=parsed.buyer_username,
            order_date=parsed.order_date,
            deliver_by_raw=parsed.deliver_by_raw,
            deliver_by=parsed.deliver_by,
            subtotal=parsed.subtotal,
            shipping_fee=parsed.shipping_fee,
            total=parsed.total,
            seller_center_url=parsed.seller_center_url,
            email_message_id=parsed.email_message_id,
            status="new",
        )
        db.add(order)
        await db.flush()

        order_queued = 0
        for p_item in parsed.items:
            item = ShopeeOrderItem(
                order_id=order.id,
                name=p_item.name,
                variation=p_item.variation,
                quantity=p_item.quantity,
                price=p_item.price,
            )
            db.add(item)
            await db.flush()
            item.queued_count = await _auto_queue_item(db, order, item, mappings)
            order_queued += item.queued_count

        if order_queued:
            order.status = "queued"
            queued += order_queued
        new_orders += 1

    await db.commit()
    return {"new_orders": new_orders, "cancellations": cancellations, "print_jobs_queued": queued}
