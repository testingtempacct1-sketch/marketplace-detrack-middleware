from sqlalchemy.orm import Session

from app.models import OrderSync
from app.schemas import StandardOrder
from app.mapper import map_standard_order_to_detrack


def create_or_get_order_sync(db: Session, order: StandardOrder) -> dict:
    existing = (
        db.query(OrderSync)
        .filter(
            OrderSync.source == order.source,
            OrderSync.source_order_id == order.source_order_id,
        )
        .first()
    )

    if existing:
        return {
            "created": False,
            "message": "Order already exists. Skipping duplicate.",
            "order_sync_id": existing.id,
            "detrack_payload": map_standard_order_to_detrack(order),
        }

    order_sync = OrderSync(
        source=order.source,
        source_order_id=order.source_order_id,
        customer_name=order.customer_name,
        phone=order.phone,
        address=order.address,
        postal_code=order.postal_code,
        detrack_do_number=f"{order.source.upper()}-{order.source_order_id}",
        sync_status="pending",
    )

    db.add(order_sync)
    db.commit()
    db.refresh(order_sync)

    return {
        "created": True,
        "message": "Order saved successfully.",
        "order_sync_id": order_sync.id,
        "detrack_payload": map_standard_order_to_detrack(order),
    }
