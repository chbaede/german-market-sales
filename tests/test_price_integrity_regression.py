from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import tempfile

import pytest

from app.models import Offer
from app.providers.aldi_nord import AldiNordProvider
from app.providers.aldi_sued import AldiSuedProvider
from app.providers.dm import DmProvider
from app.providers.edeka import EdekaProvider, _sort_candidate_markets
from app.providers.kaufland import KauflandProvider, _store_distance
from app.providers.marktguru import MarktguruProvider
from app.providers.rewe import ReweProvider
from app.services.location import (
    get_coordinates_for_plz,
    haversine_distance_km,
    is_valid_coordinates,
)
from app.services.offers import CACHE_SCHEMA_VERSION, OfferService
from app.services.price_history import (
    PriceHistoryService,
    get_geographic_identity,
    normalize_product_key,
)


# ---------------------------------------------------------------------------
# 1. PLZ and Store Selection Regression Tests
# ---------------------------------------------------------------------------


def test_plz_store_selection_four_cities_and_exact_match():
    provider = KauflandProvider()
    stores_pool = [
        {"n": "berlin_mitte", "pc": "10115", "lat": 52.53, "lng": 13.38, "cn": "Kaufland Berlin Mitte"},
        {"n": "munich_altstadt", "pc": "80331", "lat": 48.137, "lng": 11.575, "cn": "Kaufland München Altstadt"},
        {"n": "hamburg_hbf", "pc": "20095", "lat": 53.553, "lng": 10.007, "cn": "Kaufland Hamburg Hbf"},
        {"n": "cologne_innenstadt", "pc": "50667", "lat": 50.938, "lng": 6.953, "cn": "Kaufland Köln Innenstadt"},
    ]

    # Exact match for all 4 cities
    for plz, expected_id in [
        ("10115", "berlin_mitte"),
        ("80331", "munich_altstadt"),
        ("20095", "hamburg_hbf"),
        ("50667", "cologne_innenstadt"),
    ]:
        resolved = provider._resolve_store(plz, stores=stores_pool)
        assert resolved is not None
        assert resolved["n"] == expected_id

    # Spatial match when exact PLZ differs but within 60km
    # Berlin Kreuzberg 10969 -> should match Berlin Mitte 10115 (~3km away)
    resolved_kreuzberg = provider._resolve_store("10969", stores=stores_pool)
    assert resolved_kreuzberg is not None
    assert resolved_kreuzberg["n"] == "berlin_mitte"


def test_store_selection_missing_and_invalid_coordinates():
    # Test is_valid_coordinates validator
    assert is_valid_coordinates(52.52, 13.40) is True
    assert is_valid_coordinates(None, 13.40) is False
    assert is_valid_coordinates("invalid", "invalid") is False
    assert is_valid_coordinates(0.0, 0.0) is False  # Null Island
    assert is_valid_coordinates(95.0, 13.40) is False  # Out of range lat
    assert is_valid_coordinates(52.52, 210.0) is False  # Out of range lon

    # haversine_distance_km returns 99999.0 for invalid coordinates
    assert haversine_distance_km(None, 13.40, 52.52, 13.40) == 99999.0
    assert haversine_distance_km(0.0, 0.0, 52.52, 13.40) == 99999.0

    # Store with missing/invalid coords
    store_invalid = {"n": "store_bad", "pc": "99999", "lat": 0.0, "lng": 0.0}
    user_coords = get_coordinates_for_plz("10115")
    assert _store_distance(store_invalid, user_coords) == 99999.0


def test_store_selection_no_store_within_max_distance_and_no_store_data():
    kp = KauflandProvider()
    # Stores are in Munich (80331) and Stuttgart (70173), user is in Berlin (10115)
    stores_south_only = [
        {"n": "munich", "pc": "80331", "lat": 48.137, "lng": 11.575},
        {"n": "stuttgart", "pc": "70173", "lat": 48.775, "lng": 9.182},
    ]
    # No store within 60km -> must return None, not guess a southern store!
    assert kp._resolve_store("10115", stores=stores_south_only) is None

    # Empty store list -> returns None
    assert kp._resolve_store("10115", stores=[]) is None

    # EDEKA candidate sorting rejects markets > 60km
    edeka_remote_markets = [
        {"name": "Remote Edeka", "coordinates": {"lat": 48.137, "lon": 11.575}},  # Munich
    ]
    # For Berlin 10115, remote Munich market must be filtered out
    assert _sort_candidate_markets(edeka_remote_markets, "10115") == []


