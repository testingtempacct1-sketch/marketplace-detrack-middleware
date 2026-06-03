"""
printnode_client.py
Sends PDF labels to PrintNode API for automatic printing
on the Brother QL-1110NWB printer.
"""
import base64
import logging

import requests

from app.config import settings

logger = logging.getLogger(__name__)

PRINTNODE_API_URL = "https://api.printnode.com"


class PrintNodeError(Exception):
    pass


def _is_configured() -> bool:
    """Check if PrintNode is configured."""
    return bool(settings.printnode_api_key and settings.printnode_printer_id)


def get_printer_info() -> dict | None:
    """
    Fetch printer info from PrintNode.
    Returns printer dict or None if not found.
    """
    if not _is_configured():
        return None

    try:
        response = requests.get(
            f"{PRINTNODE_API_URL}/printers/{settings.printnode_printer_id}",
            auth=(settings.printnode_api_key, ""),
            timeout=10,
        )

        if response.status_code == 404:
            return None

        if response.status_code >= 400:
            raise PrintNodeError(
                f"PrintNode error {response.status_code}: {response.text[:200]}"
            )

        return response.json()

    except PrintNodeError:
        raise
    except Exception as exc:
        raise PrintNodeError(f"PrintNode connection failed: {exc}") from exc


def print_label(pdf_bytes: bytes, title: str = "Shipping Label") -> dict:
    """
    Send a PDF label to PrintNode for printing.
    Returns the print job response dict.
    Never raises — printing failures should not break order creation.
    """
    if not _is_configured():
        logger.warning(
            "[PrintNode] Not configured — skipping print. "
            "Set PRINTNODE_API_KEY and PRINTNODE_PRINTER_ID in .env"
        )
        return {"printed": False, "reason": "PrintNode not configured"}

    try:
        # Encode PDF as base64
        pdf_base64 = base64.b64encode(pdf_bytes).decode("utf-8")

        payload = {
            "printerId": int(settings.printnode_printer_id),
            "title": title,
            "contentType": "pdf_base64",
            "content": pdf_base64,
            "source": "Zen Zu Fu Middleware",
        }

        response = requests.post(
            f"{PRINTNODE_API_URL}/printjobs",
            auth=(settings.printnode_api_key, ""),
            json=payload,
            timeout=15,
        )

        if response.status_code >= 400:
            error_msg = f"PrintNode error {response.status_code}: {response.text[:200]}"
            logger.error(f"[PrintNode] {error_msg}")
            return {"printed": False, "reason": error_msg}

        job_id = response.json()
        logger.info(f"[PrintNode] Print job created: {job_id} — {title}")

        return {
            "printed": True,
            "job_id": job_id,
            "title": title,
            "printer_id": settings.printnode_printer_id,
        }

    except Exception as exc:
        logger.error(f"[PrintNode] Exception: {exc}")
        return {"printed": False, "reason": str(exc)}


def print_shipping_label(order_sync) -> dict:
    """
    Generate and print a shipping label for an OrderSync record.
    This is the main function called after a Detrack job is created.
    """
    from app.label_generator import generate_label_pdf
    import json

    try:
        # Parse items from JSON
        items = []
        if order_sync.items_json:
            try:
                raw_items = json.loads(order_sync.items_json)
                items = [
                    {
                        "description": item.get("name") or "Item",
                        "quantity": item.get("quantity") or 1,
                    }
                    for item in raw_items
                ]
            except Exception:
                items = [{"description": "Order items", "quantity": 1}]

        # Generate PDF label
        pdf_bytes = generate_label_pdf(
            do_number=order_sync.detrack_do_number or "",
            source=order_sync.source or "shopify",
            customer_name=order_sync.customer_name or "",
            phone=order_sync.phone or "",
            address=order_sync.address or "",
            postal_code=order_sync.postal_code,
            items=items,
            remarks=order_sync.remarks,
            delivery_date=order_sync.delivery_date,
        )

        # Send to printer
        title = f"Label — {order_sync.detrack_do_number or order_sync.source_order_id}"
        result = print_label(pdf_bytes, title=title)

        if result.get("printed"):
            logger.info(
                f"[PrintNode] Label printed for order {order_sync.id} "
                f"— DO: {order_sync.detrack_do_number}"
            )
        else:
            logger.warning(
                f"[PrintNode] Label not printed for order {order_sync.id}: "
                f"{result.get('reason')}"
            )

        return result

    except Exception as exc:
        logger.error(f"[PrintNode] Failed to generate/print label for order {order_sync.id}: {exc}")
        return {"printed": False, "reason": str(exc)}
