import time

import requests

from app.config import settings


class ShopifyAdminAPIError(Exception):
    pass


_token_cache: dict[str, object] = {
    "access_token": None,
    "expires_at": 0,
}


def _clean_store_domain() -> str:
    if not settings.shopify_store_domain:
        raise ShopifyAdminAPIError("SHOPIFY_STORE_DOMAIN is not configured.")

    return (
        settings.shopify_store_domain.strip()
        .replace("https://", "")
        .replace("http://", "")
        .rstrip("/")
    )


def _shopify_graphql_url() -> str:
    return f"https://{_clean_store_domain()}/admin/api/2026-04/graphql.json"


def _client_credentials_token_url() -> str:
    return f"https://{_clean_store_domain()}/admin/oauth/access_token"


def _get_cached_or_new_access_token() -> str:
    existing_token = _token_cache.get("access_token")
    expires_at = int(_token_cache.get("expires_at") or 0)

    if existing_token and time.time() < expires_at - 120:
        return str(existing_token)

    if not settings.shopify_client_id:
        raise ShopifyAdminAPIError("SHOPIFY_CLIENT_ID is not configured.")

    if not settings.shopify_client_secret:
        raise ShopifyAdminAPIError("SHOPIFY_CLIENT_SECRET is not configured.")

    response = requests.post(
        _client_credentials_token_url(),
        data={
            "grant_type": "client_credentials",
            "client_id": settings.shopify_client_id,
            "client_secret": settings.shopify_client_secret,
        },
        timeout=30,
    )

    try:
        payload = response.json()
    except ValueError as exc:
        raise ShopifyAdminAPIError(
            f"Shopify token endpoint returned non-JSON response "
            f"{response.status_code}: {response.text[:500]}"
        ) from exc

    if response.status_code >= 400:
        raise ShopifyAdminAPIError(
            f"Shopify token endpoint error {response.status_code}: {payload}"
        )

    access_token = payload.get("access_token")
    if not access_token:
        raise ShopifyAdminAPIError(
            f"Shopify token endpoint did not return access_token: {payload}"
        )

    expires_in = int(payload.get("expires_in") or 86400)

    _token_cache["access_token"] = access_token
    _token_cache["expires_at"] = int(time.time()) + expires_in

    return str(access_token)


def shopify_graphql(query: str, variables: dict | None = None) -> dict:
    access_token = _get_cached_or_new_access_token()

    response = requests.post(
        _shopify_graphql_url(),
        headers={
            "Content-Type": "application/json",
            "X-Shopify-Access-Token": access_token,
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
            f"Shopify API returned non-JSON response "
            f"{response.status_code}: {response.text[:500]}"
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
      }
    }
    """
def get_shopify_fulfilment_plan(shopify_order_id: str) -> dict:
    gid = f"gid://shopify/Order/{shopify_order_id}"

    query = """
    query GetFulfilmentPlan($id: ID!) {
      order(id: $id) {
        id
        name
        displayFulfillmentStatus
        displayFinancialStatus
        fulfillmentOrders(first: 10) {
          edges {
            node {
              id
              status
              requestStatus
              supportedActions {
                action
              }
              lineItems(first: 20) {
                edges {
                  node {
                    id
                    totalQuantity
                    remainingQuantity
                    lineItem {
                      name
                      sku
                    }
                  }
                }
              }
            }
          }
        }
      }
    }
    """

    payload = shopify_graphql(query, {"id": gid})
    order = payload.get("data", {}).get("order")

    if not order:
        raise ShopifyAdminAPIError(f"Shopify order not found: {shopify_order_id}")

    fulfilment_orders = []
    can_fulfil = False

    for edge in order.get("fulfillmentOrders", {}).get("edges", []):
        node = edge.get("node") or {}

        supported_actions = [
            item.get("action")
            for item in node.get("supportedActions", [])
            if item.get("action")
        ]

        line_items = []
        for line_edge in node.get("lineItems", {}).get("edges", []):
            line_node = line_edge.get("node") or {}
            shopify_line_item = line_node.get("lineItem") or {}

            line_items.append(
                {
                    "id": line_node.get("id"),
                    "name": shopify_line_item.get("name"),
                    "sku": shopify_line_item.get("sku"),
                    "total_quantity": line_node.get("totalQuantity"),
                    "remaining_quantity": line_node.get("remainingQuantity"),
                }
            )

        if "CREATE_FULFILLMENT" in supported_actions:
            can_fulfil = True

        fulfilment_orders.append(
            {
                "id": node.get("id"),
                "status": node.get("status"),
                "request_status": node.get("requestStatus"),
                "supported_actions": supported_actions,
                "line_items": line_items,
            }
        )

    return {
        "shopify_order_id": shopify_order_id,
        "order_id": order.get("id"),
        "order_name": order.get("name"),
        "display_fulfillment_status": order.get("displayFulfillmentStatus"),
        "display_financial_status": order.get("displayFinancialStatus"),
        "can_fulfil": can_fulfil,
        "dry_run": settings.shopify_fulfilment_dry_run,
        "fulfilment_allowed": settings.shopify_fulfilment_allowed,
        "fulfilment_orders": fulfilment_orders,
    }


    payload = shopify_graphql(query, {"id": gid})
    order = payload.get("data", {}).get("order")

    if not order:
        raise ShopifyAdminAPIError(f"Shopify order not found: {shopify_order_id}")

    return order
