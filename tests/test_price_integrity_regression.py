from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import tempfile

import pytest

from app import create_app
from app.models import Offer, OfferResult
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

        # Legacy row must not be counted in regional or national verified ATL
        assert "apfel-1kg" not in service.get_all_time_low_map(zip_code="10115")
        assert "apfel-1kg" not in service.get_all_time_low_map(zip_code=None)

        # But row is preserved in raw database
        with service._get_connection() as c:
            raw_row = c.execute("SELECT price FROM price_history WHERE product_key = 'apfel-1kg'").fetchone()
            assert raw_row["price"] == 1.99


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

        # File on disk should now be schema 14
        with open(legacy_cache_file, "r", encoding="utf-8") as f:
            disk_data = json.load(f)
            assert disk_data["schema_version"] == CACHE_SCHEMA_VERSION


# ---------------------------------------------------------------------------
# 5. Targeted Fail-Closed & Scope Integrity Regression Tests
# ---------------------------------------------------------------------------


def test_edeka_fail_closed_rejections_and_missing_store_identity():
    # 1. Candidate with missing postal code (and no coords/distance) -> rejected
    cand_no_pc = [{"id": "m1", "name": "No PC Market"}]
    assert _sort_candidate_markets(cand_no_pc, "10115") == []

    # 2. Candidate with missing coordinates (and no pc/distance) -> rejected
    cand_no_coords = [{"id": "m2", "name": "No Coords", "coordinates": {}}]
    assert _sort_candidate_markets(cand_no_coords, "10115") == []

    # 3. Candidate with missing store ID -> rejected
    cand_no_id = [{"name": "No ID Market", "contact": {"address": {"postalCode": "10115"}}}]
    assert _sort_candidate_markets(cand_no_id, "10115") == []

    # 4. Candidate with invalid coordinates -> rejected
    cand_invalid_coords = [
        {"id": "m3", "name": "Null Island", "coordinates": {"lat": 0.0, "lon": 0.0}},
        {"id": "m4", "name": "Bad Lat", "coordinates": {"lat": 95.0, "lon": 13.4}},
    ]
    assert _sort_candidate_markets(cand_invalid_coords, "10115") == []

    # 5. No valid candidates in candidate list -> returns empty list
    assert _sort_candidate_markets([], "10115") == []

    # 6. EDEKA _offer_from_dialog missing store identity -> scope_type="unknown", region_verified=False
    from bs4 import BeautifulSoup
    dialog_html = """
    <dialog id="dialog-angebot-test1">
      <h3><span>Angebot:</span> Test Butter</h3>
      <div class="sr-only">Festpreis von 1.49 €</div>
    </dialog>
    """
    soup = BeautifulSoup(dialog_html, "html.parser")
    dialog = soup.find("dialog")

    offer_no_store = EdekaProvider._offer_from_dialog(
        dialog,
        fallback_from="2026-06-01",
        fallback_to="2026-06-07",
        source_url="https://edeka.de",
        market_name=None,
        store_id=None,
    )
    assert offer_no_store is not None
    assert offer_no_store.scope_type == "unknown"
    assert offer_no_store.region_verified is False
    assert offer_no_store.verification_method == "unverified_missing_store_id"

    # With store identity -> scope_type="store", region_verified=True
    offer_with_store = EdekaProvider._offer_from_dialog(
        dialog,
        fallback_from="2026-06-01",
        fallback_to="2026-06-07",
        source_url="https://edeka.de",
        market_name="EDEKA Mitte",
        store_id="801006",
    )
    assert offer_with_store is not None
    assert offer_with_store.scope_type == "store"
    assert offer_with_store.region_verified is True
    assert offer_with_store.store_id == "801006"


