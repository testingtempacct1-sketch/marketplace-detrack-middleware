from datetime import date

from app.schemas import StandardOrder


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
            "do_number": f"{order.source.upper()}-{order.source_order_id}",
            "date": delivery_date,
            "deliver_to": order.customer_name,
            "phone_number": order.phone,
            "address": order.address,
            "postal_code": order.postal_code,
            "instructions": order.remarks or "",
            "items": item_lines,
        }
    }