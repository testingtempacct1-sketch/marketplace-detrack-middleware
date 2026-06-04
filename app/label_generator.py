"""
label_generator.py
Generates a PDF shipping label for the Brother QL-1110NWB printer
using DK-22246 continuous roll (103mm wide).
Label size: 103mm x auto-height (expands based on content)
"""
import io
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import qrcode
import qrcode.image.pil
from barcode import Code128
from barcode.writer import ImageWriter
from reportlab.lib import colors
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

logger = logging.getLogger(__name__)

SGT = ZoneInfo("Asia/Singapore")

# Register CJK font for Chinese character support
_CJK_FONT_REGISTERED = False
CJK_FONT = "WQYMicroHei"
CJK_FONT_PATH = "/usr/share/fonts/truetype/wqy-microhei.ttf"


def _ensure_cjk_font():
    global _CJK_FONT_REGISTERED
    if _CJK_FONT_REGISTERED:
        return
    try:
        pdfmetrics.registerFont(TTFont(CJK_FONT, CJK_FONT_PATH))
        _CJK_FONT_REGISTERED = True
        logger.info("[LabelGenerator] CJK font registered successfully.")
    except Exception as e:
        logger.warning(f"[LabelGenerator] Could not register CJK font: {e}")


def _cjk_font(size: int, bold: bool = False) -> tuple[str, int]:
    """Return font name and size, using CJK font if available."""
    if _CJK_FONT_REGISTERED:
        return CJK_FONT, size
    return ("Helvetica-Bold" if bold else "Helvetica"), size

# Label dimensions for DK-22246 (103mm wide continuous roll)
# Printing at 100mm x 150mm for better fit
LABEL_WIDTH_MM = 100
LABEL_WIDTH = LABEL_WIDTH_MM * mm

# Colors
COLOR_DARK = colors.HexColor("#2C2C2A")
COLOR_GREEN = colors.HexColor("#1D9E75")
COLOR_LIGHT = colors.HexColor("#F1EFE8")
COLOR_WHITE = colors.white
COLOR_GRAY = colors.HexColor("#888780")

# Source badge colors
SOURCE_COLORS = {
    "shopify": colors.HexColor("#1D9E75"),
    "tiktok_shop": colors.HexColor("#AA88FF"),
    "shopee": colors.HexColor("#FF6B35"),
}

PADDING = 6 * mm


def _get_source_label(source: str) -> str:
    s = (source or "").lower()
    if "tiktok" in s:
        return "TIKTOK"
    if "shopee" in s:
        return "SHOPEE"
    return "SHOPIFY"


def _get_source_color(source: str) -> colors.Color:
    s = (source or "").lower()
    if "tiktok" in s:
        return SOURCE_COLORS["tiktok_shop"]
    if "shopee" in s:
        return SOURCE_COLORS["shopee"]
    return SOURCE_COLORS["shopify"]


def _generate_barcode_image(do_number: str) -> io.BytesIO:
    """Generate a Code128 barcode image as PNG bytes."""
    buffer = io.BytesIO()
    writer = ImageWriter()
    writer.set_options({
        "module_width": 0.8,
        "module_height": 8,
        "font_size": 6,
        "text_distance": 2,
        "quiet_zone": 2,
        "dpi": 300,
    })
    code = Code128(do_number, writer=writer)
    code.write(buffer, options={
        "module_width": 0.8,
        "module_height": 8,
        "font_size": 6,
        "text_distance": 2,
        "quiet_zone": 2,
        "dpi": 300,
    })
    buffer.seek(0)
    return buffer


def _generate_qr_image(do_number: str) -> io.BytesIO:
    """Generate a QR code image as PNG bytes."""
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=4,
        border=2,
    )
    qr.add_data(do_number)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buffer = io.BytesIO()
    img.save(buffer, format="PNG")
    buffer.seek(0)
    return buffer