def test_marktguru_verification_logic_for_national_regional_branch_and_unknown():
    # 1. National flyer: explicitly national
    item_national = {
        "id": 101,
        "product": {"name": "National Butter"},
        "price": 1.29,
        "flyer": {"id": 999, "isNational": True},
    }
    offer_nat = MarktguruProvider._offer_from_api(item_national, "edeka", zip_code="10115")
    assert offer_nat.scope_type == "nationwide"
    assert offer_nat.region_verified is True
    assert offer_nat.source_region_id is None
    assert offer_nat.verification_method == "marktguru_national_flyer"

    # 2. Regional flyer with matching postal code
    item_reg_plz = {
        "id": 102,
        "product": {"name": "Regional Milk"},
        "price": 0.99,
        "flyer": {"id": 888, "postalCodes": ["10115", "10117"], "regionId": 42},
    }
    offer_reg = MarktguruProvider._offer_from_api(item_reg_plz, "edeka", zip_code="10115")
    assert offer_reg.scope_type == "region"
    assert offer_reg.region_verified is True
    assert offer_reg.source_region_id == "42"
    assert offer_reg.verification_method == "marktguru_regional_flyer_postal_match"

    # 3. Regional flyer with geographic regionId matching
    item_reg_id = {
        "id": 103,
        "product": {"name": "Regional Cheese"},
        "price": 2.49,
        "flyer": {"id": 777, "regionId": "berlin_north"},
    }
    offer_reg_id = MarktguruProvider._offer_from_api(item_reg_id, "edeka", zip_code="10115")
    assert offer_reg_id.scope_type == "region"
    assert offer_reg_id.region_verified is True
    assert offer_reg_id.source_region_id == "berlin_north"

    # 4. Flyer without geographic proof (only a flyerId, no postalCode match or regionId)
    # Must NOT invent region ID from flyerId!
    item_unverified_flyer = {
        "id": 104,
        "product": {"name": "Unverified Leaflet Item"},
        "price": 3.99,
        "flyer": {"id": 555},  # Only arbitrary flyerId
    }
    offer_unv_flyer = MarktguruProvider._offer_from_api(item_unverified_flyer, "edeka", zip_code="10115")
    assert offer_unv_flyer.scope_type == "region"
    assert offer_unv_flyer.region_verified is False
    assert offer_unv_flyer.source_region_id is None
    assert offer_unv_flyer.verification_method == "marktguru_unverified_flyer"

    # 5. Branch with matching postal code
    item_branch_match = {
        "id": 105,
        "product": {"name": "Branch Coffee"},
        "price": 4.99,
        "branch": {"id": 1234, "name": "Filiale Mitte", "postalCode": "10115"},
    }
    offer_branch = MarktguruProvider._offer_from_api(item_branch_match, "edeka", zip_code="10115")
    assert offer_branch.scope_type == "store"
    assert offer_branch.region_verified is True
    assert offer_branch.store_id == "1234"
    assert offer_branch.store_name == "Filiale Mitte"

    # 6. Branch with mismatched postal code and no nearby distance
    item_branch_mismatch = {
        "id": 106,
        "product": {"name": "Munich Branch Coffee"},
        "price": 4.99,
        "branch": {"id": 5678, "name": "Filiale München", "postalCode": "80331"},
    }
    offer_branch_mismatch = MarktguruProvider._offer_from_api(item_branch_mismatch, "edeka", zip_code="10115")
    assert offer_branch_mismatch.scope_type == "store"
    assert offer_branch_mismatch.region_verified is False
    assert offer_branch_mismatch.verification_method == "marktguru_unverified_branch"

    # 7. Item with no branch and no flyer
    item_no_geo = {
        "id": 107,
        "product": {"name": "Unknown Item"},
        "price": 1.00,
    }
    offer_no_geo = MarktguruProvider._offer_from_api(item_no_geo, "edeka", zip_code="10115")
    assert offer_no_geo.scope_type == "unknown"
    assert offer_no_geo.region_verified is False
    assert offer_no_geo.verification_method == "unknown_scope"


