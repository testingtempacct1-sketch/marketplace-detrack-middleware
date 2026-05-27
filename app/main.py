from fastapi import Depends, FastAPI
from sqlalchemy.orm import Session

from app.config import settings
from app.connectors.shopify import mock_shopify_order_to_standard
from app.connectors.shopee import mock_shopee_order_to_standard
from app.connectors.tiktok_shop import mock_tiktok_shop_order_to_standard
from app.database import Base, engine, get_db
from app.schemas import StandardOrder
from app.sync_service import (
    create_or_get_order_sync,
    create_order_and_send_to_detrack,
    retry_failed_detrack_sync,
)


Base.metadata.create_all(bind=engine)

app = FastAPI(title=settings.app_name)


@app.get("/health")
def health_check():
    return {
        "status": "ok",
        "service": settings.app_name,
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
    db: Session = Depends(get_db),
):
    result = retry_failed_detrack_sync(db, order_sync_id)
    return result


@app.post("/connectors/shopify/test")
def test_shopify_connector(
    db: Session = Depends(get_db),
):
    order = mock_shopify_order_to_standard()
    result = create_order_and_send_to_detrack(db, order)
    return result


@app.post("/connectors/shopee/test")
def test_shopee_connector(
    db: Session = Depends(get_db),
):
    order = mock_shopee_order_to_standard()
    result = create_order_and_send_to_detrack(db, order)
    return result


@app.post("/connectors/tiktok-shop/test")
def test_tiktok_shop_connector(
    db: Session = Depends(get_db),
):
    order = mock_tiktok_shop_order_to_standard()
    result = create_order_and_send_to_detrack(db, order)
    return result
