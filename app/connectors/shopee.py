from app.schemas import StandardOrder, StandardOrderItem


def mock_shopee_order_to_standard() -> StandardOrder:
    return StandardOrder(
        source="shopee",
        source_order_id="SHOPEE-TEST-001",
        customer_name="Shopee Customer",
        phone="92345678",
        address="20 Tampines Central, Singapore",
        postal_code="529538",
        items=[
            StandardOrderItem(
                name="Shopee Test Top",
                quantity=2,
                sku="SHP-TOP-001",
            )
        ],
        remarks="Created from mock Shopee connector.",
        delivery_date=None,
    )
