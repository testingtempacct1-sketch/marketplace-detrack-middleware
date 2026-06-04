import json
from pathlib import Path

from fastapi import Body, Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy.orm import Session

from app.admin_security import require_admin_key
from app.detrack_webhook_security import require_detrack_webhook_key
from app.config import settings
from app.connectors.shopify import (
    mock_shopify_order_to_standard,
    shopify_order_to_standard,
)
from app.connectors.shopee import mock_shopee_order_to_standard
from app.connectors.tiktok_shop import mock_tiktok_shop_order_to_standard
from app.database import Base, engine, get_db
from app.schemas import StandardOrder
from app.sync_service import (
    create_or_get_order_sync,
    create_order_and_send_to_detrack,
    list_recent_order_syncs,
    retry_failed_detrack_sync,
    update_delivery_status_from_detrack,
    handle_shopify_order_cancelled,
    cancel_shopee_detrack_job,
)
from app.webhook_security import verify_shopify_hmac
from app.db_maintenance import ensure_order_sync_schema
from app.scheduler import start_scheduler, stop_scheduler
from app.shopify_admin_client import (
    ShopifyAdminAPIError,
    build_shopify_fulfilment_dry_run,
    create_shopify_fulfilment,
    get_shopify_fulfilment_plan,
    get_shopify_order_by_id,
)
from app.models import OrderSync, OrderSyncLog


Base.metadata.create_all(bind=engine)
ensure_order_sync_schema()


app = FastAPI(title=settings.app_name)


@app.on_event("startup")
def on_startup():
    start_scheduler()


@app.on_event("shutdown")
def on_shutdown():
    stop_scheduler()


@app.get("/health")
def health_check(db: Session = Depends(get_db)):
    # DB connectivity check
    db_ok = False
    db_error = None
    try:
        from sqlalchemy import text
        db.execute(text("SELECT 1"))
        db_ok = True
    except Exception as exc:
        db_error = str(exc)

    # Detrack API reachability check
    detrack_ok = False
    detrack_error = None
    try:
        import requests as req
        response = req.get(
            settings.detrack_base_url,
            headers={"X-API-Key": settings.detrack_api_key},
            params={"limit": 1},
            timeout=5,
        )
        detrack_ok = response.status_code < 500
    except Exception as exc:
        detrack_error = str(exc)

    # Failed order counts
    failed_count = 0
    permanent_count = 0
    try:
        failed_count = (
            db.query(OrderSync)
            .filter(OrderSync.sync_status == "detrack_failed")
            .count()
        )
        permanent_count = (
            db.query(OrderSync)
            .filter(OrderSync.sync_status == "detrack_failed_permanent")
            .count()
        )
    except Exception:
        pass

    # SSL certificate expiry check
    ssl_ok = False
    ssl_days_remaining = None
    ssl_error = None
    try:
        import subprocess
        import re
        result = subprocess.run(
            ["certbot", "certificates"],
            capture_output=True, text=True, timeout=10
        )
        match = re.search(r"VALID: (\d+) days", result.stdout)
        if match:
            ssl_days_remaining = int(match.group(1))
            ssl_ok = ssl_days_remaining > 14
        else:
            ssl_error = "Could not parse certificate expiry"
    except Exception as exc:
        ssl_error = str(exc)

    overall = "ok" if db_ok and detrack_ok and ssl_ok else "degraded"

    return {
        "status": overall,
        "service": settings.app_name,
        "checks": {
            "database": {"ok": db_ok, "error": db_error},
            "detrack_api": {"ok": detrack_ok, "error": detrack_error},
            "ssl_certificate": {
                "ok": ssl_ok,
                "days_remaining": ssl_days_remaining,
                "error": ssl_error,
            },
        },
        "detrack_failed_pending_retry": failed_count,
        "detrack_failed_permanent": permanent_count,
    }


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard():
    html_path = Path(__file__).parent / "templates" / "dashboard.html"
    return HTMLResponse(content=html_path.read_text(encoding="utf-8"))


