import json
import logging
from datetime import datetime, timedelta

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
from app.models import OrderSync, OrderSyncLog
from app.schemas import StandardOrder, StandardOrderItem
from app.shopify_admin_client import ShopifyAdminAPIError, create_shopify_fulfilment
from app.telegram_client import send_permanent_failure_alert, send_retry_success_alert

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Retry configuration
# ---------------------------------------------------------------------------

MAX_RETRY_ATTEMPTS = 5

# Delay in minutes after each failed attempt before next retry
RETRY_DELAYS_MINUTES = [1, 5, 15, 30, 60]


def _next_retry_delay(retry_count: int) -> int:
    """Return delay in minutes for the given retry attempt (0-indexed)."""
    if retry_count < len(RETRY_DELAYS_MINUTES):
        return RETRY_DELAYS_MINUTES[retry_count]
    return RETRY_DELAYS_MINUTES[-1]


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
        "retry_count": order_sync.retry_count,
        "last_retry_at": order_sync.last_retry_at.isoformat() if order_sync.last_retry_at else None,
        "next_retry_at": order_sync.next_retry_at.isoformat() if order_sync.next_retry_at else None,
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


def _log_status_change(
    db: Session,
    order_sync_id: int,
    log_type: str,
    from_status: str | None,
    to_status: str | None,
    note: str | None = None,
) -> None:
    """
    Record a status change in order_sync_log.
    log_type: "sync" or "delivery"
    Never raises — logging should never break the main flow.
    """
    try:
        entry = OrderSyncLog(
            order_sync_id=order_sync_id,
            log_type=log_type,
            from_status=from_status,
            to_status=to_status,
            note=note,
            created_at=datetime.utcnow(),
        )
        db.add(entry)
        db.flush()
    except Exception as exc:
        logger.warning(f"[StatusLog] Failed to write log entry: {exc}")


def _resolve_detrack_job(order_sync: OrderSync) -> dict | None:
    """
    Try to fetch the Detrack job for an order sync record.
    First tries by detrack_job_id, then falls back to DO number search.
    Returns the job dict or None if not found.
    """
    # Primary: look up by job ID
    if order_sync.detrack_job_id:
        try:
            job = get_detrack_job(order_sync.detrack_job_id)
            if job:
                return job
        except DetrackAPIError:
            pass

    # Fallback: search by DO number
    if order_sync.detrack_do_number:
        try:
            job = find_detrack_job_by_do_number(order_sync.detrack_do_number)
            if job:
                logger.info(
                    f"[resolve_detrack_job] Found job by DO number fallback: "
                    f"{order_sync.detrack_do_number}"
                )
                return job
        except DetrackAPIError:
            pass

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
    _log_status_change(
        db, order_sync.id, "delivery", previous_status, status,
        f"Detrack webhook update. Reason: {reason}" if reason else "Detrack webhook update."
    )
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
        logger.info(
            f"[Dedup] Duplicate webhook detected — "
            f"source={order.source}, source_order_id={order.source_order_id}, "
            f"existing_id={existing.id}, sync_status={existing.sync_status}"
        )
        return {
            "created": False,
            "sent_to_detrack": False,
            "message": "Order already exists. Skipping duplicate.",
            "duplicate": True,
            "order_sync_id": existing.id,
            "sync_status": existing.sync_status,
            "detrack_do_number": existing.detrack_do_number,
        }

    detrack_payload = map_standard_order_to_detrack(order)
    shopify_fields = _shopify_fields(order)

    # Use the resolved delivery date from the payload (SGT today if not provided)
    resolved_delivery_date = detrack_payload["data"]["date"]

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
        delivery_date=resolved_delivery_date,
        detrack_do_number=detrack_payload["data"]["do_number"],
        sync_status="pending",
        retry_count=0,
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
        _log_status_change(db, order_sync.id, "sync", "pending", "sent_to_detrack", "Order sent to Detrack successfully.")
        _log_status_change(db, order_sync.id, "delivery", None, "created", "Detrack job created.")
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
        order_sync.next_retry_at = datetime.utcnow() + timedelta(
            minutes=_next_retry_delay(0)
        )
        db.commit()
        _log_status_change(db, order_sync.id, "sync", "pending", "detrack_failed", f"Detrack creation failed: {exc}")
        db.commit()

        return {
            "created": True,
            "sent_to_detrack": False,
            "message": "Order saved, but Detrack creation failed. Will auto-retry.",
            "order_sync_id": order_sync.id,
            "sync_status": order_sync.sync_status,
            "error": str(exc),
            "next_retry_at": order_sync.next_retry_at.isoformat(),
            "detrack_payload": detrack_payload,
        }