def test_store_and_offer_metadata_correspondence():
    kp = KauflandProvider()
    fake_payload = {
        "component": "OfferTemplate",
        "props": {
            "offerData": {
                "cycles": [
                    {
                        "categories": [
                            {
                                "name": "Obst & Gemüse",
                                "offers": [
                                    {
                                        "offerId": "KL-12345",
                                        "title": "Bio Äpfel",
                                        "price": "1.99",
                                        "formattedPrice": "1,99 €",
                                        "unit": "1 kg Packung",
                                        "dateFrom": "2026-01-01",
                                        "dateTo": "2026-12-31",
                                    }
                                ],
                            }
                        ]
                    }
                ]
            }
        },
    }

    # Case A: Store resolved
    offers_with_store = kp.offers_from_payload(
        fake_payload,
        limit=10,
        store_id="DE1110",
        store_name="Kaufland Berlin",
        store_address="Karl-Marx-Str. 1, 10115 Berlin",
        requested_zip="10115",
    )
    assert len(offers_with_store) == 1
    o1 = offers_with_store[0]
    assert o1.scope_type == "store"
    assert o1.store_id == "DE1110"
    assert o1.store_name == "Kaufland Berlin"
    assert o1.store_address == "Karl-Marx-Str. 1, 10115 Berlin"
    assert o1.region_verified is True
    assert o1.verification_method == "store_cookie_session"

    # Case B: No store resolved (fallback)
    offers_nationwide = kp.offers_from_payload(
        fake_payload,
        limit=10,
        store_id=None,
        store_name=None,
        store_address=None,
        requested_zip="10115",
    )
    assert len(offers_nationwide) == 1
    o2 = offers_nationwide[0]
    assert o2.scope_type == "nationwide"
    assert o2.store_id is None
    assert o2.store_name == "Kaufland (Nationale Angebote)"
    assert o2.region_verified is False
    assert o2.verification_method == "national_catalog_fallback"


# ---------------------------------------------------------------------------
# 2. Offer Scope Semantics Regression Tests
# ---------------------------------------------------------------------------


def test_offer_scope_types_and_verification_rules():
    # 1. Verified store offer
    store_offer = Offer(
        id="edeka-1",
        source_offer_id=1,
        retailer="EDEKA",
        retailer_slug="edeka",
        title="Milch",
        brand="G&G",
        category="dairy",
        price=1.09,
        old_price=None,
        unit_price=1.09,
        unit="l",
        discount_percent=None,
        description="",
        valid_from=None,
        valid_to=None,
        image_url=None,
        source_url="https://edeka.de",
        requested_zip_code="10115",
        scope_type="store",
        store_id="10001099",
        store_name="EDEKA Center Berlin",
        region_verified=True,
        verification_method="store_direct_api",
    )
    assert store_offer.scope_type == "store"
    assert store_offer.region_verified is True

    # 2. Retailer territory without offer-level regional evidence (ALDI Süd / Nord)
    aldi_sued_offer = Offer(
        id="aldi-sued-1",
        source_offer_id=2,
        retailer="ALDI Süd",
        retailer_slug="aldi-sued",
        title="Kaffee",
        brand=None,
        category="coffee",
        price=4.99,
        old_price=None,
        unit_price=9.98,
        unit="kg",
        discount_percent=None,
        description="",
        valid_from=None,
        valid_to=None,
        image_url=None,
        source_url="https://aldi-sued.de",
        requested_zip_code="80331",
        scope_type="region",
        source_region_id="aldi-sued-territory",
        region_verified=False,  # Territory alone does NOT make it region-verified!
        verification_method="retailer_territory_unverified",
    )
    assert aldi_sued_offer.scope_type == "region"
    assert aldi_sued_offer.region_verified is False

    # 3. Nationwide offer (REWE / dm)
    rewe_offer = Offer(
        id="rewe-1",
        source_offer_id=3,
        retailer="REWE",
        retailer_slug="rewe",
        title="Bananen",
        brand=None,
        category="produce",
        price=1.99,
        old_price=None,
        unit_price=1.99,
        unit="kg",
        discount_percent=None,
        description="",
        valid_from=None,
        valid_to=None,
        image_url=None,
        source_url="https://rewe.de/angebote/nationale-angebote/",
        requested_zip_code="10115",
        scope_type="nationwide",
        region_verified=False,
        verification_method="national_catalog",
    )
    assert rewe_offer.scope_type == "nationwide"
    assert rewe_offer.region_verified is False

    # 4. Unknown-scope offer
    unknown_offer = Offer(
        id="legacy-1",
        source_offer_id=4,
        retailer="Penny",
        retailer_slug="penny",
        title="Butter",
        brand=None,
        category="dairy",
        price=1.49,
        old_price=None,
        unit_price=5.96,
        unit="kg",
        discount_percent=None,
        description="",
        valid_from=None,
        valid_to=None,
        image_url=None,
        source_url="https://penny.de",
        scope_type="unknown",
        region_verified=False,
    )
    assert unknown_offer.scope_type == "unknown"
    assert unknown_offer.region_verified is False


