from app.schemas import StandardOrder, StandardOrderItem


def mock_shopify_order_to_standard() -> StandardOrder:
    return StandardOrder(
        source="shopify",
        source_order_id="SHOPIFY-TEST-001",
        source_order_name="SHOPIFY-TEST-001",
        customer_name="Shopify Customer",
        phone="91234567",
        address="10 Orchard Road, Singapore",
        postal_code="238800",
        items=[
            StandardOrderItem(
                name="Shopify Test Dress",
                quantity=1,
                sku="SHOP-DRESS-001",
            )
        ],
        remarks="Created from mock Shopify connector.",
        delivery_date=None,
    )


def shopify_order_to_standard(payload: dict) -> StandardOrder:
    order_id = str(payload.get("id") or payload.get("order_number") or payload.get("name"))

    shipping_address = payload.get("shipping_address") or {}
    customer = payload.get("customer") or {}

    first_name = shipping_address.get("first_name") or customer.get("first_name") or ""
    last_name = shipping_address.get("last_name") or customer.get("last_name") or ""
    customer_name = f"{first_name} {last_name}".strip()

    if not customer_name:
        customer_name = payload.get("name") or "Shopify Customer"

    phone = (
        shipping_address.get("phone")
        or payload.get("phone")
        or customer.get("phone")
        or ""
    )

    address_parts = [
        shipping_address.get("address1"),
        shipping_address.get("address2"),
        shipping_address.get("city"),
        shipping_address.get("province"),
        shipping_address.get("country"),
    ]
    address = ", ".join([part for part in address_parts if part])

    postal_code = shipping_address.get("zip")

    items = []
    for line_item in payload.get("line_items") or []:
        items.append(
            StandardOrderItem(
                name=line_item.get("name") or "Shopify item",
                quantity=int(line_item.get("quantity") or 1),
                sku=line_item.get("sku"),
            )
        )

    if not items:
        items = [
            StandardOrderItem(
                name="Shopify order item",
                quantity=1,
                sku=None,
            )
        ]

    note = payload.get("note") or ""
    order_name = payload.get("name") or order_id

  return StandardOrder(
    source="shopify",
    source_order_id=order_id,
    source_order_name=order_name,  # ← add this line
    customer_name=customer_name,
    phone=phone,
    address=address,
    postal_code=postal_code,
    items=items,
    remarks=f"Shopify order {order_name}. {note}".strip(),
    delivery_date=None,
)