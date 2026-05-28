from app.schemas import StandardOrder, StandardOrderItem


def detect_shopify_sales_channel(payload: dict) -> tuple[str, str]:
    """
    Returns:
        (internal_source, display_name)
    """

    source_name = str(payload.get("source_name") or "").lower()
    source_identifier = str(payload.get("source_identifier") or "").lower()
    source_url = str(payload.get("source_url") or "").lower()
    referring_site = str(payload.get("referring_site") or "").lower()
    landing_site = str(payload.get("landing_site") or "").lower()
    tags = str(payload.get("tags") or "").lower()

    app_info = payload.get("app") or {}
    app_name = str(app_info.get("name") or "").lower()

    note_attributes = payload.get("note_attributes") or []
    note_attribute_text = " ".join(
        [
            f"{item.get('name', '')} {item.get('value', '')}"
            for item in note_attributes
            if isinstance(item, dict)
        ]
    ).lower()

    combined_text = " ".join(
        [
            source_name,
            source_identifier,
            source_url,
            referring_site,
            landing_site,
            tags,
            app_name,
            note_attribute_text,
        ]
    )

    if "tiktok" in combined_text or "tik tok" in combined_text or "tik-tok" in combined_text:
        return "shopify_tiktok", "TikTok Shop via Shopify"

    if source_name in {"web", "online_store"}:
        return "shopify_online_store", "Shopify Online Store"

    if source_name == "pos":
        return "shopify_pos", "Shopify POS"

    if "draft" in source_name:
        return "shopify_draft_order", "Shopify Draft Order"

    return "shopify", "Shopify"


def mock_shopify_order_to_standard() -> StandardOrder:
    return StandardOrder(
        source="shopify_online_store",
        source_order_id="SHOPIFY-TEST-001",
        source_order_name=order_name,
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
        remarks="Sales channel: Shopify Online Store. Created from mock Shopify connector.",
        delivery_date=None,
    )


def shopify_order_to_standard(payload: dict) -> StandardOrder:
    internal_source, channel_display_name = detect_shopify_sales_channel(payload)

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
    source_name = payload.get("source_name") or "unknown"

    remarks = (
        f"Sales channel: {channel_display_name}. "
        f"Shopify order {order_name}. "
        f"Shopify source_name: {source_name}. "
        f"{note}"
    ).strip()

    return StandardOrder(
        source=internal_source,
        source_order_id=order_id,
        customer_name=customer_name,
        phone=phone,
        address=address,
        postal_code=postal_code,
        items=items,
        remarks=remarks,
        delivery_date=None,
    )