# ---------------------------------------------------------------------------
# 3. Price History Isolation and Aggregation Regression Tests
# ---------------------------------------------------------------------------


def test_price_history_geographic_identities_and_isolation():
    with tempfile.TemporaryDirectory() as tmp_dir:
        service = PriceHistoryService(Path(tmp_dir))

        # 1. Same product at different prices in Berlin (10115) and Munich (80331)
        butter_berlin = Offer(
            id="butter-b",
            source_offer_id=1,
            retailer="EDEKA",
            retailer_slug="edeka",
            title="Kerrygold Butter 250g",
            brand="Kerrygold",
            category="dairy",
            price=1.99,
            old_price=3.29,
            unit_price=7.96,
            unit="kg",
            discount_percent=39.5,
            description="250g",
            valid_from="2026-06-01",
            valid_to="2026-06-07",
            image_url=None,
            source_url="https://edeka.de",
            requested_zip_code="10115",
            scope_type="store",
            store_id="edeka_berlin_10115",
            region_verified=True,
        )
        butter_munich = Offer(
            id="butter-m",
            source_offer_id=2,
            retailer="EDEKA",
            retailer_slug="edeka",
            title="Kerrygold Butter 250g",
            brand="Kerrygold",
            category="dairy",
            price=1.49,  # Cheaper in Munich
            old_price=3.29,
            unit_price=5.96,
            unit="kg",
            discount_percent=54.7,
            description="250g",
            valid_from="2026-06-01",
            valid_to="2026-06-07",
            image_url=None,
            source_url="https://edeka.de",
            requested_zip_code="80331",
            scope_type="store",
            store_id="edeka_munich_80331",
            region_verified=True,
        )

        service.record_offers([butter_berlin], zip_code="10115", recorded_at="2026-06-01T10:00:00")
        service.record_offers([butter_munich], zip_code="80331", recorded_at="2026-06-01T10:00:00")

        pkey = normalize_product_key("Kerrygold Butter 250g", "Kerrygold")
        # Berlin ATL must strictly be 1.99, not Munich's 1.49
        assert service.get_all_time_low_map(zip_code="10115")[pkey] == 1.99
        assert service.get_all_time_low_map(zip_code="80331")[pkey] == 1.49
        # National ATL is the minimum across all
        assert service.get_all_time_low_map(zip_code=None)[pkey] == 1.49


def test_price_history_two_stores_in_same_plz_and_repeated_fetch():
    with tempfile.TemporaryDirectory() as tmp_dir:
        service = PriceHistoryService(Path(tmp_dir))

        # 2. Same PLZ (10115) with two different stores
        store_a_offer = Offer(
            id="edeka-store-a",
            source_offer_id=10,
            retailer="EDEKA",
            retailer_slug="edeka",
            title="Vollmilch 1l",
            brand=None,
            category="dairy",
            price=1.19,
            old_price=None,
            unit_price=1.19,
            unit="l",
            discount_percent=None,
            description="",
            valid_from=None,
            valid_to=None,
            image_url=None,
            source_url="https://edeka.de/storeA",
            requested_zip_code="10115",
            scope_type="store",
            store_id="STORE_A",
            region_verified=True,
        )
        store_b_offer = Offer(
            id="edeka-store-b",
            source_offer_id=20,
            retailer="EDEKA",
            retailer_slug="edeka",
            title="Vollmilch 1l",
            brand=None,
            category="dairy",
            price=0.99,
            old_price=None,
            unit_price=0.99,
            unit="l",
            discount_percent=None,
            description="",
            valid_from=None,
            valid_to=None,
            image_url=None,
            source_url="https://edeka.de/storeB",
            requested_zip_code="10115",
            scope_type="store",
            store_id="STORE_B",
            region_verified=True,
        )

        # Record both stores in the same PLZ
        inserted = service.record_offers([store_a_offer, store_b_offer], zip_code="10115", recorded_at="2026-06-01T10:00:00")
        assert inserted == 2

        # Repeated fetch of the EXACT same offers on the same day -> 0 inserted (deduped)
        repeated = service.record_offers([store_a_offer, store_b_offer], zip_code="10115", recorded_at="2026-06-01T10:05:00")
        assert repeated == 0

        # Query ATL by store_id
        pkey = normalize_product_key("Vollmilch 1l")
        assert service.get_all_time_low_map(zip_code="10115", store_id="STORE_A")[pkey] == 1.19
        assert service.get_all_time_low_map(zip_code="10115", store_id="STORE_B")[pkey] == 0.99


