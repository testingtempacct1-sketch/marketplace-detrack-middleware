import json

from sqlalchemy.orm import Session

from app.detrack_client import DetrackAPIError, create_detrack_job
from app.mapper import map_standard_order_to_detrack
from app.models import OrderSync
from app.schemas import StandardOrder, StandardOrderItem


def _items_to_json(order: StandardOrder) -> str:
    return json.dumps(
        [item.model_dump() for item in order.items],
        ensure_ascii=False,
    )


def _items_from_json(items_json: str | None) -> list[StandardOrderItem]:
    if not items_json:
        return [
            StandardOrderItem(
                name="Order items from marketplace",
                quantity=1,
                sku=None,
            )
        ]

    try:
        raw_items = json.loads(items_json)
        return [StandardOrderItem(**item) for item in raw_items]
    except Exception:
        return [
            StandardOrderItem(
                name="Order items from marketplace",
                quantity=1,
                sku=None,
            )
        ]


def _order_sync_to_dict(order_sync: OrderSync) -> dict:
    return {
        "id": order_sync.id,
        "source": order_sync.source,
        "source_order_id": order_sync.source_order_id,
        "customer_name": order_sync.customer_name,
        "phone": order_sync.phone,
        "address": order_sync.address,
        "postal_code": order_sync.postal_code,
        "detrack_do_number": order_sync.detrack_do_number,
        "detrack_job_id": order_sync.detrack_job_id,
        "sync_status": order_sync.sync_status,
        "delivery_status": order_sync.delivery_status,
        "error_message": order_sync.error_message,
        "remarks": order_sync.remarks,
        "delivery_date": order_sync.delivery_date,
        "created_at": order_sync.created_at.isoformat() if order_sync.created_at else None,
        "updated_at": order_sync.updated_at.isoformat() if order_sync.updated_at else None,
    }


def list_recent_order_syncs(db: Session, limit: int = 20) -> list[dict]:
    safe_limit = max(1, min(limit, 100))

    records = (
        db.query(OrderSync)
        .order_by(OrderSync.id.desc())
        .limit(safe_limit)
        .all()
    )

    return [_order_sync_to_dict(record) for record in records]


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
            "sync_status": existing.sync_status,
            "detrack_payload": map_standard_order_to_detrack(order),
        }

    order_sync = OrderSync(
        source=order.source,
        source_order_id=order.source_order_id,
        customer_name=order.customer_name,
        phone=order.phone,
        address=order.address,
        postal_code=order.postal_code,
        items_json=_items_to_json(order),
        remarks=order.remarks,
        delivery_date=order.delivery_date,
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
        "sync_status": order_sync.sync_status,
        "detrack_payload": map_standard_order_to_detrack(order),
    }


def create_order_and_send_to_detrack(db: Session, order: StandardOrder) -> dict:
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
            "sent_to_detrack": False,
            "message": "Order already exists. Skipping duplicate.",
            "order_sync_id": existing.id,
            "sync_status": existing.sync_status,
            "detrack_do_number": existing.detrack_do_number,
        }

    detrack_payload = map_standard_order_to_detrack(order)

    order_sync = OrderSync(
        source=order.source,
        source_order_id=order.source_order_id,
        customer_name=order.customer_name,
        phone=order.phone,
        address=order.address,
        postal_code=order.postal_code,
        items_json=_items_to_json(order),
        remarks=order.remarks,
        delivery_date=order.delivery_date,
        detrack_do_number=detrack_payload["data"]["do_number"],
        sync_status="pending",
    )

    db.add(order_sync)
    db.commit()
    db.refresh(order_sync)

    try:
        detrack_response = create_detrack_job(detrack_payload)

        order_sync.sync_status = "sent_to_detrack"
        order_sync.delivery_status = "created"
        order_sync.error_message = None

        data = detrack_response.get("data") or detrack_response
        if isinstance(data, dict):
            order_sync.detrack_job_id = str(
                data.get("id")
                or data.get("job_id")
                or data.get("delivery_id")
                or ""
            )

        db.commit()
        db.refresh(order_sync)

        return {
            "created": True,
            "sent_to_detrack": True,
            "message": "Order saved and sent to Detrack successfully.",
            "order_sync_id": order_sync.id,
            "sync_status": order_sync.sync_status,
            "detrack_do_number": order_sync.detrack_do_number,
            "detrack_job_id": order_sync.detrack_job_id,
            "detrack_response": detrack_response,
        }

    except DetrackAPIError as exc:
        order_sync.sync_status = "detrack_failed"
        order_sync.error_message = str(exc)
        db.commit()

        return {
            "created": True,
            "sent_to_detrack": False,
            "message": "Order saved, but Detrack creation failed.",
            "order_sync_id": order_sync.id,
            "sync_status": order_sync.sync_status,
            "error": str(exc),
            "detrack_payload": detrack_payload,
        }


def retry_failed_detrack_sync(db: Session, order_sync_id: int) -> dict:
    order_sync = (
        db.query(OrderSync)
        .filter(OrderSync.id == order_sync_id)
        .first()
    )

    if not order_sync:
        return {
            "retried": False,
            "sent_to_detrack": False,
            "message": "Order sync record not found.",
        }

    if order_sync.sync_status == "sent_to_detrack":
        return {
            "retried": False,
            "sent_to_detrack": True,
            "message": "Order has already been sent to Detrack.",
            "order_sync_id": order_sync.id,
            "sync_status": order_sync.sync_status,
            "detrack_do_number": order_sync.detrack_do_number,
            "detrack_job_id": order_sync.detrack_job_id,
        }

    reconstructed_order = StandardOrder(
        source=order_sync.source,
        source_order_id=order_sync.source_order_id,
        customer_name=order_sync.customer_name or "Unknown Customer",
        phone=order_sync.phone or "",
        address=order_sync.address or "",
        postal_code=order_sync.postal_code,
        items=_items_from_json(order_sync.items_json),
        remarks=order_sync.remarks or "Retried from middleware failed sync.",
        delivery_date=order_sync.delivery_date,
    )

    detrack_payload = map_standard_order_to_detrack(reconstructed_order)

    try:
        detrack_response = create_detrack_job(detrack_payload)

        order_sync.sync_status = "sent_to_detrack"
        order_sync.delivery_status = "created"
        order_sync.error_message = None
        order_sync.detrack_do_number = detrack_payload["data"]["do_number"]

        data = detrack_response.get("data") or detrack_response
        if isinstance(data, dict):
            order_sync.detrack_job_id = str(
                data.get("id")
                or data.get("job_id")
                or data.get("delivery_id")
                or ""
            )

        db.commit()
        db.refresh(order_sync)

        return {
            "retried": True,
            "sent_to_detrack": True,
            "message": "Failed order was retried and sent to Detrack successfully.",
            "order_sync_id": order_sync.id,
            "sync_status": order_sync.sync_status,
            "detrack_do_number": order_sync.detrack_do_number,
            "detrack_job_id": order_sync.detrack_job_id,
            "detrack_response": detrack_response,
        }

    except DetrackAPIError as exc:
        order_sync.sync_status = "detrack_failed"
        order_sync.error_message = str(exc)
        db.commit()

        return {
            "retried": True,
            "sent_to_detrack": False,
            "message": "Retry failed. Detrack still could not create the job.",
            "order_sync_id": order_sync.id,
            "sync_status": order_sync.sync_status,
            "error": str(exc),
            "detrack_payload": detrack_payload,
        }
