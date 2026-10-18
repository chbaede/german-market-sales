from __future__ import annotations

import json
from pathlib import Path
import tempfile

import pytest

from app import create_app
from app.models import Offer
from app.providers.aldi_nord import AldiNordProvider
from app.providers.aldi_sued import AldiSuedProvider
from app.providers.edeka import EdekaProvider, _sort_candidate_markets
from app.providers.kaufland import KauflandProvider
from app.providers.marktguru import MarktguruProvider
from app.providers.rewe import ReweProvider
from app.providers.dm import DmProvider
from app.services.location import (
    haversine_distance_km,
    is_aldi_sued_territory,
    is_aldi_nord_territory,
    get_coordinates_for_plz,
)
from app.services.offers import OfferService
from app.services.price_history import PriceHistoryService, normalize_product_key


def test_territory_separation_berlin_munich_hamburg_cologne():
    # Berlin 10115 -> ALDI Nord territory, NOT ALDI Süd
    assert is_aldi_nord_territory("10115") is True
    assert is_aldi_sued_territory("10115") is False

    # Munich 80331 -> ALDI Süd territory, NOT ALDI Nord
    assert is_aldi_sued_territory("80331") is True
    assert is_aldi_nord_territory("80331") is False

    # Hamburg 20095 -> ALDI Nord territory, NOT ALDI Süd
    assert is_aldi_nord_territory("20095") is True
    assert is_aldi_sued_territory("20095") is False

    # Cologne 50667 -> ALDI Süd territory, NOT ALDI Nord
    assert is_aldi_sued_territory("50667") is True
    assert is_aldi_nord_territory("50667") is False


def test_aldi_providers_respect_territory_without_faking_data():
    aldi_sued = AldiSuedProvider()
    aldi_nord = AldiNordProvider()

    # ALDI Süd in Berlin: returns empty with territory warning
    offers_sued_berlin, warnings_sued_berlin = aldi_sued.fetch_offers("aldi-sued", "10115", limit=10)
    assert offers_sued_berlin == []
    assert any("ALDI Nord" in w for w in warnings_sued_berlin)

    # ALDI Nord in Munich: returns empty with territory warning
    offers_nord_munich, warnings_nord_munich = aldi_nord.fetch_offers("aldi-nord", "80331", limit=10)
    assert offers_nord_munich == []
    assert any("ALDI Süd" in w for w in warnings_nord_munich)


def test_marktguru_preserves_requested_zip_for_all_regions(monkeypatch):
    mg = MarktguruProvider()
    requested_zips = []

    def mock_get_config(retailer_slug):
        from app.providers.marktguru import MarktguruConfig
        return MarktguruConfig("api.example.com", "key", "client", False)

    def mock_request_json(url, params=None, headers=None):
        if params and "zipCode" in params:
            requested_zips.append(params["zipCode"])
        return {"data": []}, False

    monkeypatch.setattr(mg, "_get_config", mock_get_config)
    monkeypatch.setattr(mg, "_request_json", mock_request_json)

    for plz in ("10115", "80331", "20095", "50667"):
        mg.fetch_offers("edeka", plz, limit=10)
        assert plz in requested_zips


def test_edeka_market_sorting_uses_haversine_and_exact_match():
    # For Cologne 50667:
    # Market A in 50667 (exact match)
    # Market B in 50825 (nearby Cologne)
    # Market C in 80331 (Munich, far away)
    cologne_coords = get_coordinates_for_plz("50667")
    munich_coords = get_coordinates_for_plz("80331")

    markets = [
        {"id": "edeka-munich", "name": "Edeka Munich", "contact": {"address": {"postalCode": "80331"}}, "coordinates": {"lat": munich_coords[0], "lon": munich_coords[1]}},
        {"id": "edeka-cologne-near", "name": "Edeka Cologne Near", "contact": {"address": {"postalCode": "50825"}}, "coordinates": {"lat": 50.94, "lon": 6.91}},
        {"id": "edeka-cologne-center", "name": "Edeka Cologne Center", "contact": {"address": {"postalCode": "50667"}}, "coordinates": {"lat": cologne_coords[0], "lon": cologne_coords[1]}},
    ]

    sorted_markets = _sort_candidate_markets(markets, "50667")
    assert sorted_markets[0]["name"] == "Edeka Cologne Center"
    assert sorted_markets[1]["name"] == "Edeka Cologne Near"
    assert len(sorted_markets) == 2  # Munich (>60km) is defensively excluded