@app.get("/orders/search")
def search_orders(
    q: str | None = Query(default=None, description="Search by customer name, order ref, or DO number"),
    date: str | None = Query(default=None, description="Filter by delivery date (YYYY-MM-DD)"),
    source: str | None = Query(default=None, description="Filter by source (shopify, tiktok_shop, shopee)"),
    status: str | None = Query(default=None, description="Filter by sync_status"),
    limit: int = Query(default=50, ge=1, le=200),
    _: bool = Depends(require_admin_key),
    db: Session = Depends(get_db),
):
    query = db.query(OrderSync)

    if q:
        search = f"%{q}%"
        query = query.filter(
            (OrderSync.customer_name.ilike(search))
            | (OrderSync.source_order_id.ilike(search))
            | (OrderSync.detrack_do_number.ilike(search))
            | (OrderSync.shopify_order_name.ilike(search))
        )

    if date:
        query = query.filter(OrderSync.delivery_date == date)

    if source:
        query = query.filter(OrderSync.source.ilike(f"%{source}%"))

    if status:
        query = query.filter(OrderSync.sync_status == status)

    records = query.order_by(OrderSync.id.desc()).limit(limit).all()

    return {
        "count": len(records),
        "filters": {"q": q, "date": date, "source": source, "status": status},
        "orders": [
            {
                "id": r.id,
                "source": r.source,
                "source_order_id": r.source_order_id,
                "shopify_order_name": r.shopify_order_name,
                "customer_name": r.customer_name,
                "detrack_do_number": r.detrack_do_number,
                "detrack_job_id": r.detrack_job_id,
                "sync_status": r.sync_status,
                "delivery_status": r.delivery_status,
                "delivery_date": r.delivery_date,
                "retry_count": r.retry_count,
                "error_message": r.error_message,
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "updated_at": r.updated_at.isoformat() if r.updated_at else None,
            }
            for r in records
        ],
    }


@app.get("/orders/stats")
def order_stats(
    _: bool = Depends(require_admin_key),
    db: Session = Depends(get_db),
):
    """Returns order counts broken down by source and sync_status."""
    from sqlalchemy import func

    rows = (
        db.query(OrderSync.source, OrderSync.sync_status, func.count(OrderSync.id))
        .group_by(OrderSync.source, OrderSync.sync_status)
        .all()
    )

    # Build breakdown by source
    by_source = {}
    total_by_status = {}

    for source, status, count in rows:
        source = source or "unknown"
        if source not in by_source:
            by_source[source] = {"total": 0}
        by_source[source][status] = count
        by_source[source]["total"] += count
        total_by_status[status] = total_by_status.get(status, 0) + count

    return {
        "by_source": by_source,
        "by_status": total_by_status,
        "total": sum(total_by_status.values()),
    }


@app.get("/orders/recent")
def recent_orders(
    limit: int = Query(default=20, ge=1, le=100),
    _: bool = Depends(require_admin_key),
    db: Session = Depends(get_db),
):
    return {
        "count": limit,
        "orders": list_recent_order_syncs(db, limit=limit),
    }


@app.get("/orders/failed")
def failed_orders(
    _: bool = Depends(require_admin_key),
    db: Session = Depends(get_db),
):
    """List all orders that have permanently failed and need manual intervention."""
    records = (
        db.query(OrderSync)
        .filter(
            OrderSync.sync_status.in_(
                ["detrack_failed", "detrack_failed_permanent"]
            )
        )
        .order_by(OrderSync.id.desc())
        .all()
    )

    return {
        "count": len(records),
        "orders": [
            {
                "id": r.id,
                "source": r.source,
                "source_order_id": r.source_order_id,
                "customer_name": r.customer_name,
                "detrack_do_number": r.detrack_do_number,
                "sync_status": r.sync_status,
                "retry_count": r.retry_count,
                "last_retry_at": r.last_retry_at.isoformat() if r.last_retry_at else None,
                "next_retry_at": r.next_retry_at.isoformat() if r.next_retry_at else None,
                "error_message": r.error_message,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in records
        ],
    }


@app.get("/orders/{order_sync_id}/label")
def preview_order_label(
    order_sync_id: int,
    _: bool = Depends(require_admin_key),
    db: Session = Depends(get_db),
):
    """Preview the shipping label PDF for a specific order in the browser."""
    from app.label_generator import generate_label_pdf
    import json

    order = db.query(OrderSync).filter(OrderSync.id == order_sync_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found.")

    # Parse items
    items = []
    if order.items_json:
        try:
            raw_items = json.loads(order.items_json)
            items = [
                {
                    "description": item.get("name") or "Item",
                    "quantity": item.get("quantity") or 1,
                }
                for item in raw_items
            ]
        except Exception:
            items = [{"description": "Order items", "quantity": 1}]

    pdf_bytes = generate_label_pdf(
        do_number=order.detrack_do_number or "",
        source=order.source or "shopify",
        customer_name=order.customer_name or "",
        phone=order.phone or "",
        address=order.address or "",
        postal_code=order.postal_code,
        items=items,
        remarks=order.remarks,
        delivery_date=order.delivery_date,
    )

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f"inline; filename=label-{order.detrack_do_number or order_sync_id}.pdf"
        },
    )


