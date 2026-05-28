import requests

from app.config import settings


class ShopifyAdminAPIError(Exception):
    pass


def _shopify_graphql_url() -> str:
    if not settings.shopify_store_domain:
        raise ShopifyAdminAPIError("SHOPIFY_STORE_DOMAIN is not configured.")

    store_domain = settings.shopify_store_domain.strip().replace("https://", "").replace("http://", "")
    return f"https://{store_domain}/admin/api/2026-04/graphql.json"


def shopify_graphql(query: str, variables: dict | None = None) -> dict:
    if not settings.shopify_admin_access_token:
        raise ShopifyAdminAPIError("SHOPIFY_ADMIN_ACCESS_TOKEN is not configured.")

    response = requests.post(
        _shopify_graphql_url(),
        headers={
            "Content-Type": "application/json",
            "X-Shopify-Access-Token": settings.shopify_admin_access_token,
        },
        json={
            "query": query,
            "variables": variables or {},
        },
        timeout=30,
    )

    try:
        payload = response.json()
    except ValueError as exc:
        raise ShopifyAdminAPIError(
            f"Shopify API returned non-JSON response {response.status_code}: {response.text[:500]}"
        ) from exc

    if response.status_code >= 400:
        raise ShopifyAdminAPIError(
            f"Shopify API error {response.status_code}: {payload}"
        )

    if payload.get("errors"):
        raise ShopifyAdminAPIError(
            f"Shopify GraphQL errors: {payload['errors']}"
        )

    return payload


def get_shopify_order_by_id(shopify_order_id: str) -> dict:
    gid = f"gid://shopify/Order/{shopify_order_id}"

    query = """
    query GetOrder($id: ID!) {
      order(id: $id) {
        id
        name
        displayFulfillmentStatus
        displayFinancialStatus
        createdAt
        legacyResourceId
        customer {
          displayName
        }
      }
    }
    """

    payload = shopify_graphql(query, {"id": gid})
    order = payload.get("data", {}).get("order")

    if not order:
        raise ShopifyAdminAPIError(f"Shopify order not found: {shopify_order_id}")

    return order
