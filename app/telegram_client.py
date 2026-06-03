import logging

import requests

from app.config import settings

logger = logging.getLogger(__name__)

TELEGRAM_API_URL = "https://api.telegram.org/bot{token}/sendMessage"


def send_telegram_message(message: str) -> bool:
    """
    Send a message to the configured Telegram chat.
    Returns True if sent successfully, False otherwise.
    Never raises — alerts should not break the main flow.
    """
    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        logger.warning("[Telegram] Bot token or chat ID not configured. Skipping alert.")
        return False

    try:
        response = requests.post(
            TELEGRAM_API_URL.format(token=settings.telegram_bot_token),
            json={
                "chat_id": settings.telegram_chat_id,
                "text": message,
                "parse_mode": "HTML",
            },
            timeout=10,
        )

        if response.status_code == 200:
            logger.info("[Telegram] Alert sent successfully.")
            return True
        else:
            logger.warning(
                f"[Telegram] Failed to send alert: {response.status_code} {response.text[:200]}"
            )
            return False

    except Exception as exc:
        logger.error(f"[Telegram] Exception while sending alert: {exc}")
        return False


def send_permanent_failure_alert(
    order_sync_id: int,
    source: str,
    source_order_id: str,
    customer_name: str | None,
    detrack_do_number: str | None,
    error_message: str | None,
    retry_count: int,
) -> bool:
    """Send a Telegram alert for a permanently failed Detrack sync."""
    source_label = (source or "unknown").upper()

    message = (
        f"🚨 <b>Detrack Sync Failed Permanently</b>\n\n"
        f"<b>Order ID:</b> #{order_sync_id}\n"
        f"<b>Source:</b> {source_label}\n"
        f"<b>Order Ref:</b> {source_order_id}\n"
        f"<b>Customer:</b> {customer_name or 'Unknown'}\n"
        f"<b>DO Number:</b> {detrack_do_number or 'N/A'}\n"
        f"<b>Retries:</b> {retry_count}\n"
        f"<b>Error:</b> {error_message or 'Unknown error'}\n\n"
        f"⚠️ Manual intervention required.\n"
        f"Check: /orders/failed"
    )

    return send_telegram_message(message)


def send_retry_success_alert(
    order_sync_id: int,
    source: str,
    source_order_id: str,
    customer_name: str | None,
    detrack_do_number: str | None,
    retry_count: int,
) -> bool:
    """Send a Telegram alert when a previously failed order syncs successfully."""
    source_label = (source or "unknown").upper()

    message = (
        f"✅ <b>Detrack Sync Recovered</b>\n\n"
        f"<b>Order ID:</b> #{order_sync_id}\n"
        f"<b>Source:</b> {source_label}\n"
        f"<b>Order Ref:</b> {source_order_id}\n"
        f"<b>Customer:</b> {customer_name or 'Unknown'}\n"
        f"<b>DO Number:</b> {detrack_do_number or 'N/A'}\n"
        f"<b>Recovered on attempt:</b> {retry_count + 1}"
    )

    return send_telegram_message(message)


def send_print_failure_alert(
    order_sync_id: int,
    source: str,
    source_order_id: str,
    detrack_do_number: str | None,
    error: str,
) -> bool:
    """Send a Telegram alert when a label fails to print."""
    source_label = (source or "unknown").upper()

    message = (
        f"🖨️ <b>Label Print Failed</b>\n\n"
        f"<b>Order ID:</b> #{order_sync_id}\n"
        f"<b>Source:</b> {source_label}\n"
        f"<b>Order Ref:</b> {source_order_id}\n"
        f"<b>DO Number:</b> {detrack_do_number or 'N/A'}\n"
        f"<b>Error:</b> {error}\n\n"
        f"⚠️ Please print the label manually."
    )

    return send_telegram_message(message)


def send_daily_summary(
    date_sgt: str,
    total_orders: int,
    by_source: dict,
    completed: int,
    cancelled: int,
    failed: int,
    permanent: int,
) -> bool:
    """Send a daily summary Telegram message at 9am SGT."""

    source_lines = ""
    for source, counts in by_source.items():
        source_label = source.upper().replace("_", " ")
        source_lines += f"  • {source_label}: {counts.get('total', 0)} orders\n"

    if not source_lines:
        source_lines = "  • No orders\n"

    status_emoji = "✅" if failed == 0 and permanent == 0 else "⚠️"

    message = (
        f"{status_emoji} <b>Daily Summary — {date_sgt}</b>\n\n"
        f"<b>Total Orders:</b> {total_orders}\n\n"
        f"<b>By Source:</b>\n{source_lines}\n"
        f"<b>Deliveries Completed:</b> {completed}\n"
        f"<b>Cancelled:</b> {cancelled}\n"
        f"<b>Failed (pending retry):</b> {failed}\n"
        f"<b>Permanently Failed:</b> {permanent}\n"
    )

    if permanent > 0:
        message += f"\n⚠️ {permanent} order(s) need manual intervention. Check /orders/failed"

    return send_telegram_message(message)
