from datetime import datetime
from zoneinfo import ZoneInfo

from app.schemas import StandardOrder

SGT = ZoneInfo("Asia/Singapore")


def _get_order_number(order: StandardOrder) -> str:
    if order.source_order_name:
        clean_order_name = str(order.source_order_name).replace("#", "").strip()

        if clean_order_name:
            return clean_order_name

    return str(order.source_order_id)


def _get_channel_code(order: StandardOrder) -> str:
    source = (order.source or "").lower()

    if "tiktok" in source:
        return "TT"

    return "SH"


def _build_detrack_do_number(order: StandardOrder) -> str:
    order_number = _get_order_number(order)
    channel_code = _get_channel_code(order)

    return f"ZF-{channel_code}-{order_number}"


def get_delivery_date_sgt() -> str:
    """Return today's date in Singapore Time (SGT, UTC+8) as YYYY-MM-DD."""
    return datetime.now(SGT).strftime("%Y-%m-%d")


def map_standard_order_to_detrack(order: StandardOrder) -> dict:
    # Use order's delivery_date if provided, otherwise default to today in SGT
    delivery_date = order.delivery_date or get_delivery_date_sgt()

    item_lines = [
        {
            "description": item.name,
            "quantity": item.quantity,
            "sku": item.sku,
        }
        for item in order.items
    ]

    return {
        "data": {
            "do_number": _build_detrack_do_number(order),
            "date": delivery_date,
            "deliver_to": order.customer_name,
            "phone_number": order.phone,
            "address": order.address,
            "postal_code": order.postal_code,
            "instructions": order.remarks or "",
            "items": item_lines,
        }
    }
