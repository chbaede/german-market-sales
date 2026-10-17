from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

try:
    import fcntl
except ImportError:
    fcntl = None

LOGGER = logging.getLogger("market_scheduler")
BERLIN_TZ = ZoneInfo("Europe/Berlin")


class AutoRefreshScheduler:
    """
    Background scheduler embedded inside the Flask/Gunicorn container.
    Periodically refreshes supermarket and drugstore offers automatically,
    ensuring price history and weekly flyers are captured even without site visitors.
    Uses an exclusive file lock so only one worker acts as the scheduler leader.
    """

    def __init__(
        self,
        app,
        check_interval_seconds: int = 900,
        max_cache_age_hours: int = 24,
    ):
        self.app = app
        self.check_interval_seconds = check_interval_seconds
        self.max_cache_age_hours = max_cache_age_hours
        self.cache_dir = Path(app.config["CACHE_DIR"])
        self.status_file = self.cache_dir / "scheduler_status.json"
        self.lock_file = self.cache_dir / ".scheduler.lock"
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock_fd: int | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop,
            daemon=True,
            name="auto-refresh-scheduler",
        )
        self._thread.start()
        LOGGER.info("AutoRefreshScheduler thread started.")

    def stop(self) -> None:
        self._stop_event.set()
        if self._lock_fd is not None:
            try:
                if fcntl:
                    fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
                os.close(self._lock_fd)
            except Exception:
                pass
            self._lock_fd = None

    def _acquire_leader_lock(self) -> bool:
        if not fcntl:
            return True
        try:
            self.lock_file.parent.mkdir(parents=True, exist_ok=True)
            self._lock_fd = os.open(str(self.lock_file), os.O_CREAT | os.O_RDWR)
            fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except (BlockingIOError, OSError):
            return False

    def _run_loop(self) -> None:
        # Give web server a few seconds to finish initialization
        time.sleep(4)

        # Try acquiring exclusive worker lock
        is_leader = self._acquire_leader_lock()
        if not is_leader:
            LOGGER.info("Worker is not the scheduler leader. Skipping background scheduler in this process.")
            return

        LOGGER.info("Acquired scheduler leader lock. AutoRefreshScheduler active.")

        while not self._stop_event.is_set():
            try:
                if self.should_refresh():
                    LOGGER.info("Triggering scheduled background offer refresh...")
                    self.execute_refresh()
            except Exception as e:
                LOGGER.exception("Error in scheduler loop: %s", e)

            # Sleep check_interval_seconds or wake on stop
            self._stop_event.wait(self.check_interval_seconds)

    def should_refresh(self) -> bool:
        status = self.get_status()
        last_run_str = status.get("last_run")
        now = datetime.now(BERLIN_TZ)

        if not last_run_str:
            supermarket_cache = self.cache_dir / "offers.json"
            if not supermarket_cache.exists():
                return True
            try:
                mtime = datetime.fromtimestamp(supermarket_cache.stat().st_mtime, tz=BERLIN_TZ)
                age_hours = (now - mtime).total_seconds() / 3600
                return age_hours >= self.max_cache_age_hours
            except Exception:
                return True

        try:
            last_run = datetime.fromisoformat(last_run_str)
            if last_run.tzinfo is None:
                last_run = last_run.replace(tzinfo=BERLIN_TZ)
        except Exception:
            return True

        age_hours = (now - last_run).total_seconds() / 3600

        # Monday morning deal refresh: between 03:00 and 07:00 Berlin time
        is_monday = now.weekday() == 0
        in_monday_window = is_monday and (3 <= now.hour <= 7)
        ran_today = (last_run.date() == now.date())

        if in_monday_window and not ran_today:
            return True

        # Standard periodic threshold (e.g. 24 hours)
        if age_hours >= self.max_cache_age_hours:
            return True

        return False

    def execute_refresh(self) -> dict:
        now_str = datetime.now(BERLIN_TZ).isoformat(timespec="seconds")
        from app.routes import offer_service, drugstore_offer_service

        with self.app.app_context():
            zip_code = self.app.config.get("DEFAULT_ZIP_CODE", "14195")
            sm_service = offer_service()
            dg_service = drugstore_offer_service()

            LOGGER.info("Scheduled refresh starting for supermarkets (ZIP: %s)...", zip_code)
            sm_res = sm_service.get_offers(zip_code=zip_code, refresh=True)

            LOGGER.info("Scheduled refresh starting for drugstores (ZIP: %s)...", zip_code)
            dg_res = dg_service.get_offers(zip_code=zip_code, refresh=True)

            result_info = {
                "last_run": now_str,
                "status": "success",
                "zip_code": zip_code,
                "supermarket_offers_count": len(sm_res.offers),
                "drugstore_offers_count": len(dg_res.offers),
                "warnings": sm_res.warnings + dg_res.warnings,
            }
            self._save_status(result_info)
            LOGGER.info(
                "Scheduled refresh finished successfully: %d supermarket deals, %d drugstore deals.",
                len(sm_res.offers),
                len(dg_res.offers),
            )
            return result_info

    def get_status(self) -> dict:
        if not self.status_file.exists():
            return {"status": "never_run", "last_run": None}
        try:
            with self.status_file.open("r", encoding="utf-8") as f:
                data = json.load(f)
                return data
        except Exception:
            return {"status": "unknown", "last_run": None}

    def _save_status(self, status: dict) -> None:
        try:
            self.status_file.parent.mkdir(parents=True, exist_ok=True)
            with self.status_file.open("w", encoding="utf-8") as f:
                json.dump(status, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

