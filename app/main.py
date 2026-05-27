from fastapi import Depends, FastAPI
from sqlalchemy.orm import Session

from app.config import settings
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
