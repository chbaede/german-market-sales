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


def test_weekly_monday_only_schedule_and_price_history_purge():
    from pathlib import Path
    from app.services.price_history import PriceHistoryService
    from app.models import Offer

    with tempfile.TemporaryDirectory() as tmp_dir:
        class FakeApp:
            config = {"CACHE_DIR": tmp_dir, "DEFAULT_ZIP_CODE": "14195"}

        scheduler = AutoRefreshScheduler(FakeApp())
        assert scheduler.max_cache_age_hours == 168

        # Last run 2 days ago (not Monday or already ran within the week) -> should not refresh daily
        now = datetime.now(BERLIN_TZ)
        if now.weekday() != 0:
            two_days_ago = (now - timedelta(days=2)).isoformat()
            scheduler._save_status({"last_run": two_days_ago, "status": "success"})
            assert scheduler.should_refresh() is False

        # Test PriceHistoryService.monday_iso_for_timestamp and purge_non_monday_records
        ph = PriceHistoryService(Path(tmp_dir))
        assert ph.monday_iso_for_timestamp("2026-10-25T18:30:00+00:00").startswith("2026-10-19T06:00:00")
        assert ph.monday_iso_for_timestamp("2026-10-27T12:00:00+01:00").startswith("2026-10-26T06:00:00")

        sample = Offer(
            id="edeka-1",
            source_offer_id=1,
            retailer="EDEKA",
            retailer_slug="edeka",
            title="Vollmilch 1L",
            brand="Gut & Günstig",
            category="dairy",
            price=0.99,
            old_price=1.29,
            unit_price=0.99,
            unit="1l",
            discount_percent=23.0,
            description="",
            valid_from="2026-10-26",
            valid_to="2026-10-31",
            image_url=None,
            source_url="https://www.edeka.de/angebot",
        )
        # Record a Monday observation and a Sunday observation
        ph.record_offers([sample], zip_code="14195", recorded_at="2026-10-26T06:00:00+01:00")
        ph.record_offers([sample], zip_code="14195", recorded_at="2026-10-25T10:00:00+01:00")
        deleted = ph.purge_non_monday_records()
        assert deleted == 1

        with ph._get_connection() as conn:
            dates = [r[0][:10] for r in conn.execute("SELECT recorded_at FROM price_history").fetchall()]
        assert dates == ["2026-10-26"]