def test_kaufland_store_resolution_uses_haversine_not_numeric_difference():
    provider = KauflandProvider()
    # Postcode 10115 (Berlin)
    # Suppose we have store in 13353 (Berlin Wedding, 4km away)
    # and a store in 09999 (numeric difference |10115-9999| = 116, but in Saxony, 200km away!)
    # Numerical proximity would have picked 09999 over 13353 (diff = 3238), but 13353 is Berlin Wedding (4km away)!
    stores = [
        {"n": "saxony", "pc": "09999", "lat": 50.8, "lng": 12.9},
        {"n": "berlin_wedding", "pc": "13353", "lat": 52.54, "lng": 13.36},
    ]
    resolved = provider._resolve_store("10115", stores=stores)
    assert resolved["n"] == "berlin_wedding"


def test_product_key_normalizes_and_preserves_package_sizes():
    # Nutella 450g vs Nutella 750g
    key_450_1 = normalize_product_key("Nutella 450g Glas", "Ferrero")
    key_450_2 = normalize_product_key("Ferrero Nutella 450 g", None)
    key_750 = normalize_product_key("Ferrero Nutella 750g", None)

    assert key_450_1 == key_450_2
    assert "450g" in key_450_1
    assert "750g" in key_750
    assert key_450_1 != key_750

    # Barilla 500g vs Barilla 1kg
    key_barilla_500 = normalize_product_key("Barilla Spaghetti No 5 500g", "Barilla")
    key_barilla_1kg = normalize_product_key("Barilla Spaghetti No 5 1kg", "Barilla")
    assert key_barilla_500 != key_barilla_1kg
    assert "500g" in key_barilla_500
    assert "1000g" in key_barilla_1kg

    # Coca Cola 1.5l vs 0.33l
    key_coke_15 = normalize_product_key("Coca-Cola 1,5l Flasche", "Coca Cola")
    key_coke_can = normalize_product_key("Coca-Cola 0.33l Dose", "Coca Cola")
    assert key_coke_15 != key_coke_can
    assert "1500ml" in key_coke_15
    assert "330ml" in key_coke_can


def test_price_history_regional_atl_separation():
    with tempfile.TemporaryDirectory() as tmp_dir:
        service = PriceHistoryService(Path(tmp_dir))

        # Record offer for Barilla in Berlin 10115 at 0.99 EUR
        offer_berlin = Offer(
            id="barilla-10115",
            source_offer_id=1,
            retailer="EDEKA",
            retailer_slug="edeka",
            title="Barilla Spaghetti 500g",
            brand="Barilla",
            category="food",
            price=0.99,
            old_price=1.89,
            unit_price=1.98,
            unit="kg",
            discount_percent=47.6,
            description="500g",
            valid_from="2026-05-25",
            valid_to="2026-05-31",
            image_url=None,
            source_url="https://edeka.de",
            requested_zip_code="10115",
            scope_type="store",
            store_id="edeka_berlin_1",
            store_name="EDEKA Berlin",
            store_address="Invalidenstraße 100, 10115 Berlin",
            source_provider="edeka-official",
            region_verified=True,
            verification_method="store_direct_api",
        )

        # Record offer for Barilla in Munich 80331 at 0.79 EUR (cheaper in Munich)
        offer_munich = Offer(
            id="barilla-80331",
            source_offer_id=2,
            retailer="EDEKA",
            retailer_slug="edeka",
            title="Barilla Spaghetti 500g",
            brand="Barilla",
            category="food",
            price=0.79,
            old_price=1.89,
            unit_price=1.58,
            unit="kg",
            discount_percent=58.2,
            description="500g",
            valid_from="2026-05-25",
            valid_to="2026-05-31",
            image_url=None,
            source_url="https://edeka.de",
            requested_zip_code="80331",
            scope_type="store",
            store_id="edeka_munich_1",
            store_name="EDEKA Munich",
            store_address="Marienplatz 1, 80331 München",
            source_provider="edeka-official",
            region_verified=True,
            verification_method="store_direct_api",
        )

        service.record_offers([offer_berlin], zip_code="10115", recorded_at="2026-05-25T10:00:00")
        service.record_offers([offer_munich], zip_code="80331", recorded_at="2026-05-25T10:00:00")

        # In Berlin, regional ATL should be 0.99, NOT Munich's 0.79!
        berlin_atl_map = service.get_all_time_low_map(zip_code="10115")
        munich_atl_map = service.get_all_time_low_map(zip_code="80331")
        national_atl_map = service.get_all_time_low_map(zip_code=None)

        pkey = normalize_product_key("Barilla Spaghetti 500g", "Barilla")
        assert berlin_atl_map[pkey] == 0.99
        assert munich_atl_map[pkey] == 0.79
        assert national_atl_map[pkey] == 0.79

        # Stats for Berlin user
        berlin_stats = service.get_product_stats(
            "Barilla Spaghetti 500g",
            brand="Barilla",
            current_price=0.99,
            zip_code="10115",
        )
        assert berlin_stats["regional_all_time_low"] == 0.99
        assert berlin_stats["national_all_time_low"] == 0.79
        assert berlin_stats["is_regional_all_time_low"] is True
        assert berlin_stats["is_national_all_time_low"] is False


