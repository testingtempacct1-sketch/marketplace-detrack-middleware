import httpx

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
            timeout=30,
        )
    except httpx.RequestError as exc:
        raise DetrackAPIError(f"Unable to connect to Detrack: {exc}") from exc

    if response.status_code >= 400:
        raise DetrackAPIError(
            f"Detrack API error {response.status_code}: {response.text}"
        )

    return response.json()
