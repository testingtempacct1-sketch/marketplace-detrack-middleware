import json
from datetime import datetime

from sqlalchemy.orm import Session

from app.detrack_client import (
    DetrackAPIError,
    create_detrack_job,
    delete_detrack_job,
    find_detrack_job_by_do_number,
    get_detrack_job,
    update_detrack_job_as_cancelled,
)
from app.mapper import map_standard_order_to_detrack
from app.models import OrderSync
from app.schemas import StandardOrder, StandardOrderItem
from app.shopify_admin_client import ShopifyAdminAPIError, create_shopify_fulfilment


# ---------------------------------------------------------------------------
# Detrack status logic (based on Detrack API v2 docs)
#
# status field API values:
#   info_recv     → job created, no driver assigned
#   in_transit    → goods in transit to warehouse, no driver assigned
#   dispatched    → ready for driver; if assign_to is null = still unassigned
#   dispatched    → if assign_to is not null = Out for delivery
#   completed     → delivered
#   failed        → failed delivery
#   on_hold       → admin placed on hold
#   return        → admin marked return
#
# DELETE the job if it has NOT yet been picked up by a driver:
#   - status is info_recv or in_transit (never assigned)
#   - status is dispatched AND assign_to is null (not yet picked up by driver)
#
# PUT ON HOLD if driver has already scanned and is out for delivery:
#   - status is dispatched AND assign_to is not null
#   - any other active status (failed, on_hold, return, etc.)
# ---------------------------------------------------------------------------

DETRACK_DELETABLE_API_STATUSES = {"info_recv", "in_transit"}


def _should_delete_detrack_job(job: dict | None) -> tuple[bool, str]:
    """
    Determine whether to DELETE or PUT ON HOLD a Detrack job on cancellation.
    Returns (should_delete: bool, current_status_description: str).
    """
    if not job:
        return True, "job_not_found"

    status = str(job.get("status") or "").lower().strip()
    assign_to = job.get("assign_to")

    if status in DETRACK_DELETABLE_API_STATUSES:
        return True, status

    if status == "dispatched" and not assign_to:
        return True, "dispatched_unassigned"

    return False, status


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

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


def _shopify_fields(order: StandardOrder) -> dict:
    if not order.source.startswith("shopify"):
        return {
            "shopify_order_id": None,
            "shopify_order_name": None,
            "shopify_order_admin_url": None,
        }

    return {
        "shopify_order_id": order.source_order_id,
        "shopify_order_name": order.source_order_name,
        "shopify_order_admin_url": None,
    }


