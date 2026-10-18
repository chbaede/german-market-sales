import tempfile
from pathlib import Path
from datetime import datetime

import pytest
from app import create_app
from app.models import Offer, OfferResult
from app.services.price_history import PriceHistoryService, normalize_product_key
from app.services.basket import calculate_basket_store_totals, BasketCandidate, BasketItem, BasketRecommendation


def _sample_offer(
    id: str,
    title: str,
    retailer: str,
    price: float | None,
    old_price: float | None = None,
    unit_price: float | None = None,
    unit: str = "kg",
    discount: float | None = None,
    brand: str | None = None,
    scope_type: str = "store",
    region_verified: bool = True,
) -> Offer:
    slug = retailer.lower().replace(" ", "-")
    return Offer(
        id=id,
        source_offer_id=1,
        retailer=retailer,
        retailer_slug=slug,
        title=title,
        brand=brand,
        category="food",
        price=price,
        old_price=old_price,
        unit_price=unit_price,
        unit=unit,
        discount_percent=discount,
        description=title,
        valid_from="2026-05-25",
        valid_to="2026-05-31",
        image_url=None,
        source_url=f"https://example.com/{slug}/{id}",
        requested_zip_code="14195",
        scope_type=scope_type,
        store_id=f"store_{slug}_14195" if scope_type == "store" else None,
        store_name=f"{retailer} Berlin Dahlem" if scope_type == "store" else None,
        store_address="Clayallee 100, 14195 Berlin" if scope_type == "store" else None,
        region_verified=region_verified,
        source_provider="edeka-official" if (region_verified and scope_type == "store") else ("marktguru" if region_verified else None),
        source_region_id="national_flyer:100" if (region_verified and scope_type == "nationwide") else None,
        verification_method="store_direct_api" if (region_verified and scope_type == "store") else ("marktguru_national_flyer" if (region_verified and scope_type == "nationwide") else None),
    )


def test_normalize_product_key():
    key1 = normalize_product_key("Nutella 450g Glas", "Ferrero")
    key2 = normalize_product_key("Ferrero Nutella 450 g", None)
    assert "nutella" in key1
    assert "nutella" in key2


def test_price_history_service_record_and_stats():
    with tempfile.TemporaryDirectory() as tmp_dir:
        service = PriceHistoryService(Path(tmp_dir))
        offers = [
            _sample_offer("1", "Barilla Pasta 500g", "REWE", 0.99, 1.89, brand="Barilla"),
            _sample_offer("2", "Barilla Pasta 500g", "Lidl", 0.89, 1.89, brand="Barilla"),
        ]

        recorded = service.record_offers(offers, zip_code="14195", recorded_at="2026-05-25T10:00:00")
        assert recorded == 2

        stats = service.get_product_stats("Barilla Pasta 500g", brand="Barilla", current_price=0.89)
        assert stats["all_time_low"] == 0.89
        assert stats["is_all_time_low"] is True
        assert len(stats["history"]) == 2

        # Check compare across stores
        comp = service.compare_across_stores("Barilla", offers)
        assert comp["cheapest_store"] is not None
        assert comp["cheapest_store"]["retailer"] == "Lidl"
        assert comp["cheapest_store"]["price"] == 0.89
        assert comp["potential_savings"] == 0.10


def test_basket_store_totals_and_smart_split():
    rewe_pasta = _sample_offer("1", "Barilla Pasta", "REWE", 1.99)
    lidl_pasta = _sample_offer("2", "Barilla Pasta", "Lidl", 1.49)
    rewe_sauce = _sample_offer("3", "Tomatensauce", "REWE", 1.29)
    dm_shampoo = _sample_offer("4", "Balea Shampoo", "dm", 0.85)

    item1 = BasketItem(raw="파스타", name="파스타", amount=1, unit="개", keywords=("pasta", "barilla"))
    item2 = BasketItem(raw="소스", name="소스", amount=1, unit="개", keywords=("sauce",))
    item3 = BasketItem(raw="샴푸", name="샴푸", amount=1, unit="개", keywords=("shampoo",))

    cand1_rewe = BasketCandidate(offer=rewe_pasta, estimated_total=1.99, estimate_note="개별가")
    cand1_lidl = BasketCandidate(offer=lidl_pasta, estimated_total=1.49, estimate_note="개별가")
    cand2_rewe = BasketCandidate(offer=rewe_sauce, estimated_total=1.29, estimate_note="개별가")
    cand3_dm = BasketCandidate(offer=dm_shampoo, estimated_total=0.85, estimate_note="개별가")

    recommendations = [
        BasketRecommendation(item=item1, candidates=[cand1_lidl, cand1_rewe]),
        BasketRecommendation(item=item2, candidates=[cand2_rewe]),
        BasketRecommendation(item=item3, candidates=[cand3_dm]),
    ]

    all_retailers = {"rewe": "REWE", "lidl": "Lidl", "dm": "dm"}
    totals = calculate_basket_store_totals(recommendations, all_retailers)

    assert totals["total_requested_items"] == 3
    # Check smart split combines Lidl (1.49) + REWE (1.29) + dm (0.85) = 3.63
    smart = totals["smart_split"]
    assert smart["total_cost"] == pytest.approx(3.63, 0.01)

    stores = {s["retailer"]: s for s in totals["stores"]}
    assert "REWE" in stores
    assert stores["REWE"]["covered_items_count"] == 2
    assert stores["REWE"]["total_cost"] == pytest.approx(3.28, 0.01)