def retry_failed_detrack_sync(db: Session, order_sync_id: int) -> dict:
    """Manual retry for a specific failed order."""
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
        order_sync.next_retry_at = None
        order_sync.last_retry_at = datetime.utcnow()

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
        order_sync.last_retry_at = datetime.utcnow()
        order_sync.retry_count = (order_sync.retry_count or 0) + 1
        order_sync.next_retry_at = datetime.utcnow() + timedelta(
            minutes=_next_retry_delay(order_sync.retry_count)
        )
        db.commit()

        return {
            "retried": True,
            "sent_to_detrack": False,
            "message": "Retry failed. Detrack still could not create the job.",
            "order_sync_id": order_sync.id,
            "sync_status": order_sync.sync_status,
            "retry_count": order_sync.retry_count,
            "next_retry_at": order_sync.next_retry_at.isoformat(),
            "error": str(exc),
            "detrack_payload": detrack_payload,
        }


def auto_retry_failed_detrack_jobs(db: Session) -> dict:
    """
    Automatically retry all eligible failed Detrack jobs.
    Called by the scheduler every 5 minutes.
    Eligible = sync_status is detrack_failed AND next_retry_at is due AND retry_count < MAX.
    """
    now = datetime.utcnow()

    eligible = (
        db.query(OrderSync)
        .filter(
            OrderSync.sync_status == "detrack_failed",
            OrderSync.retry_count < MAX_RETRY_ATTEMPTS,
            OrderSync.next_retry_at <= now,
        )
        .all()
    )

    attempted = 0
    succeeded = 0
    failed = 0
    permanent = 0

    for order_sync in eligible:
        attempted += 1

        reconstructed_order = StandardOrder(
            source=order_sync.source,
            source_order_id=order_sync.source_order_id,
            source_order_name=order_sync.shopify_order_name,
            customer_name=order_sync.customer_name or "Unknown Customer",
            phone=order_sync.phone or "",
            address=order_sync.address or "",
            postal_code=order_sync.postal_code,
            items=_items_from_json(order_sync.items_json),
            remarks=order_sync.remarks or "Auto-retried from middleware.",
            delivery_date=order_sync.delivery_date,
        )

        detrack_payload = map_standard_order_to_detrack(reconstructed_order)

        try:
            detrack_response = create_detrack_job(detrack_payload)

            order_sync.sync_status = "sent_to_detrack"
            order_sync.delivery_status = "created"
            order_sync.error_message = None
            order_sync.detrack_do_number = detrack_payload["data"]["do_number"]
            order_sync.updated_at = now
            order_sync.last_retry_at = now
            order_sync.next_retry_at = None

            data = detrack_response.get("data") or detrack_response
            if isinstance(data, dict):
                order_sync.detrack_job_id = str(
                    data.get("id")
                    or data.get("job_id")
                    or data.get("delivery_id")
                    or ""
                )

            db.commit()
            _log_status_change(
                db, order_sync.id, "sync", "detrack_failed", "sent_to_detrack",
                f"Auto-retry succeeded on attempt {(order_sync.retry_count or 0) + 1}."
            )
            _log_status_change(db, order_sync.id, "delivery", None, "created", "Detrack job created via auto-retry.")
            db.commit()
            succeeded += 1

            logger.info(
                f"[AutoRetry] Order {order_sync.id} succeeded on attempt "
                f"{order_sync.retry_count + 1}."
            )

            # Send recovery alert if this was a retry (not first attempt)
            if (order_sync.retry_count or 0) > 0:
                send_retry_success_alert(
                    order_sync_id=order_sync.id,
                    source=order_sync.source,
                    source_order_id=order_sync.source_order_id,
                    customer_name=order_sync.customer_name,
                    detrack_do_number=order_sync.detrack_do_number,
                    retry_count=order_sync.retry_count,
                )

        except Exception as exc:
            new_retry_count = (order_sync.retry_count or 0) + 1
            order_sync.retry_count = new_retry_count
            order_sync.last_retry_at = now
            order_sync.error_message = f"Auto-retry attempt {new_retry_count} failed: {exc}"
            order_sync.updated_at = now

            if new_retry_count >= MAX_RETRY_ATTEMPTS:
                order_sync.sync_status = "detrack_failed_permanent"
                order_sync.next_retry_at = None
                permanent += 1
                logger.warning(
                    f"[AutoRetry] Order {order_sync.id} permanently failed after "
                    f"{new_retry_count} attempts. Manual intervention required."
                )
                db.commit()
                _log_status_change(
                    db, order_sync.id, "sync", "detrack_failed", "detrack_failed_permanent",
                    f"All {new_retry_count} retry attempts exhausted. Manual intervention required."
                )
                db.commit()
                send_permanent_failure_alert(
                    order_sync_id=order_sync.id,
                    source=order_sync.source,
                    source_order_id=order_sync.source_order_id,
                    customer_name=order_sync.customer_name,
                    detrack_do_number=order_sync.detrack_do_number,
                    error_message=order_sync.error_message,
                    retry_count=new_retry_count,
                )
            else:
                delay = _next_retry_delay(new_retry_count)
                order_sync.next_retry_at = now + timedelta(minutes=delay)
                failed += 1
                logger.warning(
                    f"[AutoRetry] Order {order_sync.id} failed attempt {new_retry_count}. "
                    f"Next retry in {delay} minutes."
                )

            db.commit()

    return {
        "attempted": attempted,
        "succeeded": succeeded,
        "failed": failed,
        "permanent": permanent,
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

    # Resolve the Detrack job — try job_id first, fall back to DO number
    try:
        job = _resolve_detrack_job(order_sync)

        # Update detrack_job_id if we found it via fallback
        if job and not order_sync.detrack_job_id:
            resolved_job_id = str(job.get("id") or "")
            if resolved_job_id:
                order_sync.detrack_job_id = resolved_job_id

        should_delete, detrack_status_at_cancellation = _should_delete_detrack_job(job)

        if should_delete:
            job_id_to_use = order_sync.detrack_job_id or (
                str(job.get("id")) if job else None
            )

            if job_id_to_use:
                deleted = delete_detrack_job(job_id_to_use)
            else:
                deleted = False

            detrack_action = "deleted"
            detrack_result = {
                "deleted": deleted,
                "reason": (
                    f"Job status was '{detrack_status_at_cancellation}' "
                    "— deleted cleanly before driver pickup."
                ),
            }

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
            source = order_sync.source or "shopify"
            source_label = "TIKTOK" if "tiktok" in source.lower() else "SHOPIFY"
            job_id_to_use = order_sync.detrack_job_id or (
                str(job.get("id")) if job else None
            )

            if job_id_to_use:
                detrack_result = update_detrack_job_as_cancelled(
                    job_id=job_id_to_use,
                    do_number=order_sync.detrack_do_number,
                    reason=f"{source_label} order cancelled",
                    cancel_message=f"CANCELLED FROM {source_label} - DO NOT DELIVER",
                )
                detrack_action = "on_hold"
            else:
                detrack_action = "not_attempted"
                detrack_result = {"reason": "No job ID or DO number available."}

    except DetrackAPIError as exc:
        detrack_action = "error"
        detrack_result = {"error": str(exc)}

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
    _log_status_change(
        db, order_sync.id, "sync", previous_sync_status, "cancelled",
        f"Order cancelled via Shopify webhook. Detrack action: {detrack_action}."
    )
    _log_status_change(
        db, order_sync.id, "delivery", previous_delivery_status, "cancelled",
        f"Detrack job {detrack_action} at cancellation. Status was: {detrack_status_at_cancellation}."
    )
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
