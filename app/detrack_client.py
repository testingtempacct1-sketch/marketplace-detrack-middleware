import requests

from app.config import settings


class DetrackAPIError(Exception):
    pass


def create_detrack_job(payload: dict) -> dict:
    response = requests.post(
        settings.detrack_base_url,
        headers={
            "Content-Type": "application/json",
            "X-API-Key": settings.detrack_api_key,
        },
        json=payload,
        timeout=30,
    )

    try:
        result = response.json()
    except ValueError as exc:
        raise DetrackAPIError(
            f"Detrack returned non-JSON response {response.status_code}: {response.text[:500]}"
        ) from exc

    if response.status_code >= 400:
        raise DetrackAPIError(
            f"Detrack create failed {response.status_code}: {result}"
        )

    return result


def update_detrack_job_as_cancelled(job_id: str, do_number: str | None = None) -> dict:
    payload = {
        "data": {
            "status": "on_hold",
            "tracking_status": "on_hold",
            "reason": "Shopify/TikTok order cancelled",
            "instructions": "CANCELLED FROM SHOPIFY - DO NOT DELIVER",
            "note": "CANCELLED FROM SHOPIFY - DO NOT DELIVER",
        }
    }

    if do_number:
        payload["data"]["do_number"] = do_number

    response = requests.put(
        f"{settings.detrack_base_url}/{job_id}",
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