def test_price_history_isolation_mixed_verified_unverified_and_multi_region():
    with tempfile.TemporaryDirectory() as tmp_dir:
        service = PriceHistoryService(Path(tmp_dir))

        # Setup 4 offers for the same product "Kaffee 500g":
        # 1. Store in Berlin 10115 (verified, price 3.49)
        # 2. Store in Munich 80331 (verified, price 2.99)
        # 3. Nationwide offer (verified, price 3.99)
        # 4. Unknown/unverified offer in Berlin 10115 (unverified, fake low price 0.50)

        offer_berlin = Offer(
            id="berlin-kaffee",
            source_offer_id=1,
            retailer="EDEKA",
            retailer_slug="edeka",
            title="Kaffee 500g",
            brand=None,
            category="coffee",
            price=3.49,
            old_price=None,
            unit_price=6.98,
            unit="kg",
            discount_percent=None,
            description="",
            valid_from=None,
            valid_to=None,
            image_url=None,
            source_url="https://edeka.de",
            requested_zip_code="10115",
            scope_type="store",
            store_id="store_b1",
            store_name="EDEKA Berlin Mitte",
            region_verified=True,
        )

        offer_munich = Offer(
            id="munich-kaffee",
            source_offer_id=2,
            retailer="EDEKA",
            retailer_slug="edeka",
            title="Kaffee 500g",
            brand=None,
            category="coffee",
            price=2.99,
            old_price=None,
            unit_price=5.98,
            unit="kg",
            discount_percent=None,
            description="",
            valid_from=None,
            valid_to=None,
            image_url=None,
            source_url="https://edeka.de",
            requested_zip_code="80331",
            scope_type="store",
            store_id="store_m1",
            store_name="EDEKA Munich Altstadt",
            region_verified=True,
        )

        offer_nationwide = Offer(
            id="nat-kaffee",
            source_offer_id=3,
            retailer="REWE",
            retailer_slug="rewe",
            title="Kaffee 500g",
            brand=None,
            category="coffee",
            price=3.99,
            old_price=None,
            unit_price=7.98,
            unit="kg",
            discount_percent=None,
            description="",
            valid_from=None,
            valid_to=None,
            image_url=None,
            source_url="https://rewe.de",
            scope_type="nationwide",
            region_verified=False,
        )

        offer_unverified = Offer(
            id="unv-kaffee",
            source_offer_id=4,
            retailer="Unknown",
            retailer_slug="unknown",
            title="Kaffee 500g",
            brand=None,
            category="coffee",
            price=0.50,
            old_price=None,
            unit_price=1.00,
            unit="kg",
            discount_percent=None,
            description="",
            valid_from=None,
            valid_to=None,
            image_url=None,
            source_url="https://example.com",
            requested_zip_code="10115",
            scope_type="unknown",
            region_verified=False,
        )

        service.record_offers(
            [offer_berlin, offer_munich, offer_nationwide, offer_unverified],
            zip_code="10115",
            recorded_at="2026-06-01T10:00:00",
        )

        pkey = normalize_product_key("Kaffee 500g")

        # 1. Berlin regional ATL:
        # Includes Berlin store (3.49) and Nationwide (3.99).
        # MUST EXCLUDE Munich store (2.99) and Unverified fake (0.50).
        berlin_atl = service.get_all_time_low_map(zip_code="10115")
        assert berlin_atl[pkey] == 3.49

        # 2. Munich regional ATL:
        # Includes Munich store (2.99) and Nationwide (3.99).
        # MUST EXCLUDE Berlin store (3.49) and Unverified (0.50).
        munich_atl = service.get_all_time_low_map(zip_code="80331")
        assert munich_atl[pkey] == 2.99

        # 3. National verified ATL:
        # Includes verified observations (Munich 2.99, Berlin 3.49, Nationwide 3.99).
        # MUST EXCLUDE unverified (0.50).
        national_atl = service.get_all_time_low_map(zip_code=None)
        assert national_atl[pkey] == 2.99

        # 4. Product stats for Berlin user:
        berlin_stats = service.get_product_stats("Kaffee 500g", current_price=3.49, zip_code="10115")
        assert berlin_stats["regional_all_time_low"] == 3.49
        assert berlin_stats["national_all_time_low"] == 2.99

        # History points for Berlin user:
        # Must ONLY contain observations for Berlin or Nationwide.
        # MUST NOT contain Munich or unverified observations!
        history_stores = [h["store_name"] for h in berlin_stats["history"]]
        assert "EDEKA Berlin Mitte" in history_stores
        assert "EDEKA Munich Altstadt" not in history_stores
        assert all(h["price"] >= 2.99 for h in berlin_stats["history"])
        assert not any(h["price"] == 0.50 for h in berlin_stats["history"])