def _wrap_text(text: str, max_chars: int) -> list[str]:
    """Wrap text to fit within max_chars per line."""
    if not text:
        return []
    words = text.split()
    lines = []
    current_line = ""
    for word in words:
        if len(current_line) + len(word) + 1 <= max_chars:
            current_line = f"{current_line} {word}".strip()
        else:
            if current_line:
                lines.append(current_line)
            current_line = word
    if current_line:
        lines.append(current_line)
    return lines


def generate_label_pdf(
    do_number: str,
    source: str,
    customer_name: str,
    phone: str,
    address: str,
    postal_code: str | None,
    items: list[dict],
    remarks: str | None,
    delivery_date: str | None,
) -> bytes:
    """
    Generate a shipping label PDF for the given order.
    Returns PDF as bytes.
    """
    buffer = io.BytesIO()

    # Ensure CJK font is registered
    _ensure_cjk_font()
    # Start with fixed sections and add dynamic content
    address_lines = _wrap_text(address or "", 38)
    item_lines = []
    for item in (items or []):
        name = item.get("description") or item.get("name") or "Item"
        qty = item.get("quantity") or 1
        item_lines.append(f"{name}  x{qty}")

    remarks_lines = _wrap_text(remarks or "", 50) if remarks else []

    # Fixed label height of 150mm
    label_height = 150 * mm

    c = canvas.Canvas(buffer, pagesize=(LABEL_WIDTH, label_height))
    y = label_height  # Start from top

    # ── HEADER ────────────────────────────────────────────────────────
    header_height = 20 * mm
    c.setFillColor(COLOR_DARK)
    c.rect(0, y - header_height, LABEL_WIDTH, header_height, fill=1, stroke=0)

    # Store name
    c.setFillColor(COLOR_WHITE)
    c.setFont("Helvetica-Bold", 14)
    c.drawString(PADDING, y - 11 * mm, "ZEN ZU FU")

    c.setFillColor(colors.HexColor("#B4B2A9"))
    c.setFont("Helvetica", 7)
    c.drawString(PADDING, y - 16 * mm, "zenzufudurians.com.sg")

    # Source badge
    source_label = _get_source_label(source)
    source_color = _get_source_color(source)
    badge_w = 22 * mm
    badge_h = 7 * mm
    badge_x = LABEL_WIDTH - PADDING - badge_w
    badge_y = y - 14 * mm
    c.setFillColor(source_color)
    c.roundRect(badge_x, badge_y, badge_w, badge_h, 1.5 * mm, fill=1, stroke=0)
    c.setFillColor(COLOR_WHITE)
    c.setFont("Helvetica-Bold", 7)
    c.drawCentredString(badge_x + badge_w / 2, badge_y + 2 * mm, source_label)

    y -= header_height

    # ── DO NUMBER ─────────────────────────────────────────────────────
    do_bg_h = 14 * mm
    c.setFillColor(COLOR_LIGHT)
    c.rect(PADDING / 2, y - do_bg_h - 1 * mm, LABEL_WIDTH - PADDING, do_bg_h, fill=1, stroke=0)

    c.setFillColor(COLOR_GRAY)
    c.setFont("Helvetica", 6)
    c.drawString(PADDING, y - 5 * mm, "DELIVERY ORDER")

    c.setFillColor(COLOR_DARK)
    c.setFont("Helvetica-Bold", 16)
    c.drawString(PADDING, y - 13 * mm, do_number)

    y -= do_bg_h + 2 * mm

    # ── BARCODE ───────────────────────────────────────────────────────
    try:
        barcode_label = "SCAN TO ASSIGN DELIVERY"
        c.setFillColor(COLOR_GRAY)
        c.setFont("Helvetica", 6)
        c.drawCentredString(LABEL_WIDTH / 2, y - 4 * mm, barcode_label)

        barcode_buf = _generate_barcode_image(do_number)
        barcode_w = LABEL_WIDTH - PADDING * 2
        barcode_h_draw = 14 * mm
        c.drawImage(
            ImageReader(barcode_buf),
            PADDING, y - 5 * mm - barcode_h_draw,
            width=barcode_w,
            height=barcode_h_draw,
            preserveAspectRatio=False,
        )

        c.setFillColor(COLOR_GRAY)
        c.setFont("Helvetica", 6)
        c.drawCentredString(LABEL_WIDTH / 2, y - 5 * mm - barcode_h_draw - 3 * mm, do_number)

        y -= barcode_h

    except Exception as e:
        logger.warning(f"[LabelGenerator] Barcode generation failed: {e}")
        y -= 5 * mm

    # ── QR CODE + CUSTOMER INFO ───────────────────────────────────────
    # Divider
    c.setStrokeColor(COLOR_LIGHT)
    c.setLineWidth(0.3)
    c.line(PADDING, y - 1 * mm, LABEL_WIDTH - PADDING, y - 1 * mm)
    y -= 3 * mm

    qr_size = 22 * mm
    try:
        qr_buf = _generate_qr_image(do_number)
        c.drawImage(
            ImageReader(qr_buf),
            PADDING, y - qr_size,
            width=qr_size,
            height=qr_size,
        )
    except Exception as e:
        logger.warning(f"[LabelGenerator] QR code generation failed: {e}")

    # Customer info next to QR code
    info_x = PADDING + qr_size + 3 * mm
    info_y = y - 4 * mm

    c.setFillColor(COLOR_GRAY)
    c.setFont("Helvetica", 6)
    c.drawString(info_x, info_y, "DELIVER TO")

    c.setFillColor(COLOR_DARK)
    c.setFont("Helvetica-Bold", 9)
    c.drawString(info_x, info_y - 5 * mm, customer_name or "")

    c.setFont("Helvetica", 8)
    c.drawString(info_x, info_y - 10 * mm, phone or "")

    # Address lines
    addr_y = info_y - 15 * mm
    c.setFont("Helvetica", 7)
    for line in address_lines[:3]:
        c.setFillColor(colors.HexColor("#444441"))
        c.drawString(info_x, addr_y, line)
        addr_y -= 4 * mm

    if postal_code:
        c.drawString(info_x, addr_y, f"Singapore {postal_code}")
        addr_y -= 4 * mm

    y -= max(qr_size + 2 * mm, (3 + len(address_lines)) * 4 * mm + 18 * mm)

    # ── ITEMS ─────────────────────────────────────────────────────────
    c.setStrokeColor(COLOR_LIGHT)
    c.line(PADDING, y, LABEL_WIDTH - PADDING, y)
    y -= 5 * mm

    c.setFillColor(COLOR_GRAY)
    c.setFont("Helvetica", 6)
    c.drawString(PADDING, y, "ITEMS")
    y -= 5 * mm

    font_name, font_size = _cjk_font(8)
    c.setFont(font_name, font_size)
    for item_line in item_lines:
        c.setFillColor(colors.HexColor("#444441"))
        c.drawString(PADDING, y, f"• {item_line}")
        y -= 5 * mm

    # ── REMARKS ───────────────────────────────────────────────────────
    if remarks_lines:
        c.setStrokeColor(COLOR_LIGHT)
        c.line(PADDING, y, LABEL_WIDTH - PADDING, y)
        y -= 5 * mm

        c.setFillColor(COLOR_GRAY)
        c.setFont("Helvetica", 6)
        c.drawString(PADDING, y, "REMARKS")
        y -= 5 * mm

        font_name, font_size = _cjk_font(7)
        c.setFont(font_name, font_size)
        for rline in remarks_lines:
            c.setFillColor(colors.HexColor("#444441"))
            c.drawString(PADDING, y, rline)
            y -= 4 * mm

    # ── FOOTER ────────────────────────────────────────────────────────
    footer_y = 4 * mm
    c.setFillColor(COLOR_LIGHT)
    c.rect(0, 0, LABEL_WIDTH, footer_y + 4 * mm, fill=1, stroke=0)

    date_str = delivery_date or datetime.now(SGT).strftime("%Y-%m-%d")
    c.setFillColor(COLOR_GRAY)
    c.setFont("Helvetica", 7)
    c.drawString(PADDING, footer_y, f"Date: {date_str}")
    c.drawRightString(LABEL_WIDTH - PADDING, footer_y, do_number)

    c.save()
    buffer.seek(0)
    return buffer.getvalue()
