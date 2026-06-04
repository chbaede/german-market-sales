from datetime import datetime

from app.models import Offer, OfferResult
from app import create_app
from app.routes import _basket_recommendation_to_dict
from app.services.basket import BasketCandidate, BasketItem, BasketRecommendation, parse_basket_items, recommend_basket_items


def test_parse_basket_items_reads_korean_item_and_quantity():
    items = parse_basket_items("파 4개, 당근")

    assert len(items) == 2
    assert items[0].name == "파"
    assert items[0].amount == 4
    assert items[0].unit == "개"
    assert "lauchzwiebeln" in items[0].keywords
    assert items[1].name == "당근"
    assert items[1].amount is None
    assert "möhren" in items[1].keywords


def test_recommend_basket_items_matches_german_offer_keywords_and_estimates_price():
    offers = [
        _offer("Lauchzwiebeln", 0.79, "Bund"),
        _offer("Möhren", 1.29, "kg", unit_price=1.29),
        _offer("Paprika-Mix", 1.39, "kg", unit_price=2.78),
        _offer("Kaffee Crema", 6.99, "kg", unit_price=13.98),
    ]

    recommendations = recommend_basket_items("파 4개, 당근 1kg", offers)

    assert recommendations[0].candidates[0].offer.title == "Lauchzwiebeln"
    assert recommendations[0].candidates[0].estimated_total == 3.16
    assert [candidate.offer.title for candidate in recommendations[0].candidates] == ["Lauchzwiebeln"]
    assert recommendations[1].candidates[0].offer.title == "Möhren"
    assert recommendations[1].candidates[0].estimated_total == 1.29


def test_basket_recommendation_dict_includes_price_discount_and_estimate():
    offer = _offer("Lauchzwiebeln", 0.49, "Bund")
    offer.old_price = 0.59
    offer.discount_percent = None
    offer.image_url = "https://example.test/lauchzwiebeln.jpg"
    recommendation = BasketRecommendation(
        item=BasketItem(raw="파 4개", name="파", amount=4, unit="개", keywords=("lauchzwiebeln",)),
        candidates=[
            BasketCandidate(
                offer=offer,
                score=8,
                matched_keywords=("lauchzwiebeln",),
                estimated_total=1.96,
                estimate_note="수량 기준 단순 예상",
            )
        ],
    )

    payload = _basket_recommendation_to_dict(recommendation)

    candidate = payload["candidates"][0]
    assert payload["item"]["amount_text"] == "4 개"
    assert candidate["old_price_text"] == "0,59 EUR"
    assert candidate["price_text"] == "0,49 EUR"
    assert candidate["discount_text"] == "-17%"
    assert candidate["estimated_total_text"] == "1,96 EUR"
    assert candidate["image_url"] == "https://example.test/lauchzwiebeln.jpg"


def test_basket_api_respects_selected_retailers(monkeypatch):
    import app.routes as routes

    class FakeOfferService:
        def get_offers(self, zip_code: str, refresh: bool = False) -> OfferResult:
            return OfferResult(
                offers=[_offer("Lauchzwiebeln", 0.79, "Bund")],
                fetched_at="2026-05-30T08:00:00+00:00",
                warnings=[],
            )

    monkeypatch.setattr(routes, "datetime", FrozenDateTime)
    monkeypatch.setattr(routes, "offer_service", lambda: FakeOfferService())
    app = create_app()
    client = app.test_client()

    response = client.get(
        "/api/basket?zip_code=14195&week=current&basket=%ED%8C%8C%204%EA%B0%9C&retailer=rewe"
    )
    assert response.status_code == 200
    payload = response.get_json()

    assert payload["retailers"] == ["rewe"]
    candidates = payload["recommendations"][0]["candidates"]
    assert candidates
    assert {candidate["retailer"] for candidate in candidates} == {"REWE"}


def _offer(title: str, price: float, unit: str | None = None, unit_price: float | None = None) -> Offer:
    return Offer(
        id=title,
        source_offer_id=1,
        retailer="REWE",
        retailer_slug="rewe",
        title=title,
        brand=None,
        category="produce",
        price=price,
        old_price=None,
        unit_price=unit_price,
        unit=unit,
        discount_percent=20,
        description="",
        valid_from="2026-05-25T00:00:00+02:00",
        valid_to="2026-05-31T23:59:59+02:00",
        image_url=None,
        source_url="https://example.test",
    )


class FrozenDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return datetime(2026, 5, 30, tzinfo=tz)
