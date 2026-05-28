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

    payload = shopify_graphql(query, {"id": gid})
    order = payload.get("data", {}).get("order")

    if not order:
        raise ShopifyAdminAPIError(f"Shopify order not found: {shopify_order_id}")

    return order


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


def build_shopify_fulfilment_dry_run(
    shopify_order_id: str,
    tracking_number: str | None = None,
    tracking_url: str | None = None,
    notify_customer: bool = False,
) -> dict:
    plan = get_shopify_fulfilment_plan(shopify_order_id)

    safety_checks = {
        "is_paid": plan.get("display_financial_status") == "PAID",
        "is_unfulfilled": plan.get("display_fulfillment_status") == "UNFULFILLED",
        "can_fulfil": bool(plan.get("can_fulfil")),
    }

    safety_passed = all(safety_checks.values())

    if not safety_passed:
        return {
            "shopify_order_id": shopify_order_id,
            "order_name": plan.get("order_name"),
            "can_fulfil": False,
            "dry_run": settings.shopify_fulfilment_dry_run,
            "fulfilment_allowed": settings.shopify_fulfilment_allowed,
            "would_call_shopify": False,
            "reason": "Shopify fulfilment safety checks failed.",
            "safety_checks": safety_checks,
            "plan": plan,
        }


    line_items_by_fulfillment_order = []

    for fulfilment_order in plan["fulfilment_orders"]:
        supported_actions = fulfilment_order.get("supported_actions", [])

        if "CREATE_FULFILLMENT" not in supported_actions:
            continue

        fulfilment_order_line_items = []

        for line_item in fulfilment_order.get("line_items", []):
            remaining_quantity = line_item.get("remaining_quantity") or 0

            if remaining_quantity <= 0:
                continue

            fulfilment_order_line_items.append(
                {
                    "id": line_item["id"],
                    "quantity": remaining_quantity,
                }
            )

        if fulfilment_order_line_items:
            line_items_by_fulfillment_order.append(
                {
                    "fulfillmentOrderId": fulfilment_order["id"],
                    "fulfillmentOrderLineItems": fulfilment_order_line_items,
                }
            )

    fulfillment_input = {
        "lineItemsByFulfillmentOrder": line_items_by_fulfillment_order,
        "notifyCustomer": notify_customer,
    }

    tracking_info = {}

    if tracking_number:
        tracking_info["number"] = tracking_number

    if tracking_url:
        tracking_info["url"] = tracking_url

    if tracking_info:
        tracking_info["company"] = "Detrack"
        fulfillment_input["trackingInfo"] = tracking_info

        return {
        "shopify_order_id": shopify_order_id,
        "order_name": plan["order_name"],
        "can_fulfil": bool(line_items_by_fulfillment_order),
        "safety_checks": safety_checks,
        "safety_passed": safety_passed,

        "dry_run": settings.shopify_fulfilment_dry_run,
        "fulfilment_allowed": settings.shopify_fulfilment_allowed,
        "would_call_shopify": False,
        "mutation_name": "fulfillmentCreate",
        "message": "Dry run only. No Shopify fulfilment was created.",
        "variables": {
            "fulfillment": fulfillment_input,
            "message": "Fulfilled from Detrack delivery status update.",
        },
        "plan": plan,
    }


def create_shopify_fulfilment(
    shopify_order_id: str,
    tracking_number: str | None = None,
    tracking_url: str | None = None,
    notify_customer: bool = False,
) -> dict:
    dry_run_result = build_shopify_fulfilment_dry_run(
        shopify_order_id=shopify_order_id,
        tracking_number=tracking_number,
        tracking_url=tracking_url,
        notify_customer=notify_customer,
    )

    if settings.shopify_fulfilment_dry_run:
        return {
            **dry_run_result,
            "created": False,
            "would_call_shopify": False,
            "blocked_by": "SHOPIFY_FULFILMENT_DRY_RUN=true",
            "message": "Dry run is enabled. No Shopify fulfilment was created.",
        }

    if not settings.shopify_fulfilment_allowed:
        return {
            **dry_run_result,
            "created": False,
            "would_call_shopify": False,
            "blocked_by": "SHOPIFY_FULFILMENT_ALLOWED=false",
            "message": "Shopify fulfilment is not allowed. No Shopify fulfilment was created.",
        }

    if not dry_run_result["can_fulfil"]:
        return {
            **dry_run_result,
            "created": False,
            "would_call_shopify": False,
            "blocked_by": "can_fulfil=false",
            "message": "Shopify order cannot currently be fulfilled.",
        }

    mutation = """
    mutation CreateFulfillment($fulfillment: FulfillmentInput!, $message: String) {
      fulfillmentCreate(fulfillment: $fulfillment, message: $message) {
        fulfillment {
          id
          status
          trackingInfo {
            company
            number
            url
          }
        }
        userErrors {
          field
          message
        }
      }
    }
    """

    payload = shopify_graphql(mutation, dry_run_result["variables"])
    result = payload.get("data", {}).get("fulfillmentCreate") or {}
    user_errors = result.get("userErrors") or []

    if user_errors:
        raise ShopifyAdminAPIError(f"Shopify fulfilment user errors: {user_errors}")

    return {
        **dry_run_result,
        "created": True,
        "would_call_shopify": True,
        "message": "Shopify fulfilment was created.",
        "shopify_response": result,
    }

