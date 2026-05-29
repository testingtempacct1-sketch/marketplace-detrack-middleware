import requests

from app.config import settings


class DetrackAPIError(Exception):
    pass


def create_detrack_job(payload: dict) -> dict:
    if not settings.detrack_api_key or settings.detrack_api_key == "put_later":
        raise DetrackAPIError("DETRACK_API_KEY is missing in .env")

    url = settings.detrack_base_url

    headers = {
        "X-API-KEY": settings.detrack_api_key,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    try:
        response = httpx.post(
            url,
            headers=headers,
            json=payload,
            timeout=8,
        )
    except httpx.RequestError as exc:
        raise DetrackAPIError(f"Unable to connect to Detrack: {exc}") from exc

    if response.status_code >= 400:
        raise DetrackAPIError(
            f"Detrack API error {response.status_code}: {response.text}"
        )

    return response.json()

    
def update_detrack_job_as_cancelled(do_number: str) -> dict:
    payload = {
        "data": {
            "do_number": do_number,
            "status": "on_hold",
            "tracking_status": "on_hold",
            "reason": "Shopify/TikTok order cancelled",
            "instructions": "CANCELLED FROM SHOPIFY — DO NOT DELIVER",
            "note": "CANCELLED FROM SHOPIFY — DO NOT DELIVER",
        }
    }

    response = requests.put(
        f"{settings.detrack_base_url}/{do_number}",
        headers={
            "Content-Type": "application/json",
            "X-API-Key": settings.detrack_api_key,
        },
        json=payload,
        timeout=8,
    )

    try:
        result = response.json()
    except ValueError as exc:
        raise DetrackAPIError(
            f"Detrack returned non-JSON response {response.status_code}: {response.text[:500]}"
        ) from exc

    if response.status_code >= 400:
        raise DetrackAPIError(
            f"Detrack update failed {response.status_code}: {result}"
        )

    return result


