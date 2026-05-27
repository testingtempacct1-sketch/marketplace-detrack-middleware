from app.schemas import StandardOrder, StandardOrderItem


def mock_shopify_order_to_standard() -> StandardOrder:
    return StandardOrder(
        source="shopify",
        source_order_id="SHOPIFY-TEST-001",
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
