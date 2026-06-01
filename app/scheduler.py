import logging
from datetime import datetime

from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.sync_service import auto_retry_failed_detrack_jobs

logger = logging.getLogger(__name__)

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
        next_run_time=datetime.now(),  # Run immediately on startup too
    )

    _scheduler.start()
    logger.info("[Scheduler] Started — retry job runs every 5 minutes.")


def stop_scheduler() -> None:
    global _scheduler

    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
        logger.info("[Scheduler] Stopped.")
