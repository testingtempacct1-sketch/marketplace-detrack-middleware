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
    logger.info("[Scheduler] Started — retry job every 5min, daily summary at 9:00 AM SGT.")


def stop_scheduler() -> None:
    global _scheduler

    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
        logger.info("[Scheduler] Stopped.")
