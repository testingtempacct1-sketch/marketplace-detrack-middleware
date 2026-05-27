from app.schemas import StandardOrder, StandardOrderItem


def mock_tiktok_shop_order_to_standard() -> StandardOrder:
    return StandardOrder(
        source="tiktok_shop",
        source_order_id="TIKTOK-TEST-001",
        customer_name="TikTok Customer",
        phone="93456789",
        address="30 Jurong East Street 21, Singapore",
        postal_code="609601",
        items=[
            StandardOrderItem(
                name="TikTok Test Skirt",
                quantity=1,
                sku="TT-SKIRT-001",
            )
        ],
        remarks="Created from mock TikTok Shop connector.",
        delivery_date=None,
    )