def test_nationwide_offers_recorded_uniformly_without_duplication():
    with tempfile.TemporaryDirectory() as tmp_dir:
        service = PriceHistoryService(Path(tmp_dir))

        rewe_national = Offer(
            id="rewe-nat-1",
            source_offer_id=101,
            retailer="REWE",
            retailer_slug="rewe",
            title="Bio Gurken",
            brand=None,
            category="produce",
            price=0.89,
            old_price=1.29,
            unit_price=0.89,
            unit="stk",
            discount_percent=31.0,
            description="",
            valid_from="2026-06-01",
            valid_to="2026-06-07",
            image_url=None,
            source_url="https://rewe.de/angebote/nationale-angebote/",
            scope_type="nationwide",
            region_verified=False,
            verification_method="national_catalog",
        )

        # Fetched from Berlin (10115)
        ins1 = service.record_offers([rewe_national], zip_code="10115", recorded_at="2026-06-01T10:00:00")
        assert ins1 == 1

        # Fetched from Munich (80331) on the same day -> should NOT create a duplicate row!
        ins2 = service.record_offers([rewe_national], zip_code="80331", recorded_at="2026-06-01T10:10:00")
        assert ins2 == 0

        # Check in DB that zip_code is "DE"
        with service._get_connection() as conn:
            row = conn.execute("SELECT zip_code, scope_type FROM price_history WHERE id = 1").fetchone()
            assert row["zip_code"] == "DE"
            assert row["scope_type"] == "nationwide"


def test_unknown_scope_excluded_from_verified_regional_atl():
    with tempfile.TemporaryDirectory() as tmp_dir:
        service = PriceHistoryService(Path(tmp_dir))

        # Unknown scope offer with a super cheap fake price (0.10 EUR)
        unknown_offer = Offer(
            id="unknown-1",
            source_offer_id=999,
            retailer="Netto",
            retailer_slug="netto",
            title="Olivenoel 500ml",
            brand=None,
            category="oil",
            price=0.10,
            old_price=None,
            unit_price=0.20,
            unit="l",
            discount_percent=None,
            description="",
            valid_from=None,
            valid_to=None,
            image_url=None,
            source_url="https://netto.de",
            requested_zip_code="10115",
            scope_type="unknown",
            region_verified=False,
        )

        # Verified store offer at realistic price (4.99 EUR)
        verified_offer = Offer(
            id="verified-1",
            source_offer_id=888,
            retailer="EDEKA",
            retailer_slug="edeka",
            title="Olivenoel 500ml",
            brand=None,
            category="oil",
            price=4.99,
            old_price=None,
            unit_price=9.98,
            unit="l",
            discount_percent=None,
            description="",
            valid_from=None,
            valid_to=None,
            image_url=None,
            source_url="https://edeka.de",
            requested_zip_code="10115",
            scope_type="store",
            store_id="DE10115",
            region_verified=True,
        )

        service.record_offers([unknown_offer, verified_offer], zip_code="10115", recorded_at="2026-06-01T10:00:00")

        pkey = normalize_product_key("Olivenoel 500ml")
        # Verified regional ATL must NOT include the 0.10 EUR unknown observation!
        reg_map = service.get_all_time_low_map(zip_code="10115")
        assert reg_map[pkey] == 4.99


