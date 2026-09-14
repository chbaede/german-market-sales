from datetime import datetime

from app import create_app
from app.models import Offer, OfferResult


def test_supermarket_page_lists_added_berlin_retailers(monkeypatch):
    import app.routes as routes

    class FakeOfferService:
        def get_offers(self, zip_code: str, refresh: bool = False) -> OfferResult:
            return OfferResult(
                offers=[
                    _offer("ALDI default item", "aldi-nord"),
                    _offer("PENNY extra item", "penny"),
                ],
                fetched_at="2026-06-04T08:00:00+00:00",
                warnings=[],
            )

    monkeypatch.setattr(routes, "datetime", FrozenDateTime)
    monkeypatch.setattr(routes, "offer_service", lambda: FakeOfferService())
    app = create_app()
    response = app.test_client().get("/")

    html = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "PENNY" in html
    assert "nahkauf" in html
    assert 'name="retailer" value="aldi-nord" checked' in html
    assert 'name="retailer" value="edeka" checked' in html
    assert 'name="retailer" value="rewe" checked' in html
    assert 'name="retailer" value="lidl" checked' in html
    assert 'name="retailer" value="penny" checked' not in html
    assert 'name="retailer_filter" value="1"' in html
    assert "ALDI default item" in html
    assert "PENNY extra item" not in html
    assert '<span class="check-pill-count">1</span>' in html
    assert 'data-basket-template="우유 2개, 계란, 양파, 당근, 토마토, 바나나, 빵, 버터"' in html
    assert "data-share-page" in html
    assert "site-footer" in html


def test_supermarket_default_sort_prioritizes_weekly_essentials(monkeypatch):
    import app.routes as routes

    class FakeOfferService:
        def get_offers(self, zip_code: str, refresh: bool = False) -> OfferResult:
            return OfferResult(
                offers=[
                    _offer("Sneaker Aktion", "aldi-nord", category="nonfood", discount_percent=80),
                    _offer("Frische Milch", "edeka", category="dairy", discount_percent=5),
                ],
                fetched_at="2026-06-04T08:00:00+00:00",
                warnings=[],
            )

    monkeypatch.setattr(routes, "datetime", FrozenDateTime)
    monkeypatch.setattr(routes, "offer_service", lambda: FakeOfferService())
    app = create_app()
    response = app.test_client().get("/")

    html = response.get_data(as_text=True)
    assert response.status_code == 200
    assert '<option value="essentials" selected' in html
    assert html.index("Frische Milch") < html.index("Sneaker Aktion")


def test_supermarket_page_can_hide_offers_without_prices(monkeypatch):
    import app.routes as routes

    class FakeOfferService:
        def get_offers(self, zip_code: str, refresh: bool = False) -> OfferResult:
            return OfferResult(
                offers=[
                    _offer("Frische Milch", "edeka", category="dairy", price=1.0),
                    _offer("Preis folgt", "edeka", category="dairy", price=None),
                ],
                fetched_at="2026-06-04T08:00:00+00:00",
                warnings=[],
            )

    monkeypatch.setattr(routes, "datetime", FrozenDateTime)
    monkeypatch.setattr(routes, "offer_service", lambda: FakeOfferService())
    app = create_app()
    response = app.test_client().get("/?priced=1")

    html = response.get_data(as_text=True)
    assert response.status_code == 200
    assert 'name="priced" value="1" checked' in html
    assert "Frische Milch" in html
    assert "Preis folgt" not in html
    assert '<span class="check-pill-count">1</span>' in html


def test_supermarket_page_can_filter_offers_ending_soon(monkeypatch):
    import app.routes as routes

    class FakeOfferService:
        def get_offers(self, zip_code: str, refresh: bool = False) -> OfferResult:
            return OfferResult(
                offers=[
                    _offer("Heute endet", "edeka", category="dairy", valid_to="2026-06-04T23:59:59+02:00"),
                    _offer("Morgen endet", "edeka", category="dairy", valid_to="2026-06-05T23:59:59+02:00"),
                    _offer("Noch Zeit", "edeka", category="dairy", valid_to="2026-06-07T23:59:59+02:00"),
                ],
                fetched_at="2026-06-04T08:00:00+00:00",
                warnings=[],
            )

    monkeypatch.setattr(routes, "datetime", FrozenDateTime)
    monkeypatch.setattr(routes, "offer_service", lambda: FakeOfferService())
    app = create_app()
    response = app.test_client().get("/?ending=1")

    html = response.get_data(as_text=True)
    assert response.status_code == 200
    assert 'name="ending" value="1" checked' in html
    assert "Heute endet" in html
    assert "Morgen endet" in html
    assert "Noch Zeit" not in html
    assert "마감 임박" in html


