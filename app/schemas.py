from typing import List, Optional

from pydantic import BaseModel, Field


class StandardOrderItem(BaseModel):
    name: str
    quantity: int = 1
    sku: Optional[str] = None


class StandardOrder(BaseModel):
    source: str = Field(..., examples=["shopify", "shopee", "tiktok_shop"])
    source_order_id: str

    customer_name: str
    phone: str
    address: str
    postal_code: Optional[str] = None

    items: List[StandardOrderItem]

    remarks: Optional[str] = None
    delivery_date: Optional[str] = None