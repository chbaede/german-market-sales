from datetime import date, datetime

from app.models import Offer
from app.routes import build_retailer_week_status, filter_offers_by_week


def test_filter_offers_by_week_separates_current_and_next_week():
    today = date(2026, 5, 30)
    current = _offer("current", "2026-05-26T00:00:00+02:00", "2026-05-30T23:59:59+02:00")
    next_week = _offer("next", "2026-06-01T00:00:00+02:00", "2026-06-07T23:59:59+02:00")
    expired = _offer("expired", "2026-05-19T00:00:00+02:00", "2026-05-24T23:59:59+02:00")

    assert filter_offers_by_week([current, next_week, expired], "current", today=today) == [current]
    assert filter_offers_by_week([current, next_week, expired], "next", today=today) == [next_week]


def test_filter_offers_by_week_uses_berlin_date_for_utc_timestamps():
    today = date(2026, 5, 30)
    next_week = _offer("next", "2026-05-31T22:00:00Z", "2026-06-06T21:59:59Z")

    assert filter_offers_by_week([next_week], "next", today=today) == [next_week]


def test_build_retailer_week_status_reports_missing_next_week_sources(monkeypatch):
    import app.routes as routes

    monkeypatch.setattr(routes, "datetime", FrozenDateTime)
    offers = [
        _offer("lidl-next", "2026-06-08T00:00:00+02:00", "2026-06-14T23:59:59+02:00", "lidl"),
        _offer("edeka-current", "2026-06-01T00:00:00+02:00", "2026-06-06T23:59:59+02:00", "edeka"),
    ]

    status = build_retailer_week_status(offers, {"edeka": "EDEKA", "lidl": "Lidl"}, "next")

    assert status["available"] == [{"slug": "lidl", "label": "Lidl", "count": 1}]
    assert status["missing"] == [{"slug": "edeka", "label": "EDEKA", "count": 0}]


def _offer(identifier: str, valid_from: str, valid_to: str, retailer_slug: str = "rewe") -> Offer:
    return Offer(
        id=identifier,
        source_offer_id=1,
        retailer="REWE",
        retailer_slug=retailer_slug,
        title=identifier,
        brand=None,
        category="other",
        price=1.0,
        old_price=None,
        unit_price=None,
        unit=None,
        discount_percent=None,
        description="",
        valid_from=valid_from,
        valid_to=valid_to,
        image_url=None,
        source_url="https://example.test",
    )


class FrozenDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return datetime(2026, 6, 4, tzinfo=tz)