def test_supermarket_page_uses_cached_offers_and_queues_refresh_by_default(monkeypatch):
    import app.routes as routes

    calls: list[str] = []
    queued: list[str] = []

    class FakeOfferService:
        def get_cached_offers(self, zip_code: str) -> OfferResult | None:
            calls.append(f"cache:{zip_code}")
            return OfferResult(
                offers=[_offer("Frische Milch", "edeka", category="dairy")],
                fetched_at="2026-06-04T08:00:00+00:00",
                warnings=[],
                from_cache=True,
            )

        def get_offers(self, zip_code: str, refresh: bool = False) -> OfferResult:
            calls.append(f"live:{refresh}")
            return OfferResult(
                offers=[_offer("Frische Milch", "edeka", category="dairy")],
                fetched_at="2026-06-04T08:00:00+00:00",
                warnings=[],
            )

    monkeypatch.setattr(routes, "datetime", FrozenDateTime)
    monkeypatch.setattr(routes, "offer_service", lambda: FakeOfferService())
    monkeypatch.setattr(routes, "_queue_refresh", lambda service, zip_code: queued.append(zip_code))
    app = create_app()

    response = app.test_client().get("/")

    assert response.status_code == 200
    assert calls == ["cache:14195"]
    assert queued == ["14195"]


def test_offers_api_uses_cached_offers_and_queues_refresh_by_default(monkeypatch):
    import app.routes as routes

    calls: list[str] = []
    queued: list[str] = []

    class FakeOfferService:
        def get_cached_offers(self, zip_code: str) -> OfferResult | None:
            calls.append(f"cache:{zip_code}")
            return OfferResult(
                offers=[_offer("Frische Milch", "edeka", category="dairy")],
                fetched_at="2026-06-04T08:00:00+00:00",
                warnings=[],
                from_cache=True,
            )

        def get_offers(self, zip_code: str, refresh: bool = False) -> OfferResult:
            calls.append(f"live:{refresh}")
            return OfferResult(
                offers=[_offer("Frische Milch", "edeka", category="dairy")],
                fetched_at="2026-06-04T08:00:00+00:00",
                warnings=[],
            )

    monkeypatch.setattr(routes, "datetime", FrozenDateTime)
    monkeypatch.setattr(routes, "offer_service", lambda: FakeOfferService())
    monkeypatch.setattr(routes, "_queue_refresh", lambda service, zip_code: queued.append(zip_code))
    app = create_app()

    response = app.test_client().get("/api/offers")

    assert response.status_code == 200
    assert calls == ["cache:14195"]
    assert queued == ["14195"]


def test_supermarket_page_refresh_parameter_still_fetches_synchronously(monkeypatch):
    import app.routes as routes

    calls: list[bool] = []
    queued: list[str] = []

    class FakeOfferService:
        def get_cached_offers(self, zip_code: str) -> OfferResult | None:
            raise AssertionError("refresh=1 should not use the cache-first path")

        def get_offers(self, zip_code: str, refresh: bool = False) -> OfferResult:
            calls.append(refresh)
            return OfferResult(
                offers=[_offer("Frische Milch", "edeka", category="dairy")],
                fetched_at="2026-06-04T08:00:00+00:00",
                warnings=[],
            )

    monkeypatch.setattr(routes, "datetime", FrozenDateTime)
    monkeypatch.setattr(routes, "offer_service", lambda: FakeOfferService())
    monkeypatch.setattr(routes, "_queue_refresh", lambda service, zip_code: queued.append(zip_code))
    app = create_app()

    response = app.test_client().get("/?refresh=1")

    assert response.status_code == 200
    assert calls == [True]
    assert queued == []


def _offer(
    title: str,
    retailer_slug: str,
    category: str = "other",
    discount_percent: float | None = None,
    price: float | None = 1.0,
    valid_to: str = "2026-06-07T23:59:59+02:00",
) -> Offer:
    return Offer(
        id=title,
        source_offer_id=1,
        retailer=retailer_slug,
        retailer_slug=retailer_slug,
        title=title,
        brand=None,
        category=category,
        price=price,
        old_price=None,
        unit_price=None,
        unit=None,
        discount_percent=discount_percent,
        description="",
        valid_from="2026-06-01T00:00:00+02:00",
        valid_to=valid_to,
        image_url=None,
        source_url="https://example.test",
    )


class FrozenDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return datetime(2026, 6, 4, tzinfo=tz)
