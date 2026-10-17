import json
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from app import create_app
from app.services.scheduler import AutoRefreshScheduler, BERLIN_TZ


def test_scheduler_status_persistence():
    with tempfile.TemporaryDirectory() as tmp_dir:
        class FakeApp:
            config = {"CACHE_DIR": tmp_dir, "DEFAULT_ZIP_CODE": "14195"}

        scheduler = AutoRefreshScheduler(FakeApp())
        status = scheduler.get_status()
        assert status.get("status") == "never_run"

        test_data = {"last_run": "2026-10-17T03:00:00+02:00", "status": "success", "offers_count": 100}
        scheduler._save_status(test_data)

        loaded = scheduler.get_status()
        assert loaded["status"] == "success"
        assert loaded["offers_count"] == 100


def test_scheduler_should_refresh_logic():
    with tempfile.TemporaryDirectory() as tmp_dir:
        class FakeApp:
            config = {"CACHE_DIR": tmp_dir, "DEFAULT_ZIP_CODE": "14195"}

        scheduler = AutoRefreshScheduler(FakeApp(), max_cache_age_hours=24)

        # 1. No status file and no cache -> should refresh
        assert scheduler.should_refresh() is True

        # 2. Status saved recently (1 hour ago) -> should not refresh
        now = datetime.now(BERLIN_TZ)
        recent = (now - timedelta(hours=1)).isoformat()
        scheduler._save_status({"last_run": recent, "status": "success"})
        assert scheduler.should_refresh() is False

        # 3. Status saved 25 hours ago -> should refresh
        old = (now - timedelta(hours=25)).isoformat()
        scheduler._save_status({"last_run": old, "status": "success"})
        assert scheduler.should_refresh() is True


def test_scheduler_api_endpoints():
    app = create_app()
    client = app.test_client()

    res = client.get("/api/scheduler/status")
    assert res.status_code == 200
    data = res.get_json()
    assert "status" in data
    assert "scheduler_running" in data

