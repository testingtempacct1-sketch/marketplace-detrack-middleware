from datetime import date

from app.schemas import StandardOrder

def _build_detrack_do_number(order) -> str:
    if order.source_order_name:
        clean_order_name = str(order.source_order_name).replace("#", "").strip()

        if clean_order_name:
            return f"ZF-{clean_order_name}"

    return f"ZF-{order.source_order_id}"


def map_standard_order_to_detrack(order: StandardOrder) -> dict:
    delivery_date = order.delivery_date or date.today().isoformat()

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