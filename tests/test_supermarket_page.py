from datetime import datetime

from app import create_app
from app.models import OfferResult


def test_supermarket_page_lists_added_berlin_retailers(monkeypatch):
    import app.routes as routes

    class FakeOfferService:
        def get_offers(self, zip_code: str, refresh: bool = False) -> OfferResult:
            return OfferResult(offers=[], fetched_at="2026-06-04T08:00:00+00:00", warnings=[])

    monkeypatch.setattr(routes, "datetime", FrozenDateTime)
    monkeypatch.setattr(routes, "offer_service", lambda: FakeOfferService())
    app = create_app()
    response = app.test_client().get("/")

    html = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "PENNY" in html
    assert "nahkauf" in html


class FrozenDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return datetime(2026, 6, 4, tzinfo=tz)
