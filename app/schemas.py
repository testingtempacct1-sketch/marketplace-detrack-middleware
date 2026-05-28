from typing import List, Optional

from pydantic import BaseModel, Field


class StandardOrderItem(BaseModel):
    name: str
    quantity: int = 1
    sku: Optional[str] = None


class StandardOrder(BaseModel):
    source: str
    source_order_id: str
    source_order_name: str | None = None
    customer_name: str
    phone: str
    address: str
    postal_code: str | None = None
    items: list[StandardOrderItem]
    remarks: str | None = None
    delivery_date: str | None = None