def test_legacy_database_migration_preserves_history_without_inventing_verification():
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = Path(tmp_dir) / "price_history.db"

        # Create old schema table without scope_type, region_verified, etc.
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            """
            CREATE TABLE price_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                product_key TEXT NOT NULL,
                title TEXT NOT NULL,
                brand TEXT,
                category TEXT NOT NULL,
                retailer_slug TEXT NOT NULL,
                retailer_name TEXT NOT NULL,
                price REAL NOT NULL,
                old_price REAL,
                unit_price REAL,
                unit TEXT,
                discount_percent REAL,
                recorded_at TEXT NOT NULL,
                valid_from TEXT,
                valid_to TEXT,
                source_url TEXT NOT NULL,
                zip_code TEXT
            )
            """
        )
        conn.execute(
            """
            INSERT INTO price_history (
                product_key, title, category, retailer_slug, retailer_name, price, recorded_at, source_url, zip_code
            ) VALUES ('apfel-1kg', 'Apfel 1kg', 'produce', 'edeka', 'EDEKA', 1.99, '2026-01-01T10:00:00', 'https://edeka.de', '10115')
            """
        )
        conn.commit()
        conn.close()

        # Initialize PriceHistoryService -> runs migration on old table
        service = PriceHistoryService(Path(tmp_dir))

        with service._get_connection() as c:
            row = c.execute("SELECT scope_type, region_verified FROM price_history WHERE id = 1").fetchone()
            # Must default to unknown and 0, NOT verified
            assert row["scope_type"] == "unknown"
            assert row["region_verified"] == 0

        # Legacy row must not be counted in regional verified ATL
        assert "apfel-1kg" not in service.get_all_time_low_map(zip_code="10115")
        # But counted in national general history
        assert service.get_all_time_low_map(zip_code=None)["apfel-1kg"] == 1.99


# ---------------------------------------------------------------------------
# 4. Cache Migration and Isolation Regression Tests
# ---------------------------------------------------------------------------


def test_cache_migration_from_schema_11_and_12():
    with tempfile.TemporaryDirectory() as tmp_dir:
        cache_path = Path(tmp_dir)
        service = OfferService(
            cache_dir=cache_path,
            retailers={"rewe": "REWE", "edeka": "EDEKA"},
            cache_ttl_seconds=3600,
            timeout=5,
            verify_tls=True,
            max_offers_per_retailer=10,
        )

        # Write a legacy cache file (schema_version = 11, without scope_type or region_verified)
        legacy_cache_file = cache_path / "offers_10115.json"
        legacy_data = {
            "schema_version": 11,
            "zip_code": "10115",
            "fetched_at": "2026-06-01T10:00:00",
            "warnings": ["Legacy note"],
            "offers": [
                {
                    "id": "rewe-legacy-1",
                    "source_offer_id": 1,
                    "retailer": "REWE",
                    "retailer_slug": "rewe",
                    "title": "REWE Bio Milch",
                    "brand": None,
                    "category": "dairy",
                    "price": 1.15,
                    "old_price": None,
                    "unit_price": 1.15,
                    "unit": "l",
                    "discount_percent": None,
                    "description": "",
                    "valid_from": None,
                    "valid_to": None,
                    "image_url": None,
                    "source_url": "https://rewe.de",
                },
                {
                    "id": "edeka-legacy-1",
                    "source_offer_id": 2,
                    "retailer": "EDEKA",
                    "retailer_slug": "edeka",
                    "title": "EDEKA Kaffee",
                    "brand": None,
                    "category": "coffee",
                    "price": 3.99,
                    "old_price": None,
                    "unit_price": 7.98,
                    "unit": "kg",
                    "discount_percent": None,
                    "description": "",
                    "valid_from": None,
                    "valid_to": None,
                    "image_url": None,
                    "source_url": "https://edeka.de",
                },
            ],
        }
        with open(legacy_cache_file, "w", encoding="utf-8") as f:
            json.dump(legacy_data, f)

        # Reading cache should migrate it safely to CACHE_SCHEMA_VERSION = 13
        result = service.get_cached_offers("10115")
        assert result is not None
        assert len(result.offers) == 2

        rewe_migrated = next(o for o in result.offers if o.retailer_slug == "rewe")
        edeka_migrated = next(o for o in result.offers if o.retailer_slug == "edeka")

        # REWE is nationwide, but NOT region-verified
        assert rewe_migrated.scope_type == "nationwide"
        assert rewe_migrated.region_verified is False

        # EDEKA from legacy is unknown, NOT region-verified
        assert edeka_migrated.scope_type == "unknown"
        assert edeka_migrated.region_verified is False

        # File on disk should now be schema 13
        with open(legacy_cache_file, "r", encoding="utf-8") as f:
            disk_data = json.load(f)
            assert disk_data["schema_version"] == CACHE_SCHEMA_VERSION
