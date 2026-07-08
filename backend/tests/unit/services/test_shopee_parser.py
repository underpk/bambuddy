"""Parser tests for Shopee TH notification emails.

Fixture HTML mirrors the real seller-notification template (table soup with
Thai labels) — trimmed to the fields the parser reads.
"""

from backend.app.services.shopee_service import _parse_thai_date, parse_shopee_email

SHIP_SUBJECT = "ถึงเวลาจัดส่งสินค้าหมายเลข #2607096MHPE1YH แล้ว!"

SHIP_HTML = """
<html><body>
<div>เรียน คุณ modcables101,</div>
<div>คำสั่งซื้อหมายเลข <a
href="https://shopee.co.th/universal-link?redir=https%3A%2F%2Fseller.shopee.co.th%2Funiversal-link%2Fportal%2Fsale%2Forder%2F237228227221489%3Futm_content%3DXX%26utm_medium%3Demail">
<font>#2607096MHPE1YH</font></a> ได้รับการยืนยันการชำระเงินเรียบร้อยแล้ว.
<font>กรุณาจัดส่งสินค้าไปยังผู้ซื้อ captain887 ลูกค้าควรได้รับสินค้าภายในวันที่14 ก.ค. 2026</font>.</div>
<td> รายละเอียดคำสั่งซื้อ </td>
<td> หมายเลขคำสั่งซื้อ: </td><td> #2607096MHPE1YH </td>
<td> วันที่สั่งซื้อ: </td><td> 08 ก.ค. 2026 23:43:47 </td>
<td> 1.  Melodrip Tray สำหรับ Orea V3 / V4 / Aero press / Solo Dripper / UFO / April   </td>
<td> ตัวเลือกสินค้า: </td><td>  Universal,ดำ  </td>
<td> จำนวน: </td><td> 1 </td>
<td> ราคา: </td><td> ฿260 </td>
<td> ยอดรวมค่าสินค้า: </td><td> ฿260 </td>
<td> ค่าจัดส่งสินค้า: </td><td> ฿29 </td>
<td> ยอดที่ต้องชำระทั้งหมด: </td><td> ฿289 </td>
</body></html>
"""

MULTI_ITEM_HTML = SHIP_HTML.replace(
    "<td> ยอดรวมค่าสินค้า: </td>",
    """
<td> 2.  บริการพิมพ์ 3 มิติ ระบบ SLS วัสดุ Nylon PA12 | SLS 3D Printing Services </td>
<td> จำนวน: </td><td> 3 </td>
<td> ราคา: </td><td> ฿1,500 </td>
<td> ยอดรวมค่าสินค้า: </td>
""",
)

CANCEL_SUBJECT = "ผู้ซื้อsabun ได้ส่งคำร้องขอยกเลิกคำสั่งซื้อหมายเลข #2607085VVYP5U3"


def test_parse_ship_email():
    order = parse_shopee_email(SHIP_SUBJECT, SHIP_HTML, "<msg-1@shopee>")
    assert order is not None
    assert order.kind == "ship"
    assert order.order_sn == "2607096MHPE1YH"
    assert order.buyer_username == "captain887"
    assert order.deliver_by_raw == "14 ก.ค. 2026"
    assert order.deliver_by is not None and (order.deliver_by.year, order.deliver_by.month, order.deliver_by.day) == (
        2026,
        7,
        14,
    )
    assert order.order_date is not None and order.order_date.hour == 23
    assert order.subtotal == 260.0
    assert order.shipping_fee == 29.0
    assert order.total == 289.0
    assert order.seller_center_url is not None and "237228227221489" in order.seller_center_url
    assert order.email_message_id == "<msg-1@shopee>"

    assert len(order.items) == 1
    item = order.items[0]
    assert item.name.startswith("Melodrip Tray")
    assert item.variation == "Universal,ดำ"
    assert item.quantity == 1
    assert item.price == 260.0


def test_parse_multi_item_email():
    order = parse_shopee_email(SHIP_SUBJECT, MULTI_ITEM_HTML, None)
    assert order is not None
    assert len(order.items) == 2
    assert order.items[1].quantity == 3
    assert order.items[1].price == 1500.0
    assert order.items[1].variation is None
    assert "SLS" in order.items[1].name


def test_parse_cancel_email():
    order = parse_shopee_email(CANCEL_SUBJECT, "<html></html>", None)
    assert order is not None
    assert order.kind == "cancel"
    assert order.order_sn == "2607085VVYP5U3"


def test_non_order_email_ignored():
    assert parse_shopee_email("โปรโมชั่นพิเศษสำหรับคุณ!", "<html>ads</html>", None) is None


def test_thai_date_parsing():
    d = _parse_thai_date("09 ก.ค. 2026 00:39:45")
    assert d is not None and (d.year, d.month, d.day, d.hour, d.minute, d.second) == (2026, 7, 9, 0, 39, 45)
    # Buddhist Era year gets normalised
    d = _parse_thai_date("01 ม.ค. 2569")
    assert d is not None and d.year == 2026
    assert _parse_thai_date("garbage") is None
