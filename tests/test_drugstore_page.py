from datetime import datetime

from app import create_app
from app.models import Offer, OfferResult
from app.providers.dm import DmProvider


def test_dm_provider_maps_sale_product_prices_and_image():
    offer = DmProvider._offer_from_product(
        {
            "dan": 3108999,
            "gtin": 4262373259379,
            "brandName": "Dekorieren & Einrichten",
            "title": "Pickleball Set aus Holz & PE, rosa/dunkelgrün/weiß, 1 St",
            "tileData": {
                "brand": {"name": "Dekorieren & Einrichten"},
                "dan": 3108999,
                "eyecatchers": [{"alt": "Ausverkauf Grafik"}],
                "images": [{"tileSrc": "https://products.dm-static.com/example.jpg"}],
                "price": {
                    "price": {
                        "current": {"value": "12,95 €"},
                        "previous": {"value": "17,95 €"},
                    },
                    "tileInfos": ["1 St (12,95 € je 1 St)"],
                },
                "self": "/p/d/3108999/example",
                "title": {"tileHeadline": "Pickleball Set aus Holz & PE, rosa/dunkelgrün/weiß, 1 St"},
                "trackingData": {"categories": ["Deko Limited Editions"]},
            },
        },
        "dm-drogerie-markt",
    )

    assert offer.retailer == "dm"
    assert offer.retailer_slug == "dm-drogerie-markt"
    assert offer.price == 12.95
    assert offer.old_price == 17.95
    assert offer.discount_percent == 27.9
    assert offer.unit_price == 12.95
    assert offer.unit == "St"
    assert offer.image_url == "https://products.dm-static.com/example.jpg"
    assert offer.source_url == "https://www.dm.de/p/d/3108999/example"


def test_drugstore_page_uses_separate_api_and_storage(monkeypatch):
    import app.routes as routes

    class FakeDrugstoreService:
        def get_offers(self, zip_code: str, refresh: bool = False) -> OfferResult:
            return OfferResult(
                offers=[_offer("dm-test", "dm", "dm-drogerie-markt", "2026-05-30T00:00:00+02:00")],
                fetched_at="2026-05-30T08:00:00+00:00",
                warnings=[],
            )

    monkeypatch.setattr(routes, "drugstore_offer_service", lambda: FakeDrugstoreService())
    app = create_app()
    response = app.test_client().get("/drogerie")

    html = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "Drogerie-Angebote" in html
    assert "dm" in html
    assert "ROSSMANN" in html
    assert "budni" in html
    assert 'name="retailer" value="dm-drogerie-markt" checked' in html
    assert 'name="retailer" value="rossmann" checked' in html
    assert 'name="retailer" value="budnikowsky" checked' not in html
    assert "/api/drogerie/basket" in html
    assert "drugstoreDealShoppingList" in html
    assert 'data-basket-template="샴푸 1개, 치약, 바디워시, 비누, 데오"' in html
    assert "data-shopping-whatsapp" in html
    assert "Nächste Woche" in html


def test_week_counts_respect_selected_retailer(monkeypatch):
    import app.routes as routes

    class FakeDrugstoreService:
        def get_offers(self, zip_code: str, refresh: bool = False) -> OfferResult:
            return OfferResult(
                offers=[
                    _offer("dm-test", "dm", "dm-drogerie-markt", "2026-05-30T00:00:00+02:00"),
                    _offer("rossmann-test", "ROSSMANN", "rossmann", "2026-06-01T00:00:00+02:00"),
                ],
                fetched_at="2026-05-30T08:00:00+00:00",
                warnings=[],
            )

    monkeypatch.setattr(routes, "datetime", FrozenDateTime)
    monkeypatch.setattr(routes, "drugstore_offer_service", lambda: FakeDrugstoreService())
    app = create_app()
    response = app.test_client().get("/drogerie?retailer=rossmann")

    html = response.get_data(as_text=True)
    assert response.status_code == 200
    assert ">0</strong>" in html
    assert ">1</strong>" in html
    assert "다음 주 할인 보기" in html


def _offer(identifier: str, retailer: str, retailer_slug: str, valid_from: str) -> Offer:
    return Offer(
        id=identifier,
        source_offer_id=1,
        retailer=retailer,
        retailer_slug=retailer_slug,
        title="Shampoo Angebot",
        brand=None,
        category="personal_care",
        price=1.95,
        old_price=2.45,
        unit_price=None,
        unit=None,
        discount_percent=20.4,
        description="",
        valid_from=valid_from,
        valid_to=None,
        image_url=None,
        source_url="https://example.test",
    )


class FrozenDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return datetime(2026, 5, 30, tzinfo=tz)
