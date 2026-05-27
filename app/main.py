from fastapi import Depends, FastAPI
from sqlalchemy.orm import Session

from app.config import settings
from app.database import Base, engine, get_db
from app.schemas import StandardOrder
from app.sync_service import create_or_get_order_sync


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