@app.get("/admin/label/test")
def preview_test_label(
    _: bool = Depends(require_admin_key),
):
    """Generate and preview a sample shipping label with dummy data."""
    from app.label_generator import generate_label_pdf
    from datetime import datetime
    from zoneinfo import ZoneInfo

    today = datetime.now(ZoneInfo("Asia/Singapore")).strftime("%Y-%m-%d")

    pdf_bytes = generate_label_pdf(
        do_number="ZF-SH-1352",
        source="shopify",
        customer_name="John Tan Wei Ming",
        phone="+65 91234567",
        address="123 Tampines Street 45, #08-12",
        postal_code="529538",
        items=[
            {"description": "Black Thorn 黑刺 Durian 1500g", "quantity": 1},
            {"description": "Mao Shan Wang 猫山王 400g", "quantity": 2},
        ],
        remarks="Please call before delivery. Leave at door if no answer.",
        delivery_date=today,
    )

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": "inline; filename=test-label.pdf"
        },
    )


@app.put("/orders/{order_sync_id}")
def update_order(
    order_sync_id: int,
    customer_name: str | None = Body(default=None),
    phone: str | None = Body(default=None),
    address: str | None = Body(default=None),
    postal_code: str | None = Body(default=None),
    remarks: str | None = Body(default=None),
    delivery_date: str | None = Body(default=None),
    items_json: str | None = Body(default=None),
    sync_to_detrack: bool = Body(default=True),
    _: bool = Depends(require_admin_key),
    db: Session = Depends(get_db),
):
    """Update order details and optionally sync changes to Detrack."""
    from app.detrack_client import DetrackAPIError
    from datetime import datetime
    import requests as req

    order = db.query(OrderSync).filter(OrderSync.id == order_sync_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found.")

    # Track what changed
    changes = []

    if customer_name is not None and customer_name != order.customer_name:
        order.customer_name = customer_name
        changes.append("customer_name")

    if phone is not None and phone != order.phone:
        order.phone = phone
        changes.append("phone")

    if address is not None and address != order.address:
        order.address = address
        changes.append("address")

    if postal_code is not None and postal_code != order.postal_code:
        order.postal_code = postal_code
        changes.append("postal_code")

    if remarks is not None and remarks != order.remarks:
        order.remarks = remarks
        changes.append("remarks")

    if delivery_date is not None and delivery_date != order.delivery_date:
        order.delivery_date = delivery_date
        changes.append("delivery_date")

    if items_json is not None and items_json != order.items_json:
        order.items_json = items_json
        changes.append("items")

    if not changes:
        return {
            "updated": False,
            "message": "No changes detected.",
            "order_sync_id": order_sync_id,
        }

    order.updated_at = datetime.utcnow()

    # Sync changes to Detrack if job exists
    detrack_sync_result = None
    if sync_to_detrack and order.detrack_job_id:
        try:
            import json
            items = []
            if order.items_json:
                try:
                    raw_items = json.loads(order.items_json)
                    items = [
                        {
                            "description": item.get("name") or "Item",
                            "quantity": item.get("quantity") or 1,
                            "sku": item.get("sku"),
                        }
                        for item in raw_items
                    ]
                except Exception:
                    pass

            payload = {
                "data": {
                    "deliver_to": order.customer_name,
                    "phone_number": order.phone,
                    "address": order.address,
                    "postal_code": order.postal_code,
                    "instructions": order.remarks or "",
                    "date": order.delivery_date,
                }
            }

            if items:
                payload["data"]["items"] = items

            response = req.put(
                f"{settings.detrack_base_url}/{order.detrack_job_id}",
                headers={
                    "Content-Type": "application/json",
                    "X-API-Key": settings.detrack_api_key,
                },
                json=payload,
                timeout=10,
            )
            detrack_sync_result = {
                "synced": response.status_code < 400,
                "status_code": response.status_code,
            }
        except Exception as exc:
            detrack_sync_result = {"synced": False, "error": str(exc)}

    db.commit()
    db.refresh(order)

    return {
        "updated": True,
        "message": f"Order updated. Changed: {', '.join(changes)}",
        "order_sync_id": order_sync_id,
        "changes": changes,
        "detrack_sync": detrack_sync_result,
    }


@app.post("/orders/{order_sync_id}/reprint")
def reprint_order_label(
    order_sync_id: int,
    _: bool = Depends(require_admin_key),
    db: Session = Depends(get_db),
):
    """Manually reprint the shipping label for an order."""
    from app.printnode_client import print_shipping_label
    from datetime import datetime

    order = db.query(OrderSync).filter(OrderSync.id == order_sync_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found.")

    result = print_shipping_label(order)

    if result.get("printed"):
        order.label_printed = "printed"
        order.label_print_error = None
    else:
        order.label_printed = "failed"
        order.label_print_error = result.get("reason", "Unknown error")

    order.updated_at = datetime.utcnow()
    db.commit()

    return {
        "reprinted": result.get("printed", False),
        "order_sync_id": order_sync_id,
        "detrack_do_number": order.detrack_do_number,
        "result": result,
    }


@app.get("/orders/{order_sync_id}/logs")
def get_order_logs(
    order_sync_id: int,
    _: bool = Depends(require_admin_key),
    db: Session = Depends(get_db),
):
    order = db.query(OrderSync).filter(OrderSync.id == order_sync_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found.")

    logs = (
        db.query(OrderSyncLog)
        .filter(OrderSyncLog.order_sync_id == order_sync_id)
        .order_by(OrderSyncLog.created_at.asc())
        .all()
    )

    return {
        "order_sync_id": order_sync_id,
        "source_order_id": order.source_order_id,
        "customer_name": order.customer_name,
        "logs": [
            {
                "id": log.id,
                "log_type": log.log_type,
                "from_status": log.from_status,
                "to_status": log.to_status,
                "note": log.note,
                "created_at": log.created_at.isoformat() if log.created_at else None,
            }
            for log in logs
        ],
    }


@app.get("/admin/shopify/orders/{shopify_order_id}")
def admin_get_shopify_order(
    shopify_order_id: str,
    _: bool = Depends(require_admin_key),
):
    try:
        order = get_shopify_order_by_id(shopify_order_id)
    except ShopifyAdminAPIError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return {
        "found": True,
        "order": order,
    }


@app.get("/admin/shopify/orders/{shopify_order_id}/fulfilment-plan")
def admin_get_shopify_fulfilment_plan(
    shopify_order_id: str,
    _: bool = Depends(require_admin_key),
):
    try:
        plan = get_shopify_fulfilment_plan(shopify_order_id)
    except ShopifyAdminAPIError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return plan


@app.post("/admin/shopify/orders/{shopify_order_id}/fulfilment-dry-run")
def admin_shopify_fulfilment_dry_run(
    shopify_order_id: str,
    tracking_number: str | None = Body(default=None),
    tracking_url: str | None = Body(default=None),
    notify_customer: bool = Body(default=False),
    _: bool = Depends(require_admin_key),
):
    try:
        result = build_shopify_fulfilment_dry_run(
            shopify_order_id=shopify_order_id,
            tracking_number=tracking_number,
            tracking_url=tracking_url,
            notify_customer=notify_customer,
        )
    except ShopifyAdminAPIError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return result


@app.post("/admin/shopify/orders/{shopify_order_id}/fulfil")
def admin_shopify_fulfil_order(
    shopify_order_id: str,
    tracking_number: str | None = Body(default=None),
    tracking_url: str | None = Body(default=None),
    notify_customer: bool = Body(default=False),
    _: bool = Depends(require_admin_key),
):
    try:
        result = create_shopify_fulfilment(
            shopify_order_id=shopify_order_id,
            tracking_number=tracking_number,
            tracking_url=tracking_url,
            notify_customer=notify_customer,
        )
    except ShopifyAdminAPIError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return result


@app.post("/admin/shopee/orders/{shopee_order_sn}/cancel-detrack")
def admin_cancel_shopee_detrack_job(
    shopee_order_sn: str,
    _: bool = Depends(require_admin_key),
):
    return cancel_shopee_detrack_job(shopee_order_sn)


@app.post("/orders/test-standard")
def test_standard_order(
    order: StandardOrder,
    db: Session = Depends(get_db),
):
    result = create_or_get_order_sync(db, order)
    return result


@app.post("/orders/test-send-detrack")
def test_send_detrack(
    order: StandardOrder,
    db: Session = Depends(get_db),
):
    result = create_order_and_send_to_detrack(db, order)
    return result


@app.post("/orders/retry-failed/{order_sync_id}")
def retry_failed_order(
    order_sync_id: int,
    _: bool = Depends(require_admin_key),
    db: Session = Depends(get_db),
):
    result = retry_failed_detrack_sync(db, order_sync_id)
    return result


@app.post("/connectors/shopify/test")
def test_shopify_connector(
    _: bool = Depends(require_admin_key),
    db: Session = Depends(get_db),
):
    order = mock_shopify_order_to_standard()
    result = create_order_and_send_to_detrack(db, order)
    return result


@app.post("/connectors/shopee/test")
def test_shopee_connector(
    _: bool = Depends(require_admin_key),
    db: Session = Depends(get_db),
):
    order = mock_shopee_order_to_standard()
    result = create_order_and_send_to_detrack(db, order)
    return result


@app.post("/connectors/tiktok-shop/test")
def test_tiktok_shop_connector(
    _: bool = Depends(require_admin_key),
    db: Session = Depends(get_db),
):
    order = mock_tiktok_shop_order_to_standard()
    result = create_order_and_send_to_detrack(db, order)
    return result


@app.post("/webhooks/detrack/job-status")
def detrack_job_status_webhook(
    payload: dict = Body(...),
    _: bool = Depends(require_detrack_webhook_key),
    db: Session = Depends(get_db),
):
    result = update_delivery_status_from_detrack(db, payload)

    return {
        "received": True,
        "result": result,
    }


@app.post("/webhooks/shopify/orders-create")
async def shopify_orders_create_webhook(
    request: Request,
    x_shopify_hmac_sha256: str | None = Header(default=None),
    x_shopify_topic: str | None = Header(default=None),
    x_shopify_shop_domain: str | None = Header(default=None),
    db: Session = Depends(get_db),
):
    raw_body = await request.body()

    is_valid = verify_shopify_hmac(
        raw_body=raw_body,
        received_hmac=x_shopify_hmac_sha256,
        secret=settings.shopify_webhook_secret,
    )

    if not is_valid:
        raise HTTPException(status_code=401, detail="Invalid Shopify webhook signature")

    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON payload") from exc

    order = shopify_order_to_standard(payload)
    result = create_order_and_send_to_detrack(db, order)

    return {
        "received": True,
        "topic": x_shopify_topic,
        "shop": x_shopify_shop_domain,
        "result": result,
    }


@app.post("/webhooks/shopify/orders-cancelled")
async def shopify_order_cancelled_webhook(
    request: Request,
    x_shopify_hmac_sha256: str | None = Header(default=None),
    x_shopify_topic: str | None = Header(default=None),
    x_shopify_shop_domain: str | None = Header(default=None),
    db: Session = Depends(get_db),
):
    raw_body = await request.body()

    is_valid = verify_shopify_hmac(
        raw_body=raw_body,
        received_hmac=x_shopify_hmac_sha256,
        secret=settings.shopify_webhook_secret,
    )

    if not is_valid:
        raise HTTPException(status_code=401, detail="Invalid Shopify webhook signature")

    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON payload") from exc

    result = handle_shopify_order_cancelled(db, payload)

    return {
        "received": True,
        "topic": x_shopify_topic,
        "shop": x_shopify_shop_domain,
        "result": result,
    }
