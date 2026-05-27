import json

from fastapi import Body, Depends, FastAPI, Header, HTTPException, Query, Request
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
)
from app.webhook_security import verify_shopify_hmac


Base.metadata.create_all(bind=engine)

app = FastAPI(title=settings.app_name)


@app.get("/health")
def health_check():
    return {
        "status": "ok",
        "service": settings.app_name,
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