def _order_sync_to_dict(order_sync: OrderSync) -> dict:
    return {
        "id": order_sync.id,
        "source": order_sync.source,
        "source_order_id": order_sync.source_order_id,
        "shopify_order_id": order_sync.shopify_order_id,
        "shopify_order_name": order_sync.shopify_order_name,
        "shopify_order_admin_url": order_sync.shopify_order_admin_url,
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


def _get_nested(payload: dict, *paths: tuple[str, ...]):
    for path in paths:
        current = payload
        for key in path:
            if not isinstance(current, dict):
                current = None
                break
            current = current.get(key)

        if current not in (None, ""):
            return current

    return None


# ---------------------------------------------------------------------------
# Detrack webhook handler
# ---------------------------------------------------------------------------

def extract_detrack_webhook_info(payload: dict) -> dict:
    do_number = _get_nested(
        payload,
        ("do_number",),
        ("data", "do_number"),
        ("job", "do_number"),
        ("delivery", "do_number"),
        ("tracking", "do_number"),
    )

    job_id = _get_nested(
        payload,
        ("id",),
        ("job_id",),
        ("delivery_id",),
        ("data", "id"),
        ("data", "job_id"),
        ("data", "delivery_id"),
        ("job", "id"),
        ("delivery", "id"),
    )

    status = _get_nested(
        payload,
        ("status",),
        ("delivery_status",),
        ("job_status",),
        ("tracking_status",),
        ("data", "status"),
        ("data", "delivery_status"),
        ("data", "job_status"),
        ("job", "status"),
        ("delivery", "status"),
    )

    reason = _get_nested(
        payload,
        ("reason",),
        ("failed_reason",),
        ("failure_reason",),
        ("data", "reason"),
        ("data", "failed_reason"),
        ("delivery", "reason"),
    )

    return {
        "do_number": str(do_number) if do_number is not None else None,
        "job_id": str(job_id) if job_id is not None else None,
        "status": str(status) if status is not None else "unknown",
        "reason": str(reason) if reason is not None else None,
    }


def update_delivery_status_from_detrack(db: Session, payload: dict) -> dict:
    info = extract_detrack_webhook_info(payload)

    do_number = info["do_number"]
    job_id = info["job_id"]
    status = info["status"]
    reason = info["reason"]

    order_sync = None

    if do_number:
        order_sync = (
            db.query(OrderSync)
            .filter(OrderSync.detrack_do_number == do_number)
            .first()
        )

    if not order_sync and job_id:
        order_sync = (
            db.query(OrderSync)
            .filter(OrderSync.detrack_job_id == job_id)
            .first()
        )

    if not order_sync:
        return {
            "updated": False,
            "message": "No matching order sync record found for Detrack webhook.",
            "matched_by": None,
            "received": info,
        }

    previous_status = order_sync.delivery_status

    order_sync.delivery_status = status
    order_sync.error_message = None if not reason else f"Detrack status reason: {reason}"
    order_sync.updated_at = datetime.utcnow()

    if job_id and not order_sync.detrack_job_id:
        order_sync.detrack_job_id = job_id

    shopify_fulfilment_result = None

    if status.lower() == "completed" and order_sync.shopify_order_id:
        try:
            shopify_fulfilment_result = create_shopify_fulfilment(
                shopify_order_id=order_sync.shopify_order_id,
                tracking_number=order_sync.detrack_do_number,
                tracking_url=None,
                notify_customer=False,
            )

            order_sync.error_message = (
                "Shopify fulfilment attempt: "
                f"created={shopify_fulfilment_result.get('created')}, "
                f"would_call_shopify={shopify_fulfilment_result.get('would_call_shopify')}, "
                f"blocked_by={shopify_fulfilment_result.get('blocked_by')}"
            )

        except ShopifyAdminAPIError as exc:
            shopify_fulfilment_result = {
                "created": False,
                "error": str(exc),
                "message": "Shopify fulfilment attempt failed.",
            }
            order_sync.error_message = f"Shopify fulfilment failed: {exc}"

    db.commit()
    db.refresh(order_sync)

    matched_by = "detrack_do_number" if do_number else "detrack_job_id"

    return {
        "updated": True,
        "message": "Delivery status updated from Detrack webhook.",
        "matched_by": matched_by,
        "order_sync_id": order_sync.id,
        "detrack_do_number": order_sync.detrack_do_number,
        "detrack_job_id": order_sync.detrack_job_id,
        "previous_delivery_status": previous_status,
        "new_delivery_status": order_sync.delivery_status,
        "shopify_fulfilment_result": shopify_fulfilment_result,
        "received": info,
    }


# ---------------------------------------------------------------------------
# Order sync helpers
# ---------------------------------------------------------------------------

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

    shopify_fields = _shopify_fields(order)
    detrack_payload = map_standard_order_to_detrack(order)

    order_sync = OrderSync(
        source=order.source,
        source_order_id=order.source_order_id,
        shopify_order_id=shopify_fields["shopify_order_id"],
        shopify_order_name=shopify_fields["shopify_order_name"],
        shopify_order_admin_url=shopify_fields["shopify_order_admin_url"],
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

    return {
        "created": True,
        "message": "Order saved successfully.",
        "order_sync_id": order_sync.id,
        "sync_status": order_sync.sync_status,
        "detrack_payload": detrack_payload,
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
    shopify_fields = _shopify_fields(order)

    order_sync = OrderSync(
        source=order.source,
        source_order_id=order.source_order_id,
        shopify_order_id=shopify_fields["shopify_order_id"],
        shopify_order_name=shopify_fields["shopify_order_name"],
        shopify_order_admin_url=shopify_fields["shopify_order_admin_url"],
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
        order_sync.updated_at = datetime.utcnow()

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

    except Exception as exc:
        order_sync.sync_status = "detrack_failed"
        order_sync.error_message = str(exc)
        order_sync.updated_at = datetime.utcnow()
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
        source_order_name=order_sync.shopify_order_name,
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
        order_sync.updated_at = datetime.utcnow()

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
        order_sync.updated_at = datetime.utcnow()
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


# ---------------------------------------------------------------------------
# Shopify / TikTok cancellation handler
# ---------------------------------------------------------------------------

def handle_shopify_order_cancelled(db: Session, payload: dict) -> dict:
    shopify_order_id = str(payload.get("id") or "").strip()
    shopify_order_name = str(payload.get("name") or "").strip() or None

    if not shopify_order_id:
        return {
            "updated": False,
            "message": "Shopify cancelled webhook missing order id.",
        }

    order_sync = (
        db.query(OrderSync)
        .filter(OrderSync.shopify_order_id == shopify_order_id)
        .first()
    )

    if not order_sync:
        return {
            "updated": False,
            "message": "No matching order sync record found for cancelled Shopify order.",
            "shopify_order_id": shopify_order_id,
            "shopify_order_name": shopify_order_name,
        }

    previous_sync_status = order_sync.sync_status
    previous_delivery_status = order_sync.delivery_status

    detrack_action = "not_attempted"
    detrack_result = None
    detrack_status_at_cancellation = None

    if order_sync.detrack_job_id:
        try:
            # Fetch current job from Detrack to decide delete vs on_hold
            job = get_detrack_job(order_sync.detrack_job_id)
            should_delete, detrack_status_at_cancellation = _should_delete_detrack_job(job)

            if should_delete:
                # Job not yet picked up by driver — delete cleanly
                deleted = delete_detrack_job(order_sync.detrack_job_id)
                detrack_action = "deleted"
                detrack_result = {
                    "deleted": deleted,
                    "reason": (
                        f"Job status was '{detrack_status_at_cancellation}' "
                        "— deleted cleanly before driver pickup."
                    ),
                }

                # Clean removal from middleware DB
                db.delete(order_sync)
                db.commit()

                return {
                    "updated": True,
                    "message": "Order and Detrack job deleted cleanly (driver had not yet picked up).",
                    "shopify_order_id": shopify_order_id,
                    "shopify_order_name": shopify_order_name,
                    "detrack_action": detrack_action,
                    "detrack_status_at_cancellation": detrack_status_at_cancellation,
                    "detrack_result": detrack_result,
                    "previous_sync_status": previous_sync_status,
                    "previous_delivery_status": previous_delivery_status,
                }

            else:
                # Driver is already out for delivery — put on hold
                source = order_sync.source or "shopify"
                source_label = "TIKTOK" if "tiktok" in source.lower() else "SHOPIFY"

                detrack_result = update_detrack_job_as_cancelled(
                    job_id=order_sync.detrack_job_id,
                    do_number=order_sync.detrack_do_number,
                    reason=f"{source_label} order cancelled",
                    cancel_message=f"CANCELLED FROM {source_label} - DO NOT DELIVER",
                )
                detrack_action = "on_hold"

        except DetrackAPIError as exc:
            detrack_action = "error"
            detrack_result = {"error": str(exc)}

    # Update DB record for on_hold or error cases
    # (delete case already returned above)
    order_sync.sync_status = "cancelled"
    order_sync.delivery_status = "cancelled"
    order_sync.error_message = (
        f"Order cancelled. "
        f"Previous sync_status={previous_sync_status}, "
        f"previous delivery_status={previous_delivery_status}. "
        f"Detrack action={detrack_action}, "
        f"detrack_status_at_cancellation={detrack_status_at_cancellation}."
    )
    order_sync.updated_at = datetime.utcnow()

    db.commit()
    db.refresh(order_sync)

    return {
        "updated": True,
        "message": f"Order cancelled. Detrack job action: {detrack_action}.",
        "order_sync_id": order_sync.id,
        "shopify_order_id": order_sync.shopify_order_id,
        "shopify_order_name": order_sync.shopify_order_name,
        "detrack_do_number": order_sync.detrack_do_number,
        "detrack_job_id": order_sync.detrack_job_id,
        "detrack_action": detrack_action,
        "detrack_status_at_cancellation": detrack_status_at_cancellation,
        "previous_sync_status": previous_sync_status,
        "previous_delivery_status": previous_delivery_status,
        "new_sync_status": order_sync.sync_status,
        "new_delivery_status": order_sync.delivery_status,
        "detrack_result": detrack_result,
    }


# ---------------------------------------------------------------------------
# Shopee cancellation handler (native Detrack integration)
# ---------------------------------------------------------------------------

def cancel_shopee_detrack_job(shopee_order_sn: str) -> dict:
    raw_sn = str(shopee_order_sn or "").strip()

    if not raw_sn:
        return {
            "updated": False,
            "message": "Shopee order number is missing.",
        }

    # Native Shopee→Detrack integration prefixes DO numbers with "SP"
    do_number = raw_sn if raw_sn.upper().startswith("SP") else f"SP{raw_sn}"

    try:
        job = find_detrack_job_by_do_number(do_number)
    except Exception as exc:
        return {
            "updated": False,
            "message": "Failed to look up Detrack job.",
            "detrack_do_number": do_number,
            "error": str(exc),
        }

    if not job:
        return {
            "updated": False,
            "message": "No Detrack job found for Shopee order number.",
            "detrack_do_number": do_number,
        }

    job_id = job.get("id")

    if not job_id:
        return {
            "updated": False,
            "message": "Detrack job found but missing job id.",
            "detrack_do_number": do_number,
            "job": job,
        }

    # Apply same delete vs on_hold logic for Shopee jobs
    should_delete, detrack_status_at_cancellation = _should_delete_detrack_job(job)

    if should_delete:
        try:
            deleted = delete_detrack_job(job_id)
            return {
                "updated": True,
                "message": "Shopee Detrack job deleted cleanly (driver had not yet picked up).",
                "detrack_do_number": do_number,
                "detrack_job_id": job_id,
                "detrack_action": "deleted",
                "detrack_status_at_cancellation": detrack_status_at_cancellation,
                "deleted": deleted,
            }
        except Exception as exc:
            return {
                "updated": False,
                "message": "Failed to delete Detrack job.",
                "detrack_do_number": do_number,
                "detrack_job_id": job_id,
                "error": str(exc),
            }

    try:
        result = update_detrack_job_as_cancelled(
            job_id=job_id,
            do_number=do_number,
            reason="Shopee order cancelled",
            cancel_message="CANCELLED FROM SHOPEE - DO NOT DELIVER",
        )
    except Exception as exc:
        return {
            "updated": False,
            "message": "Failed to update Detrack job as cancelled.",
            "detrack_do_number": do_number,
            "detrack_job_id": job_id,
            "error": str(exc),
        }

    data = result.get("data") or {}

    return {
        "updated": True,
        "message": "Shopee Detrack job placed on hold (driver already out for delivery).",
        "detrack_do_number": do_number,
        "detrack_job_id": job_id,
        "detrack_action": "on_hold",
        "detrack_status_at_cancellation": detrack_status_at_cancellation,
        "status": data.get("status"),
        "tracking_status": data.get("tracking_status"),
        "reason": data.get("reason"),
        "instructions": data.get("instructions"),
        "note": data.get("note"),
    }