def test_cache_isolation_between_regions():
    with tempfile.TemporaryDirectory() as tmp_dir:
        cache_path = Path(tmp_dir)
        service = OfferService(
            cache_dir=cache_path,
            retailers={"edeka": "EDEKA"},
            cache_ttl_seconds=3600,
            timeout=10,
            verify_tls=True,
            max_offers_per_retailer=50,
        )

        # Write cache specifically for Munich 80331
        service._write_cache("80331", "2026-05-25T10:00:00", [], ["Munich note"])

        # Berlin 10115 must not read Munich cache
        assert service.get_cached_offers("10115") is None

        # Munich 80331 reads its own cache
        cached_munich = service.get_cached_offers("80331")
        assert cached_munich is not None
        assert cached_munich.warnings == ["Munich note"]


def test_api_diagnostics_endpoint(monkeypatch):
    app = create_app()
    client = app.test_client()

    def mock_fetch_retailer_offers(self, slug, zip_code):
        return [
            Offer(
                id=f"{slug}-test-1",
                source_offer_id=1,
                retailer=slug.upper(),
                retailer_slug=slug,
                title="Test Product",
                brand=None,
                category="food",
                price=1.99,
                old_price=None,
                unit_price=1.99,
                unit="kg",
                discount_percent=None,
                description="",
                valid_from="2026-06-01",
                valid_to="2026-06-07",
                image_url=None,
                source_url="https://example.com",
                requested_zip_code=zip_code,
                scope_type="nationwide",
                region_verified=True,
            )
        ], []

    monkeypatch.setattr(OfferService, "_fetch_retailer_offers", mock_fetch_retailer_offers)

    response = client.get("/api/diagnostics?zip_code=10115")
    assert response.status_code == 200
    data = response.get_json()
    assert data["zip_code"] == "10115"
    assert "retailers" in data
    assert "timestamp" in data


