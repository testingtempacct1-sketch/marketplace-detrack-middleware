from fastapi import Header, HTTPException, Query

from app.config import settings


def require_detrack_webhook_key(
    key: str | None = Query(default=None),
    x_detrack_webhook_key: str | None = Header(default=None),
) -> bool:
    if not settings.detrack_webhook_secret or settings.detrack_webhook_secret == "put_later":
        raise HTTPException(
            status_code=500,
            detail="DETRACK_WEBHOOK_SECRET is not configured.",
        )

    provided_key = x_detrack_webhook_key or key

    if provided_key != settings.detrack_webhook_secret:
        raise HTTPException(
            status_code=401,
            detail="Invalid Detrack webhook key.",
        )

    return True
