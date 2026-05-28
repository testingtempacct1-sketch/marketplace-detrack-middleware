from datetime import datetime

from sqlalchemy import Column, DateTime, Integer, String, Text, UniqueConstraint

from app.database import Base


class OrderSync(Base):
    __tablename__ = "order_sync"

    id = Column(Integer, primary_key=True, index=True)

    source = Column(String(50), nullable=False)
    source_order_id = Column(String(100), nullable=False)
    shopify_order_id = Column(String(100), nullable=True)
    shopify_order_name = Column(String(100), nullable=True)
    shopify_order_admin_url = Column(String(255), nullable=True)


    customer_name = Column(String(255), nullable=True)
    phone = Column(String(50), nullable=True)
    address = Column(Text, nullable=True)
    postal_code = Column(String(20), nullable=True)

    items_json = Column(Text, nullable=True)
    remarks = Column(Text, nullable=True)
    delivery_date = Column(String(20), nullable=True)

    detrack_job_id = Column(String(100), nullable=True)
    detrack_do_number = Column(String(100), nullable=True)

    sync_status = Column(String(50), nullable=False, default="pending")
    delivery_status = Column(String(100), nullable=True)
    error_message = Column(Text, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("source", "source_order_id", name="uq_source_order"),
    )