def test_cache_migration_forces_legacy_unverified_even_with_store_or_region_scope():
    with tempfile.TemporaryDirectory() as tmp_dir:
        cache_path = Path(tmp_dir)
        service = OfferService(
            cache_dir=cache_path,
            retailers={"edeka": "EDEKA"},
            cache_ttl_seconds=3600,
            timeout=5,
            verify_tls=True,
            max_offers_per_retailer=10,
        )

        legacy_cache_file = cache_path / "offers_10115.json"
        # Legacy cache that claimed to have store scope and region_verified=True
        legacy_data = {
            "schema_version": 12,
            "zip_code": "10115",
            "fetched_at": "2026-06-01T10:00:00",
            "warnings": [],
            "offers": [
                {
                    "id": "edeka-old-claimed-verified",
                    "source_offer_id": 99,
                    "retailer": "EDEKA",
                    "retailer_slug": "edeka",
                    "title": "Legacy Claimed Verified Butter",
                    "brand": None,
                    "category": "dairy",
                    "price": 1.79,
                    "old_price": None,
                    "unit_price": 7.16,
                    "unit": "kg",
                    "discount_percent": None,
                    "description": "",
                    "valid_from": None,
                    "valid_to": None,
                    "image_url": None,
                    "source_url": "https://edeka.de",
                    "scope_type": "store",
                    "region_verified": True,
                }
            ],
        }
        with open(legacy_cache_file, "w", encoding="utf-8") as f:
            json.dump(legacy_data, f)

        result = service.get_cached_offers("10115")
        assert result is not None
        assert len(result.offers) == 1
        migrated = result.offers[0]

        # Must be demoted to region_verified=False upon cache migration!
        assert migrated.region_verified is False
        assert migrated.verification_method == "legacy_cache_migration"


def test_ui_unverified_store_badge_rendering(monkeypatch):
    import app.routes as routes

    from datetime import datetime as real_datetime

    class FrozenDateTime(real_datetime):
        @classmethod
        def now(cls, tz=None):
            return real_datetime(2026, 6, 4, tzinfo=tz)

    unverified_store_offer = Offer(
        id="edeka-unv-store",
        source_offer_id=1,
        retailer="EDEKA",
        retailer_slug="edeka",
        title="Unverified Store Apple",
        brand=None,
        category="produce",
        price=1.99,
        old_price=None,
        unit_price=1.99,
        unit="kg",
        discount_percent=None,
        description="",
        valid_from="2026-06-01T00:00:00+02:00",
        valid_to="2026-06-07T23:59:59+02:00",
        image_url=None,
        source_url="https://edeka.de",
        requested_zip_code="10115",
        scope_type="store",
        store_name="EDEKA Unverified Store",
        region_verified=False,
        verification_method="unverified_missing_coords",
    )

    class FakeOfferService:
        def get_offers(self, zip_code: str, refresh: bool = False) -> OfferResult:
            return OfferResult(
                offers=[unverified_store_offer],
                fetched_at="2026-06-04T08:00:00+00:00",
                warnings=[],
            )

    monkeypatch.setattr(routes, "datetime", FrozenDateTime)
    monkeypatch.setattr(routes, "offer_service", lambda: FakeOfferService())
    app = create_app()
    response = app.test_client().get("/?zip=10115")
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    # Must contain unverified badge
    assert "scope-unverified" in html
    assert "미검증" in html
    # Must NOT contain verified store badge
    assert "scope-badge scope-store" not in html

