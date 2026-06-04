import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.sync_service import auto_retry_failed_detrack_jobs

logger = logging.getLogger(__name__)

SGT = ZoneInfo("Asia/Singapore")

_scheduler: BackgroundScheduler | None = None


def _run_retry_job() -> None:
    """Background job that runs every 5 minutes to retry failed Detrack syncs."""
    db: Session = SessionLocal()
    try:
        results = auto_retry_failed_detrack_jobs(db)
        if results["attempted"] > 0:
            logger.info(
                f"[Scheduler] Retry run complete — "
                f"attempted={results['attempted']}, "
                f"succeeded={results['succeeded']}, "
                f"failed={results['failed']}, "
                f"permanent={results['permanent']}"
            )
    except Exception as exc:
        logger.error(f"[Scheduler] Retry job error: {exc}")
    finally:
        db.close()


def _run_daily_summary() -> None:
    """Background job that runs at 9am SGT to send a daily Telegram summary."""
    from app.models import OrderSync
    from app.telegram_client import send_daily_summary

    db: Session = SessionLocal()
    try:
        today_sgt = datetime.now(SGT).strftime("%Y-%m-%d")

        total = db.query(OrderSync).count()

        rows = (
            db.query(OrderSync.source, OrderSync.sync_status, func.count(OrderSync.id))
            .group_by(OrderSync.source, OrderSync.sync_status)
            .all()
        )

        by_source = {}
        for source, status, count in rows:
            source = source or "unknown"
            if source not in by_source:
                by_source[source] = {"total": 0}
            by_source[source][status] = count
            by_source[source]["total"] += count

        completed = (
            db.query(OrderSync)
            .filter(OrderSync.delivery_status == "completed")
            .count()
        )
        cancelled = (
            db.query(OrderSync)
            .filter(OrderSync.sync_status == "cancelled")
            .count()
        )
        failed = (
            db.query(OrderSync)
            .filter(OrderSync.sync_status == "detrack_failed")
            .count()
        )
        permanent = (
            db.query(OrderSync)
            .filter(OrderSync.sync_status == "detrack_failed_permanent")
            .count()
        )

        send_daily_summary(
            date_sgt=today_sgt,
            total_orders=total,
            by_source=by_source,
            completed=completed,
            cancelled=cancelled,
            failed=failed,
            permanent=permanent,
        )

        logger.info(f"[Scheduler] Daily summary sent for {today_sgt}.")

    except Exception as exc:
        logger.error(f"[Scheduler] Daily summary error: {exc}")
    finally:
        db.close()


def _run_print_retry_job() -> None:
    """
    Background job that runs every 5 minutes.
    Checks if printer is online, then retries failed label prints.
    """
    from app.printnode_client import get_printer_info, print_shipping_label
    from app.models import OrderSync

    db: Session = SessionLocal()
    try:
        # Check if PrintNode is configured
        from app.config import settings
        if not settings.printnode_api_key or not settings.printnode_printer_id:
            return

        # Check if printer is online
        printer = get_printer_info()
        if not printer:
            logger.debug("[Scheduler] Printer offline or not found — skipping print retry.")
            return

        # Check printer state
        printer_state = printer.get("computer", {}).get("state") if isinstance(printer, dict) else None
        if printer_state and printer_state != "connected":
            logger.debug(f"[Scheduler] Printer state: {printer_state} — skipping print retry.")
            return

        # Find orders with failed label prints
        failed_prints = (
            db.query(OrderSync)
            .filter(OrderSync.label_printed == "failed")
            .filter(OrderSync.sync_status == "sent_to_detrack")
            .all()
        )

        if not failed_prints:
            return

        logger.info(f"[Scheduler] Printer online — retrying {len(failed_prints)} failed label(s).")

        success = 0
        for order in failed_prints:
            result = print_shipping_label(order)
            if result.get("printed"):
                order.label_printed = "printed"
                order.label_print_error = None
                success += 1
                logger.info(f"[Scheduler] Label reprinted for order #{order.id} — {order.detrack_do_number}")
            else:
                order.label_printed = "failed"
                order.label_print_error = result.get("reason")

        db.commit()

        if success > 0:
            logger.info(f"[Scheduler] Print retry complete — {success}/{len(failed_prints)} succeeded.")

    except Exception as exc:
        logger.error(f"[Scheduler] Print retry job error: {exc}")
    finally:
        db.close()



    global _scheduler

    if _scheduler and _scheduler.running:
        return

    _scheduler = BackgroundScheduler(timezone="Asia/Singapore")

    _scheduler.add_job(
        _run_retry_job,
        trigger="interval",
        minutes=5,
        id="retry_failed_detrack_jobs",
        name="Retry failed Detrack jobs",
        replace_existing=True,
        next_run_time=datetime.now(),
    )

    _scheduler.add_job(
        _run_print_retry_job,
        trigger="interval",
        minutes=5,
        id="retry_failed_prints",
        name="Retry failed label prints",
        replace_existing=True,
    )

    _scheduler.add_job(
        _run_daily_summary,
        trigger="cron",
        hour=9,
        minute=0,
        timezone="Asia/Singapore",
        id="daily_summary",
        name="Daily Telegram summary",
        replace_existing=True,
    )

    _scheduler.start()
    logger.info("[Scheduler] Started — retry job every 5min, print retry every 5min, daily summary at 9:00 AM SGT.")


def stop_scheduler() -> None:
    global _scheduler

    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
        logger.info("[Scheduler] Stopped.")

def start_scheduler() -> None:
    global _scheduler

    if _scheduler and _scheduler.running:
        return

    _scheduler = BackgroundScheduler(timezone="Asia/Singapore")

    _scheduler.add_job(
        _run_retry_job,
        trigger="interval",
        minutes=5,
        id="retry_failed_detrack_jobs",
        name="Retry failed Detrack jobs",
        replace_existing=True,
        next_run_time=datetime.now(),
    )

    _scheduler.add_job(
        _run_print_retry_job,
        trigger="interval",
        minutes=5,
        id="retry_failed_prints",
        name="Retry failed label prints",
        replace_existing=True,
    )

    _scheduler.add_job(
        _run_daily_summary,
        trigger="cron",
        hour=9,
        minute=0,
        timezone="Asia/Singapore",
        id="daily_summary",
        name="Daily Telegram summary",
        replace_existing=True,
    )

    _scheduler.start()
    logger.info("[Scheduler] Started — retry every 5min, print retry every 5min, daily summary 9AM SGT.")