def test_four_cities_deterministic_pipeline(monkeypatch):
    """
    Deterministic end-to-end verification for Berlin (10115), Munich (80331),
    Hamburg (20095), and Cologne (50667) without external network dependencies.
    """
    dummy_offer = Offer(
        id="dummy",
        source_offer_id=1,
        retailer="ALDI",
        retailer_slug="aldi",
        title="Dummy",
        brand=None,
        category="food",
        price=1.0,
        old_price=None,
        unit_price=None,
        unit=None,
        discount_percent=None,
        description="",
        valid_from=None,
        valid_to=None,
        image_url=None,
        source_url="https://example.com",
    )
    monkeypatch.setattr(AldiSuedProvider, "offers_from_html", lambda *args, **kwargs: [dummy_offer])
    monkeypatch.setattr(AldiSuedProvider, "_request_text", lambda *args, **kwargs: ("<html></html>", False))
    monkeypatch.setattr(AldiNordProvider, "offers_from_html", lambda *args, **kwargs: [dummy_offer])
    monkeypatch.setattr(AldiNordProvider, "_request_text", lambda *args, **kwargs: ("<html></html>", False))

    app = create_app()
    client = app.test_client()

    target_cities = {
        "10115": {"name": "Berlin", "aldi_nord": True, "aldi_sued": False},
        "80331": {"name": "Munich", "aldi_nord": False, "aldi_sued": True},
        "20095": {"name": "Hamburg", "aldi_nord": True, "aldi_sued": False},
        "50667": {"name": "Cologne", "aldi_nord": False, "aldi_sued": True},
    }

    aldi_sued = AldiSuedProvider()
    aldi_nord = AldiNordProvider()

    for plz, meta in target_cities.items():
        # 1. ALDI Süd and Nord territory validation
        offers_sued, warn_sued = aldi_sued.fetch_offers("aldi-sued", plz, limit=5)
        if meta["aldi_sued"]:
            # In ALDI Süd territory, fetch_offers doesn't return territory failure
            assert not any("ALDI Nord 관할 지역" in w for w in warn_sued)
        else:
            # In ALDI Nord territory, ALDI Süd must be empty with clear warning
            assert offers_sued == []
            assert any("ALDI Nord 관할 지역" in w for w in warn_sued)

        offers_nord, warn_nord = aldi_nord.fetch_offers("aldi-nord", plz, limit=5)
        if meta["aldi_nord"]:
            assert not any("ALDI Süd 관할 지역" in w for w in warn_nord)
        else:
            assert offers_nord == []
            assert any("ALDI Süd 관할 지역" in w for w in warn_nord)

        # 2. Kaufland store resolution requires nearby coordinates
        kp = KauflandProvider()
        # Mock storefinder with stores across Germany
        stores_pool = [
            {"n": "berlin_alex", "pc": "10178", "lat": 52.52, "lng": 13.41, "cn": "Kaufland Berlin-Mitte"},
            {"n": "munich_center", "pc": "80339", "lat": 48.13, "lng": 11.53, "cn": "Kaufland München"},
            {"n": "hamburg_altona", "pc": "22765", "lat": 53.55, "lng": 9.93, "cn": "Kaufland Hamburg-Altona"},
            {"n": "cologne_ehrenfeld", "pc": "50825", "lat": 50.95, "lng": 6.92, "cn": "Kaufland Köln-Ehrenfeld"},
        ]
        resolved_kaufland = kp._resolve_store(plz, stores=stores_pool)
        assert resolved_kaufland is not None

        # Verify correct city store is selected
        if plz == "10115":
            assert resolved_kaufland["n"] == "berlin_alex"
        elif plz == "80331":
            assert resolved_kaufland["n"] == "munich_center"
        elif plz == "20095":
            assert resolved_kaufland["n"] == "hamburg_altona"
        elif plz == "50667":
            assert resolved_kaufland["n"] == "cologne_ehrenfeld"

        # 3. REWE is always marked nationwide, never fake store-verified
        rp = ReweProvider()
        fake_article_html = """
        <article>
          <a data-offer-id="rewe-123" data-offer-title="REWE Bio Bananen" href="/rewe-123">
            <span class="cor-offer-information__title">REWE Bio Bananen</span>
          </a>
          <span class="cor-offer-price__regular-price">1,99 €</span>
        </article>
        """
        rewe_offers = rp.offers_from_include_contents(
            [fake_article_html],
            valid_from="2026-05-25",
            valid_to="2026-05-31",
            source_url="https://www.rewe.de/angebote/nationale-angebote/",
            limit=5,
            requested_zip_code=plz,
        )
        assert len(rewe_offers) == 1
        assert rewe_offers[0].scope_type == "nationwide"
        assert rewe_offers[0].region_verified is False
        assert rewe_offers[0].store_name == "REWE (Nationale Angebote)"

