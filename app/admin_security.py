from fastapi import Header, HTTPException, Query

from app.config import settings


def require_admin_key(
    x_admin_key: str | None = Header(default=None),
    key: str | None = Query(default=None),
) -> bool:
    if not settings.admin_api_key or settings.admin_api_key == "put_later":
        raise HTTPException(
            status_code=500,
            detail="ADMIN_API_KEY is not configured.",
        )

    provided_key = x_admin_key or key

    if provided_key != settings.admin_api_key:
        raise HTTPException(
            status_code=401,
            detail="Invalid admin key.",
        )

    return True