def test_price_tracker_apis(monkeypatch, tmp_path):
    import app.routes as routes
    from app.config import Config

    class TestConfig(Config):
        TESTING = True
        AUTO_REFRESH_ENABLED = False
        CACHE_DIR = tmp_path

    fake_offers = [
        _sample_offer("rewe-kaffee", "Eduscho Kaffee 500g", "REWE", 3.99, old_price=5.99, brand="Eduscho"),
        _sample_offer("edeka-kaffee", "Eduscho Kaffee 500g", "EDEKA", 4.49, old_price=5.99, brand="Eduscho"),
    ]
    for o in fake_offers:
        o.valid_to = "2099-12-31"
    ph_service = PriceHistoryService(cache_dir=tmp_path)
    ph_service.record_offers(fake_offers, zip_code="14195")

    class FakeService:
        price_history = ph_service

        def get_cached_offers(self, zip_code: str):
            return OfferResult(offers=fake_offers, fetched_at="2026-06-08T08:00:00+00:00", warnings=[], from_cache=True)

        def get_offers(self, zip_code: str, refresh: bool = False):
            return self.get_cached_offers(zip_code)

    monkeypatch.setattr(routes, "offer_service", lambda: FakeService())
    monkeypatch.setattr(routes, "drugstore_offer_service", lambda: FakeService())
    monkeypatch.setattr(routes, "_queue_refresh", lambda service, zip_code: None)

    app = create_app(TestConfig)
    client = app.test_client()

    # Test /api/price-history
    res = client.get("/api/price-history?q=Eduscho%20Kaffee&brand=Eduscho&price=3.99")
    assert res.status_code == 200
    data = res.get_json()
    assert "product_name" in data
    assert "avg_price" in data
    assert "history" in data

    # Test /api/compare
    res = client.get("/api/compare?q=Kaffee&zip_code=14195")
    assert res.status_code == 200
    comp_data = res.get_json()
    assert "stores" in comp_data
    assert comp_data["stores_count"] == 2

    # Test /api/watchlist-deals
    payload = {
        "watchlist": [
            {"id": "wl_1", "keyword": "Kaffee", "target_price": 5.0},
        ],
        "zip_code": "14195",
        "week": "current",
    }
    res = client.post("/api/watchlist-deals", json=payload)
    assert res.status_code == 200
    wl_data = res.get_json()
    assert "results" in wl_data
    assert len(wl_data["results"]) == 1
    assert wl_data["results"][0]["deals_count"] >= 2



def test_build_weekly_competitor_map():
    from app.services.price_history import build_weekly_competitor_map

    rewe_pasta = _sample_offer("rewe-1", "Barilla Pasta 500g", "REWE", 0.99, brand="Barilla")
    lidl_pasta = _sample_offer("lidl-1", "Barilla Pasta 500g", "Lidl", 0.89, brand="Barilla")
    kaufland_pasta = _sample_offer("kaufland-1", "Barilla Pasta 500g", "Kaufland", 1.19, brand="Barilla")
    other_item = _sample_offer("other-1", "Brot", "REWE", 1.50)

    offers = [rewe_pasta, lidl_pasta, kaufland_pasta, other_item]
    comp_map = build_weekly_competitor_map(offers)

    # REWE pasta should have 2 competitors: Lidl (0.89) and Kaufland (1.19)
    assert "rewe-1" in comp_map
    rewe_comps = comp_map["rewe-1"]
    assert len(rewe_comps) == 2
    assert rewe_comps[0]["retailer"] == "Lidl"
    assert rewe_comps[0]["price"] == 0.89
    assert rewe_comps[0]["price_diff"] == 0.10  # REWE is 0.10 more expensive than Lidl

    # Lidl pasta should have REWE (0.99) and Kaufland (1.19)
    assert "lidl-1" in comp_map
    lidl_comps = comp_map["lidl-1"]
    assert lidl_comps[0]["retailer"] == "REWE"
    assert lidl_comps[0]["price_diff"] == -0.10  # Lidl is 0.10 cheaper than REWE

    # Single-store item should have no competitors
    assert "other-1" not in comp_map


def test_enrich_weekly_competitors_and_overlap_week_counts():
    from datetime import date
    from app.routes import build_week_counts, WEEK_LABELS
    from app.services.price_history import enrich_weekly_competitors

    rewe_pasta = _sample_offer("rewe-1", "Barilla Pasta 500g", "REWE", 0.99, brand="Barilla")
    lidl_pasta = _sample_offer("lidl-1", "Barilla Pasta 500g", "Lidl", 0.89, brand="Barilla")
    solo_item = _sample_offer("solo-1", "Bio Eier", "REWE", 2.49)
    for o in (rewe_pasta, lidl_pasta, solo_item):
        o.valid_from = "2026-01-01"
        o.valid_to = "2099-12-31"

    enriched = enrich_weekly_competitors([rewe_pasta, lidl_pasta, solo_item])
    assert enriched[0].is_cheapest_in_week is False
    assert enriched[1].is_cheapest_in_week is True
    assert enriched[0].cheapest_competitor["retailer"] == "Lidl"
    assert not enriched[2].competitors

    # Verify build_week_counts with overlap_only=True works even on fresh un-enriched offers
    fresh_offers = [
        _sample_offer("rewe-2", "Barilla Pasta 500g", "REWE", 0.99, brand="Barilla"),
        _sample_offer("lidl-2", "Barilla Pasta 500g", "Lidl", 0.89, brand="Barilla"),
        _sample_offer("solo-2", "Bio Eier", "REWE", 2.49),
    ]
    for o in fresh_offers:
        o.valid_from = "2026-01-01"
        o.valid_to = "2099-12-31"

    counts = build_week_counts(fresh_offers, WEEK_LABELS, overlap_only=True)
    assert counts["current"] == 2



