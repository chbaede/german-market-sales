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
from app.providers.marktguru import (
    MarktguruProvider,
    NationwideCoverageEvidence,
    RegionalCoverageEvidence,
    clear_campaign_registries,
    get_verified_campaign_plzs,
    get_verified_nationwide_evidence,
    get_verified_campaign_evidence,
    is_verified_nationwide_campaign,
    register_verified_campaign,
    register_verified_nationwide_campaign,
    register_approved_evidence_root,
    ingest_trusted_evidence_artifact,
    clear_dynamic_ingestion_catalog,
    clear_dynamic_approved_roots,
    VERIFIED_CAMPAIGN_REGISTRY,
    VERIFIED_NATIONWIDE_REGISTRY,
)
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
    validate_observation_provenance,
    build_store_verification_evidence,
)

FIXTURE_EVIDENCE_DIR = Path(__file__).parent / "fixtures" / "evidence"


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
            store_address="Invalidenstraße 100, 10115 Berlin",
            source_provider="edeka-official",
            region_verified=True,
            verification_method="store_direct_api",
            verification_evidence=build_store_verification_evidence(
                store_id="edeka_berlin_10115",
                retailer_slug="edeka",
                store_address="Invalidenstraße 100, 10115 Berlin",
                requested_plz="10115",
                store_name="EDEKA Berlin",
                metadata_source="edeka_official_market_api",
                verification_method="store_direct_api",
            ),
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
            store_address="Marienplatz 1, 80331 München",
            source_provider="edeka-official",
            region_verified=True,
            verification_method="store_direct_api",
            verification_evidence=build_store_verification_evidence(
                store_id="edeka_munich_80331",
                retailer_slug="edeka",
                store_address="Marienplatz 1, 80331 München",
                requested_plz="80331",
                store_name="EDEKA Munich",
                metadata_source="edeka_official_market_api",
                verification_method="store_direct_api",
            ),
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
            store_address="Invalidenstr. 10, 10115 Berlin",
            source_provider="edeka-official",
            region_verified=True,
            verification_method="store_direct_api",
            verification_evidence=build_store_verification_evidence(
                store_id="STORE_A",
                retailer_slug="edeka",
                store_address="Invalidenstr. 10, 10115 Berlin",
                requested_plz="10115",
                store_name="EDEKA Store A",
                metadata_source="edeka_official_market_api",
                verification_method="store_direct_api",
            ),
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
            store_address="Torstr. 20, 10115 Berlin",
            source_provider="edeka-official",
            region_verified=True,
            verification_method="store_direct_api",
            verification_evidence=build_store_verification_evidence(
                store_id="STORE_B",
                retailer_slug="edeka",
                store_address="Torstr. 20, 10115 Berlin",
                requested_plz="10115",
                store_name="EDEKA Store B",
                metadata_source="edeka_official_market_api",
                verification_method="store_direct_api",
            ),
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
            store_address="Invalidenstr. 10, 10115 Berlin",
            source_provider="edeka-official",
            region_verified=True,
            verification_method="store_direct_api",
            verification_evidence=build_store_verification_evidence(
                store_id="DE10115",
                retailer_slug="edeka",
                store_address="Invalidenstr. 10, 10115 Berlin",
                requested_plz="10115",
                store_name="EDEKA Berlin",
                metadata_source="edeka_official_market_api",
                verification_method="store_direct_api",
            ),
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


def test_legacy_database_revalidation_demotes_unsubstantiated_records():
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = Path(tmp_dir) / "price_history.db"

        # Create schema with verification fields and insert rows with mixed provenance claiming region_verified = 1
        register_verified_nationwide_campaign(
            "nat_proven_999",
            {
                "campaign_id": "nat_proven_999",
                "source_url": "https://www.marktguru.de/leaflets/nat_proven_999",
                "evidence_type": "verified_nationwide_distribution_manifest",
                "verification_date": "2026-06-01",
                "scope": "nationwide",
                "promotional_applicability_confirmed": True,
                "limitations": "Audited nationwide campaign",
                "evidence_artifact_path": str(FIXTURE_EVIDENCE_DIR / "nat_proven_999.json"),
            },
        )

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
                zip_code TEXT,
                scope_type TEXT,
                store_id TEXT,
                store_name TEXT,
                store_address TEXT,
                region_verified INTEGER DEFAULT 0,
                source_provider TEXT,
                source_region_id TEXT,
                verification_method TEXT,
                verification_evidence TEXT
            )
            """
        )
        row_5_ev = json.dumps(
            build_store_verification_evidence(
                store_id="edeka_berlin_1",
                retailer_slug="edeka",
                store_address="Torstr. 1, 10115 Berlin",
                requested_plz="10115",
                store_name="EDEKA Berlin",
                metadata_source="edeka_official_market_api",
                verification_method="store_direct_api",
            )
        )
        row_12_ev = json.dumps(
            build_store_verification_evidence(
                store_id="edeka_prenzlauer",
                retailer_slug="edeka",
                store_address="Karl-Marx-Str. 1, 10407 Berlin",
                requested_plz="10407",
                store_name="EDEKA Prenzlauer Berg",
                metadata_source="edeka_official_market_api",
                verification_method="store_direct_api",
            )
        )
        conn.executemany(
            """
            INSERT INTO price_history (
                id, product_key, title, category, retailer_slug, retailer_name, price, recorded_at,
                source_url, zip_code, scope_type, store_id, store_name, store_address, region_verified,
                source_provider, source_region_id, verification_method, verification_evidence
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                # Row 1: Claims region_verified = 1, but verification_method is 'legacy_unverified' -> demote to 0
                (1, "kaffee", "Kaffee", "coffee", "penny", "Penny", 1.99, "2026-01-01T10:00:00", "https://penny.de", "10115", "store", "penny_1", "Penny Mitte", None, 1, "penny", None, "legacy_unverified", None),
                # Row 2: Claims region_verified = 1, but verification_method is NULL -> demote to 0
                (2, "kaffee", "Kaffee", "coffee", "penny", "Penny", 1.89, "2026-01-01T10:00:00", "https://penny.de", "10115", "store", "penny_1", "Penny Mitte", None, 1, "penny", None, None, None),
                # Row 3: Claims region_verified = 1 for store, but lacks store_id (even if store_name exists) -> demote to 0
                (3, "kaffee", "Kaffee", "coffee", "edeka", "EDEKA", 1.79, "2026-01-01T10:00:00", "https://edeka.de", "10115", "store", None, "EDEKA Mitte", None, 1, "edeka-official", None, "store_direct_api", None),
                # Row 4: Claims nationwide with unverified online catalog method -> demote to 0
                (4, "kaffee", "Kaffee", "coffee", "rewe", "REWE", 2.29, "2026-01-01T10:00:00", "https://rewe.de", "DE", "nationwide", None, None, None, 1, "rewe-official", "national_flyer:1", "national_catalog", None),
                # Row 5: Legitimate verified store offer with store_direct_api, store_id, matching 5-digit PLZ and authoritative store evidence -> KEEP verified = 1
                (5, "kaffee", "Kaffee", "coffee", "edeka", "EDEKA", 2.49, "2026-01-01T10:00:00", "https://edeka.de", "10115", "store", "edeka_berlin_1", "EDEKA Berlin", "Torstr. 1, 10115 Berlin", 1, "edeka-official", None, "store_direct_api", row_5_ev),
                # Row 6: Claims nationwide with national_flyer:555, but 555 lacks scope-specific evidence in registry -> demote to 0
                (6, "kaffee", "Kaffee", "coffee", "rewe", "REWE", 2.99, "2026-01-01T10:00:00", "https://www.marktguru.de/r/rewe", "DE", "nationwide", None, None, None, 1, "marktguru", "national_flyer:555", "marktguru_national_flyer", None),
                # Row 7: Claims nationwide for territory-split ALDI -> demote to 0
                (7, "kaffee", "Kaffee", "coffee", "aldi-sued", "ALDI Süd", 1.59, "2026-01-01T10:00:00", "https://www.marktguru.de/r/aldi-sued", "DE", "nationwide", None, None, None, 1, "marktguru", "national_flyer:556", "marktguru_national_flyer", None),
                # Row 8: Claims region with unsupported 'berlin_north' mapping -> demote to 0
                (8, "kaffee", "Kaffee", "coffee", "edeka", "EDEKA", 1.49, "2026-01-01T10:00:00", "https://www.marktguru.de/r/edeka", "10115", "region", None, None, None, 1, "marktguru", "berlin_north", "marktguru_authoritative_region_mapping", None),
                # Row 9: Claims region with 'rossmann_catalog_match' -> demote to 0
                (9, "kaffee", "Kaffee", "coffee", "rossmann", "ROSSMANN", 1.39, "2026-01-01T10:00:00", "https://www.rossmann.de", "10115", "region", None, "ROSSMANN", None, 1, "rossmann-official", None, "rossmann_catalog_match", None),
                # Row 10: Claims region with arbitrary unrecognized verification_method -> demote to 0
                (10, "kaffee", "Kaffee", "coffee", "lidl", "Lidl", 1.29, "2026-01-01T10:00:00", "https://lidl.de", "10115", "region", None, None, None, 1, "lidl", "reg_1", "invented_method_xyz", None),
                # Row 11: Verified nationwide offer with registered evidence in VERIFIED_NATIONWIDE_REGISTRY -> KEEP verified = 1
                (11, "kaffee", "Kaffee", "coffee", "rewe", "REWE", 2.99, "2026-01-01T10:00:00", "https://www.marktguru.de/r/rewe", "DE", "nationwide", None, None, None, 1, "marktguru", "national_flyer:nat_proven_999", "marktguru_national_flyer", None),
                # Row 12: Store offer with address PLZ 10407 differing from requested PLZ 10115 -> demote to 0
                (12, "kaffee", "Kaffee", "coffee", "edeka", "EDEKA", 2.49, "2026-01-01T10:00:00", "https://edeka.de", "10115", "store", "edeka_prenzlauer", "EDEKA Prenzlauer Berg", "Karl-Marx-Str. 1, 10407 Berlin", 1, "edeka-official", None, "store_direct_api", row_12_ev),
            ],
        )
        conn.commit()
        conn.close()

        # Initializing PriceHistoryService triggers migration / revalidation
        service = PriceHistoryService(Path(tmp_dir))

        # Running revalidate_historical_records again must be idempotent (0 additional demotions)
        assert service.revalidate_historical_records() == 0

        with service._get_connection() as c:
            rows = {
                r["id"]: (r["region_verified"], r["verification_method"])
                for r in c.execute("SELECT id, region_verified, verification_method FROM price_history").fetchall()
            }

        # Assert all 12 rows are preserved (non-destructive)
        assert len(rows) == 12

        # Assert unsubstantiated records demoted
        for bad_id in (1, 2, 3, 4, 6, 7, 8, 9, 10, 12):
            assert rows[bad_id][0] == 0, f"Row {bad_id} should have been demoted"

        assert rows[6][1] == "demoted_unsubstantiated_nationwide_claim"
        assert rows[12][1] == "demoted_store_location_mismatch"

        # Assert substantiated records kept
        assert rows[5][0] == 1
        assert rows[5][1] == "store_direct_api"
        assert rows[11][0] == 1
        assert rows[11][1] == "marktguru_national_flyer"


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

    # 6b. With store identity ONLY (missing requested_zip / location evidence) -> scope_type="store", region_verified=False
    offer_store_no_loc = EdekaProvider._offer_from_dialog(
        dialog,
        fallback_from="2026-06-01",
        fallback_to="2026-06-07",
        source_url="https://edeka.de",
        market_name="EDEKA Mitte",
        store_id="801006",
    )
    assert offer_store_no_loc is not None
    assert offer_store_no_loc.scope_type == "store"
    assert offer_store_no_loc.region_verified is False
    assert offer_store_no_loc.verification_method == "unverified_store_location"

    # 6c. With store identity AND verified location evidence -> scope_type="store", region_verified=True
    offer_with_store = EdekaProvider._offer_from_dialog(
        dialog,
        fallback_from="2026-06-01",
        fallback_to="2026-06-07",
        source_url="https://edeka.de",
        market_name="EDEKA Mitte",
        store_id="801006",
        store_address="Chausseestr. 1, 10115 Berlin",
        requested_zip="10115",
        location_verified=True,
    )
    assert offer_with_store is not None
    assert offer_with_store.scope_type == "store"
    assert offer_with_store.region_verified is True
    assert offer_with_store.store_id == "801006"
    assert offer_with_store.verification_method == "store_direct_api"


def test_marktguru_verification_logic_for_national_regional_branch_and_unknown():
    item_national = {
        "id": 101,
        "product": {"name": "National Butter"},
        "price": 1.29,
        "flyer": {"id": 999, "isNational": True},
    }
    # 1a. National flyer: without scope-specific evidence in VERIFIED_NATIONWIDE_REGISTRY -> fails closed (unverified)
    offer_nat_unverified = MarktguruProvider._offer_from_api(item_national, "edeka", zip_code="10115")
    assert offer_nat_unverified.scope_type == "nationwide"
    assert offer_nat_unverified.region_verified is False
    assert offer_nat_unverified.verification_method == "marktguru_unverified_national"

    # Once registered with verified nationwide evidence -> verifies as marktguru_national_flyer
    register_verified_nationwide_campaign(
        "999",
        {
            "campaign_id": "999",
            "source_url": "https://www.marktguru.de/leaflets/999",
            "evidence_type": "verified_nationwide_distribution_manifest",
            "verification_date": "2026-06-01",
            "scope": "nationwide",
            "promotional_applicability_confirmed": True,
            "limitations": "Audited nationwide flyer",
            "evidence_artifact_path": str(FIXTURE_EVIDENCE_DIR / "999.json"),
        },
    )
    offer_nat = MarktguruProvider._offer_from_api(item_national, "edeka", zip_code="10115")
    assert offer_nat.scope_type == "nationwide"
    assert offer_nat.region_verified is True
    assert offer_nat.source_region_id == "national_flyer:999"
    assert offer_nat.verification_method == "marktguru_national_flyer"

    # 1b. National flyer with contradictory regional restrictions -> fails closed (region_verified=False)
    item_nat_contradictory = {
        "id": 1011,
        "product": {"name": "Contradictory National Butter"},
        "price": 1.29,
        "flyer": {"id": 999, "isNational": True, "regionId": "munich_south"},
    }
    offer_nat_contra = MarktguruProvider._offer_from_api(item_nat_contradictory, "edeka", zip_code="10115")
    assert offer_nat_contra.scope_type == "nationwide"
    assert offer_nat_contra.region_verified is False
    assert offer_nat_contra.verification_method == "marktguru_unverified_national"

    # 1c. National flyer for territory-split retailer (aldi-sued) -> fails closed (region_verified=False)
    offer_nat_aldi = MarktguruProvider._offer_from_api(item_national, "aldi-sued", zip_code="80331")
    assert offer_nat_aldi.scope_type == "nationwide"
    assert offer_nat_aldi.region_verified is False
    assert offer_nat_aldi.verification_method == "marktguru_unverified_national"

    # 2a. Regional flyer with confirmed promotional campaign PLZ coverage
    item_reg_plz = {
        "id": 102,
        "product": {"name": "Regional Milk"},
        "price": 0.99,
        "flyer": {
            "id": 888,
            "validZipCodes": ["10115", "10117"],
            "coverageScope": "promotional_campaign",
            "regionId": 42,
        },
    }
    offer_reg = MarktguruProvider._offer_from_api(item_reg_plz, "edeka", zip_code="10115")
    assert offer_reg.scope_type == "region"
    assert offer_reg.region_verified is True
    assert offer_reg.source_region_id == "42"
    assert offer_reg.verification_method == "marktguru_regional_flyer_postal_match"

    # 2b. Regional flyer with generic postalCodes (unconfirmed campaign applicability) -> diagnostic match only
    item_generic_plz = {
        "id": 1021,
        "product": {"name": "Generic Postal Milk"},
        "price": 0.99,
        "flyer": {"id": 888, "postalCodes": ["10115", "10117"], "regionId": 42},
    }
    offer_generic_plz = MarktguruProvider._offer_from_api(item_generic_plz, "edeka", zip_code="10115")
    assert offer_generic_plz.scope_type == "region"
    assert offer_generic_plz.region_verified is False
    assert offer_generic_plz.source_region_id == "42"
    assert offer_generic_plz.verification_method == "marktguru_diagnostic_region_match"

    # 3a. Regional flyer with unsubstantiated "berlin_north" regionId -> fails closed
    item_reg_id = {
        "id": 103,
        "product": {"name": "Regional Cheese"},
        "price": 2.49,
        "flyer": {"id": 777, "regionId": "berlin_north"},
    }
    offer_reg_id = MarktguruProvider._offer_from_api(item_reg_id, "edeka", zip_code="10115")
    assert offer_reg_id.scope_type == "region"
    assert offer_reg_id.region_verified is False
    assert offer_reg_id.source_region_id == "berlin_north"
    assert offer_reg_id.verification_method == "marktguru_unverified_flyer"

    # 3b. Regional flyer with prefix heuristics (prefix wildcards fail closed without full 5-digit verification)
    item_reg_prefixes = {
        "id": 1031,
        "product": {"name": "Regional Yoghurt"},
        "price": 0.79,
        "flyer": {"id": 778, "region": {"id": "reg_prefix", "plzPrefixes": ["10", "12"]}},
    }
    offer_reg_prefixes = MarktguruProvider._offer_from_api(item_reg_prefixes, "edeka", zip_code="10115")
    assert offer_reg_prefixes.scope_type == "region"
    assert offer_reg_prefixes.region_verified is False
    assert offer_reg_prefixes.verification_method == "marktguru_unverified_flyer"

    # 3c. Regional flyer with mismatched postal codes (Munich flyer for Berlin PLZ)
    item_mismatched_pc = {
        "id": 1032,
        "product": {"name": "Munich Leaflet Beer"},
        "price": 1.19,
        "flyer": {"id": 779, "postalCodes": ["80331", "80333"], "regionId": "munich_central"},
    }
    offer_mismatched_pc = MarktguruProvider._offer_from_api(item_mismatched_pc, "edeka", zip_code="10115")
    assert offer_mismatched_pc.scope_type == "region"
    assert offer_mismatched_pc.region_verified is False
    assert offer_mismatched_pc.verification_method == "marktguru_unverified_flyer"
    assert "postal codes do not cover" in offer_mismatched_pc.verification_reason

    # 3d. Regional flyer with mismatched region name/ID (Munich region for Berlin PLZ)
    item_mismatched_region = {
        "id": 1033,
        "product": {"name": "Munich Leaflet Pretzel"},
        "price": 0.69,
        "flyer": {"id": 780, "regionId": "munich_south", "regionName": "München Süd"},
    }
    offer_mismatched_reg = MarktguruProvider._offer_from_api(item_mismatched_region, "edeka", zip_code="10115")
    assert offer_mismatched_reg.scope_type == "region"
    assert offer_mismatched_reg.region_verified is False
    assert offer_mismatched_reg.verification_method == "marktguru_unverified_flyer"
    assert "mismatches requested PLZ" in offer_mismatched_reg.verification_reason

    # 3e. Regional flyer with regionId but missing geographic mapping metadata
    item_missing_mapping = {
        "id": 1034,
        "product": {"name": "Opaque Leaflet Butter"},
        "price": 1.99,
        "flyer": {"id": 781, "regionId": 42},
    }
    offer_missing_mapping = MarktguruProvider._offer_from_api(item_missing_mapping, "edeka", zip_code="10115")
    assert offer_missing_mapping.scope_type == "region"
    assert offer_missing_mapping.region_verified is False
    assert offer_missing_mapping.source_region_id == "42"
    assert offer_missing_mapping.verification_method == "marktguru_unverified_flyer"
    assert "lacks authoritative geographic coverage mapping" in offer_missing_mapping.verification_reason

    # 4. Flyer without geographic proof (only a flyerId, no postalCode match or regionId)
    # Must NOT invent region ID from flyerId!
    item_unverified_flyer = {
        "id": 104,
        "product": {"name": "Unverified Leaflet Item"},
        "price": 3.99,
        "flyer": {"id": 555},
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

    # 6a. Branch with mismatched postal code and no nearby distance
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

    # 6b. Branch with conflicting postal code (Munich 80331) but misleading small distance (5km) -> fails closed!
    item_branch_conflict = {
        "id": 1061,
        "product": {"name": "Conflicting Branch Coffee"},
        "price": 4.99,
        "branch": {"id": 5679, "name": "Filiale Conflict", "postalCode": "80331", "distance": 5.0},
    }
    offer_branch_conflict = MarktguruProvider._offer_from_api(item_branch_conflict, "edeka", zip_code="10115")
    assert offer_branch_conflict.scope_type == "store"
    assert offer_branch_conflict.region_verified is False
    assert offer_branch_conflict.verification_method == "marktguru_unverified_branch"

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
            store_address="Invalidenstr. 10, 10115 Berlin",
            source_provider="edeka-official",
            region_verified=True,
            verification_method="store_direct_api",
            verification_evidence=build_store_verification_evidence(
                store_id="store_b1",
                retailer_slug="edeka",
                store_address="Invalidenstr. 10, 10115 Berlin",
                requested_plz="10115",
                store_name="EDEKA Berlin Mitte",
                metadata_source="edeka_official_market_api",
                verification_method="store_direct_api",
            ),
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
            store_address="Marienplatz 1, 80331 München",
            source_provider="edeka-official",
            region_verified=True,
            verification_method="store_direct_api",
            verification_evidence=build_store_verification_evidence(
                store_id="store_m1",
                retailer_slug="edeka",
                store_address="Marienplatz 1, 80331 München",
                requested_plz="80331",
                store_name="EDEKA Munich Altstadt",
                metadata_source="edeka_official_market_api",
                verification_method="store_direct_api",
            ),
        )

        register_verified_nationwide_campaign(
            "901",
            {
                "campaign_id": "901",
                "source_url": "https://www.marktguru.de/leaflets/901",
                "evidence_type": "verified_nationwide_distribution_manifest",
                "verification_date": "2026-06-01",
                "scope": "nationwide",
                "promotional_applicability_confirmed": True,
                "limitations": "Audited nationwide promotion",
                "evidence_artifact_path": str(FIXTURE_EVIDENCE_DIR / "901.json"),
            },
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
            source_url="https://www.marktguru.de/r/rewe",
            scope_type="nationwide",
            source_provider="marktguru",
            source_region_id="national_flyer:901",
            region_verified=True,
            verification_method="marktguru_national_flyer",
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
            verification_method="legacy_unverified",
        )

        offer_unverified_nationwide = Offer(
            id="unv-nat-kaffee",
            source_offer_id=5,
            retailer="Penny",
            retailer_slug="penny",
            title="Kaffee 500g",
            brand=None,
            category="coffee",
            price=0.80,
            old_price=None,
            unit_price=1.60,
            unit="kg",
            discount_percent=None,
            description="",
            valid_from=None,
            valid_to=None,
            image_url=None,
            source_url="https://penny.de",
            scope_type="nationwide",
            region_verified=False,
            verification_method="national_online_catalog",
        )

        service.record_offers(
            [offer_berlin, offer_munich, offer_nationwide, offer_unverified, offer_unverified_nationwide],
            zip_code="10115",
            recorded_at="2026-06-01T10:00:00",
        )

        pkey = normalize_product_key("Kaffee 500g")

        # 1. Berlin regional ATL:
        # Includes Berlin store (3.49) and Nationwide verified (3.99).
        # MUST EXCLUDE Munich store (2.99), Unverified fake (0.50), and Unverified nationwide (0.80).
        berlin_atl = service.get_all_time_low_map(zip_code="10115")
        assert berlin_atl[pkey] == 3.49

        # 2. Munich regional ATL:
        # Includes Munich store (2.99) and Nationwide verified (3.99).
        # MUST EXCLUDE Berlin store (3.49), Unverified fake (0.50), and Unverified nationwide (0.80).
        munich_atl = service.get_all_time_low_map(zip_code="80331")
        assert munich_atl[pkey] == 2.99

        # 3. National verified ATL:
        # Includes verified observations (Munich 2.99, Berlin 3.49, Nationwide verified 3.99).
        # MUST EXCLUDE unverified (0.50) and unverified nationwide (0.80).
        national_atl = service.get_all_time_low_map(zip_code=None)
        assert national_atl[pkey] == 2.99

        # 4. Product stats for Berlin user (with zip_code="10115"):
        berlin_stats = service.get_product_stats("Kaffee 500g", current_price=3.49, zip_code="10115")
        assert berlin_stats["regional_all_time_low"] == 3.49
        assert berlin_stats["national_all_time_low"] == 2.99
        assert berlin_stats["all_time_low"] == 3.49
        assert berlin_stats["record_count"] == 3  # Verified records (Berlin 3.49, Munich 2.99, Nationwide 3.99)
        assert len(berlin_stats["history"]) == 2  # Berlin Mitte + Nationwide
        assert not any(h["price"] in (0.50, 0.80) for h in berlin_stats["history"])
        assert len(berlin_stats["unverified_history"]) == 2  # Unverified 0.50 and unverified nationwide 0.80 exposed separately
        unverified_prices = {h["price"] for h in berlin_stats["unverified_history"]}
        assert unverified_prices == {0.50, 0.80}

        # History points for Berlin user:
        # Must ONLY contain observations for Berlin or Nationwide.
        # MUST NOT contain Munich or unverified observations!
        history_stores = [h["store_name"] for h in berlin_stats["history"]]
        assert "EDEKA Berlin Mitte" in history_stores
        assert "EDEKA Munich Altstadt" not in history_stores
        assert all(h["price"] >= 2.99 for h in berlin_stats["history"])

        # 5. Product stats without PLZ (zip_code=None):
        national_stats = service.get_product_stats("Kaffee 500g", current_price=3.49, zip_code=None)
        assert national_stats["all_time_low"] == 2.99  # Munich 2.99
        assert national_stats["all_time_high"] == 3.99  # Nationwide 3.99
        assert national_stats["avg_price"] == round((3.49 + 2.99 + 3.99) / 3, 2)
        assert national_stats["record_count"] == 3
        # Must contain all verified observations (Berlin, Munich, Nationwide), but MUST NOT contain unverified (0.50, 0.80)!
        assert len(national_stats["history"]) == 3
        assert not any(h["price"] in (0.50, 0.80) for h in national_stats["history"])
        assert all(h["price"] >= 2.99 for h in national_stats["history"])
        # Unverified history exposed separately
        assert len(national_stats["unverified_history"]) == 2
        unverified_nat_prices = {h["price"] for h in national_stats["unverified_history"]}
        assert unverified_nat_prices == {0.50, 0.80}


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
    # Scope badge must be rendered in its own card-scope-row container
    assert '<div class="card-scope-row">' in html


def test_ui_rendered_price_history_modal_isolates_unverified(monkeypatch):
    import shutil
    import subprocess

    node_bin = shutil.which("node")
    if not node_bin:
        pytest.skip("Node.js binary not available for rendered UI test")

    with tempfile.TemporaryDirectory() as tmp_dir:
        service = PriceHistoryService(Path(tmp_dir))

        # 1. Verified store observation in Berlin (10115) at 3.49 €
        verified_berlin = Offer(
            id="v-berlin",
            source_offer_id=1,
            retailer="EDEKA",
            retailer_slug="edeka",
            title="Kaffee 500g",
            brand="Tchibo",
            category="coffee",
            price=3.49,
            old_price=4.99,
            unit_price=6.98,
            unit="kg",
            discount_percent=30.0,
            description="",
            valid_from="2026-06-01",
            valid_to="2026-06-07",
            image_url=None,
            source_url="https://edeka.de/berlin",
            requested_zip_code="10115",
            scope_type="store",
            store_id="edeka_berlin_mitte",
            store_name="EDEKA Berlin Mitte",
            store_address="Invalidenstraße 100, 10115 Berlin",
            source_provider="edeka-official",
            region_verified=True,
            verification_method="store_direct_api",
            verification_evidence=build_store_verification_evidence(
                store_id="edeka_berlin_mitte",
                retailer_slug="edeka",
                store_address="Invalidenstraße 100, 10115 Berlin",
                requested_plz="10115",
                store_name="EDEKA Berlin Mitte",
                metadata_source="edeka_official_market_api",
                verification_method="store_direct_api",
            ),
        )

        # 2. Verified nationwide observation at 3.99 €
        register_verified_nationwide_campaign(
            "700",
            {
                "campaign_id": "700",
                "source_url": "https://www.marktguru.de/leaflets/700",
                "evidence_type": "verified_nationwide_distribution_manifest",
                "verification_date": "2026-06-01",
                "scope": "nationwide",
                "promotional_applicability_confirmed": True,
                "limitations": "Audited nationwide promotion",
                "evidence_artifact_path": str(FIXTURE_EVIDENCE_DIR / "700.json"),
            },
        )
        verified_national = Offer(
            id="v-nat",
            source_offer_id=2,
            retailer="REWE",
            retailer_slug="rewe",
            title="Kaffee 500g",
            brand="Tchibo",
            category="coffee",
            price=3.99,
            old_price=4.99,
            unit_price=7.98,
            unit="kg",
            discount_percent=20.0,
            description="",
            valid_from="2026-06-01",
            valid_to="2026-06-07",
            image_url=None,
            source_url="https://www.marktguru.de/r/rewe",
            requested_zip_code="10115",
            scope_type="nationwide",
            source_provider="marktguru",
            source_region_id="national_flyer:700",
            region_verified=True,
            verification_method="marktguru_national_flyer",
        )

        # 3. Unverified observation at 0.50 € (misleading unverified data)
        unverified_offer = Offer(
            id="unv-1",
            source_offer_id=3,
            retailer="Penny",
            retailer_slug="penny",
            title="Kaffee 500g",
            brand="Tchibo",
            category="coffee",
            price=0.50,
            old_price=4.99,
            unit_price=1.00,
            unit="kg",
            discount_percent=90.0,
            description="",
            valid_from="2026-06-01",
            valid_to="2026-06-07",
            image_url=None,
            source_url="https://penny.de",
            scope_type="unknown",
            region_verified=False,
            verification_method="legacy_unverified",
        )

        # 4. Unverified nationwide observation at 0.75 €
        unverified_national = Offer(
            id="unv-nat",
            source_offer_id=4,
            retailer="Netto",
            retailer_slug="netto",
            title="Kaffee 500g",
            brand="Tchibo",
            category="coffee",
            price=0.75,
            old_price=4.99,
            unit_price=1.50,
            unit="kg",
            discount_percent=85.0,
            description="",
            valid_from="2026-06-01",
            valid_to="2026-06-07",
            image_url=None,
            source_url="https://netto.de",
            scope_type="nationwide",
            region_verified=False,
            verification_method="national_online_catalog",
        )

        # 5. Missing / sparse fields observation with unknown scope
        unverified_missing_fields = Offer(
            id="unv-sparse",
            source_offer_id=5,
            retailer="Unknown",
            retailer_slug="unknown",
            title="Kaffee 500g",
            brand="Tchibo",
            category="coffee",
            price=0.60,
            old_price=None,
            unit_price=None,
            unit=None,
            discount_percent=None,
            description="",
            valid_from=None,
            valid_to=None,
            image_url=None,
            source_url="https://example.com/sparse",
            scope_type=None,
            region_verified=False,
            verification_method="legacy_unverified",
        )

        service.record_offers(
            [verified_berlin, verified_national, unverified_offer, unverified_national, unverified_missing_fields],
            zip_code="10115",
        )

        class FakeOfferServiceWithHistory:
            def __init__(self, history_service):
                self.price_history = history_service

        from app import routes
        monkeypatch.setattr(routes, "offer_service", lambda: FakeOfferServiceWithHistory(service))

        app = create_app()
        client = app.test_client()

        # Query backend /api/price-history
        res = client.get("/api/price-history?q=Kaffee+500g&brand=Tchibo&price=3.49&zip=10115")
        assert res.status_code == 200
        data = res.get_json()

        # Backend verification
        assert data["all_time_low"] == 3.49
        assert data["is_all_time_low"] is True
        assert len(data["history"]) == 2
        assert len(data["unverified_history"]) == 3
        unv_prices = {h["price"] for h in data["unverified_history"]}
        assert unv_prices == {0.50, 0.75, 0.60}

        # Execute Node.js frontend rendering
        js_file = Path(__file__).resolve().parent.parent / "app" / "static" / "js" / "shopping_list.js"
        runner_code = f"""
        const {{ renderPriceHistoryHtml }} = require({json.dumps(str(js_file))});
        const data = {json.dumps(data)};
        const html = renderPriceHistoryHtml(data, 'ko');
        console.log(html);
        """
        proc = subprocess.run(
            [node_bin, "-e", runner_code],
            capture_output=True,
            text=True,
            check=True,
        )
        rendered_html = proc.stdout

        # Assertions on rendered UI:
        # A. Stats grid: All-time low is verified 3.49 €, NEVER unverified 0.50 €, 0.60 €, or 0.75 €
        assert "3.49 €" in rendered_html
        assert "역대 최저가" in rendered_html
        assert "현재 최저가" in rendered_html

        # B. Verified table contains only verified items
        assert "가격 관측 이력 (검증된 지역/전국 데이터)" in rendered_html
        assert "EDEKA Berlin Mitte" in rendered_html
        assert "scope-store" in rendered_html
        assert "scope-nationwide" in rendered_html
        assert "전국공통" in rendered_html

        # C. Unverified section is rendered with clear warning and separate container
        assert "unverified-history-wrap" in rendered_html
        assert "미검증 관측 이력 (참고용)" in rendered_html
        assert "공식 매장 또는 지역 적용 여부가 확인되지 않아 공식 통계에서 분리된 관측 데이터입니다." in rendered_html
        assert "0.50 €" in rendered_html
        assert "0.75 €" in rendered_html
        assert "0.60 €" in rendered_html
        assert "scope-unverified" in rendered_html
        assert "전국(미검증)" in rendered_html
        assert "미확인" in rendered_html

        # D. Verified table portion must NOT contain unverified prices
        verified_section, unverified_section = rendered_html.split("unverified-history-wrap", 1)
        assert "0.50 €" not in verified_section
        assert "0.75 €" not in verified_section
        assert "0.60 €" not in verified_section
        assert "0.50 €" in unverified_section
        assert "0.75 €" in unverified_section
        assert "0.60 €" in unverified_section
        assert "3.49 €" not in unverified_section


def test_raw_sql_malformed_verified_rows_do_not_pollute_atl_or_stats(tmp_path):
    """
    Even if malformed or incomplete 'verified' rows are inserted via raw SQL
    after DB initialization, SQL provenance predicates in get_all_time_low_map()
    and get_product_stats() must exclude them from verified stats and history.
    """
    service = PriceHistoryService(tmp_path)
    pkey = normalize_product_key("Bio Milch 1l")

    with service._get_connection() as conn:
        conn.executemany(
            """
            INSERT INTO price_history (
                product_key, title, category, retailer_slug, retailer_name, price, recorded_at,
                source_url, zip_code, scope_type, store_id, store_name, region_verified,
                source_provider, source_region_id, verification_method
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                # 1. Malformed store row: region_verified=1, store_direct_api, but store_id is NULL
                (pkey, "Bio Milch 1l", "dairy", "edeka", "EDEKA", 0.30, "2026-06-01T10:00:00", "https://edeka.de", "10115", "store", None, "EDEKA", 1, "edeka-official", None, "store_direct_api"),
                # 2. Malformed region row: region_verified=1, but source_region_id is NULL
                (pkey, "Bio Milch 1l", "dairy", "edeka", "EDEKA", 0.40, "2026-06-01T10:00:00", "https://edeka.de", "10115", "region", None, None, 1, "marktguru", None, "marktguru_regional_flyer_postal_match"),
                # 3. Malformed region row: retailer territory disguised as region_verified=1
                (pkey, "Bio Milch 1l", "dairy", "aldi-sued", "ALDI Süd", 0.45, "2026-06-01T10:00:00", "https://aldi-sued.de", "10115", "region", None, None, 1, "aldi-sued-official", "aldi-sued-territory", "marktguru_regional_flyer_postal_match"),
                # 4. Malformed nationwide row: national_catalog claiming region_verified=1
                (pkey, "Bio Milch 1l", "dairy", "rewe", "REWE", 0.50, "2026-06-01T10:00:00", "https://rewe.de", "DE", "nationwide", None, None, 1, "rewe-official", "national_flyer:1", "national_catalog"),
                # 5. Malformed nationwide row: marktguru_national_flyer without source_region_id
                (pkey, "Bio Milch 1l", "dairy", "rewe", "REWE", 0.55, "2026-06-01T10:00:00", "https://rewe.de", "DE", "nationwide", None, None, 1, "marktguru", None, "marktguru_national_flyer"),
                # 6. Malformed nationwide row: territory-split retailer (aldi) claiming nationwide
                (pkey, "Bio Milch 1l", "dairy", "aldi", "ALDI", 0.60, "2026-06-01T10:00:00", "https://aldi.de", "DE", "nationwide", None, None, 1, "marktguru", "national_flyer:2", "marktguru_national_flyer"),
                # 7. Valid verified store row in 10115 at 1.29 EUR
                (pkey, "Bio Milch 1l", "dairy", "edeka", "EDEKA", 1.29, "2026-06-01T10:00:00", "https://edeka.de", "10115", "store", "store_10115", "EDEKA Mitte", 1, "edeka-official", None, "store_direct_api"),
            ],
        )
        conn.commit()

    assert service.get_all_time_low_map(zip_code="10115")[pkey] == 1.29
    assert service.get_all_time_low_map(zip_code=None)[pkey] == 1.29

    stats_regional = service.get_product_stats("Bio Milch 1l", current_price=1.29, zip_code="10115")
    assert stats_regional["all_time_low"] == 1.29
    assert stats_regional["regional_all_time_low"] == 1.29
    assert stats_regional["national_all_time_low"] == 1.29
    assert stats_regional["record_count"] == 1
    assert len(stats_regional["history"]) == 1
    assert stats_regional["history"][0]["price"] == 1.29
    assert len(stats_regional["unverified_history"]) == 6


def test_no_cross_plz_leakage_or_current_price_fallback_when_region_has_zero_verified_records(tmp_path):
    """
    1. When Munich (80331) has a verified store price (2.49) and Berlin (10115) has 0 verified records,
       querying get_product_stats for Berlin (zip_code='10115') must NOT leak Munich's 2.49 into
       Berlin's all_time_low or average_price.
    2. When a product has ONLY unverified records (0 verified records anywhere), get_product_stats
       must NOT fall back all_time_low/avg_price to current_price or claim is_all_time_low=True.
    3. _enrich_atl_if_needed must NEVER set is_all_time_low=True on an unverified offer.
    4. Recording an unverified offer on the same day must NOT suppress recording a subsequent verified offer.
    """
    from app.routes import _enrich_atl_if_needed

    service = PriceHistoryService(tmp_path)

    # Case A: Unverified offer recorded first, followed by verified offer for same product/price/day
    unv_berlin = Offer(
        id="unv-b",
        source_offer_id=1,
        retailer="EDEKA",
        retailer_slug="edeka",
        title="Bio Butter 250g",
        brand=None,
        category="dairy",
        price=2.19,
        old_price=None,
        unit_price=8.76,
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
        region_verified=False,
        verification_method="unverified_store_location",
    )
    ver_berlin = Offer(
        id="ver-b",
        source_offer_id=1,
        retailer="EDEKA",
        retailer_slug="edeka",
        title="Bio Butter 250g",
        brand=None,
        category="dairy",
        price=2.19,
        old_price=None,
        unit_price=8.76,
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
        store_address="Invalidenstr. 10, 10115 Berlin",
        source_provider="edeka-official",
        region_verified=True,
        verification_method="store_direct_api",
        verification_evidence=build_store_verification_evidence(
            store_id="store_b1",
            retailer_slug="edeka",
            store_address="Invalidenstr. 10, 10115 Berlin",
            requested_plz="10115",
            store_name="EDEKA Berlin",
            metadata_source="edeka_official_market_api",
            verification_method="store_direct_api",
        ),
    )
    ins_unv = service.record_offers([unv_berlin], zip_code="10115", recorded_at="2026-06-01T09:00:00")
    ins_ver = service.record_offers([ver_berlin], zip_code="10115", recorded_at="2026-06-01T10:00:00")
    assert ins_unv == 1
    assert ins_ver == 1  # Must NOT be suppressed by the earlier unverified row!
    assert service.get_all_time_low_map(zip_code="10115")[normalize_product_key("Bio Butter 250g")] == 2.19

    # Case B: Munich has verified record for "Alpenkäse 200g" at 2.49; Berlin has only unverified at 1.99
    munich_cheese = Offer(
        id="m-cheese",
        source_offer_id=2,
        retailer="EDEKA",
        retailer_slug="edeka",
        title="Alpenkäse 200g",
        brand=None,
        category="dairy",
        price=2.49,
        old_price=None,
        unit_price=12.45,
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
        store_address="Marienplatz 1, 80331 München",
        source_provider="edeka-official",
        region_verified=True,
        verification_method="store_direct_api",
        verification_evidence=build_store_verification_evidence(
            store_id="store_m1",
            retailer_slug="edeka",
            store_address="Marienplatz 1, 80331 München",
            requested_plz="80331",
            store_name="EDEKA Munich",
            metadata_source="edeka_official_market_api",
            verification_method="store_direct_api",
        ),
    )
    berlin_unv_cheese = Offer(
        id="b-unv-cheese",
        source_offer_id=3,
        retailer="Penny",
        retailer_slug="penny",
        title="Alpenkäse 200g",
        brand=None,
        category="dairy",
        price=1.99,
        old_price=None,
        unit_price=9.95,
        unit="kg",
        discount_percent=None,
        description="",
        valid_from=None,
        valid_to=None,
        image_url=None,
        source_url="https://penny.de",
        requested_zip_code="10115",
        scope_type="unknown",
        region_verified=False,
    )
    service.record_offers([munich_cheese], zip_code="80331", recorded_at="2026-06-01T10:00:00")
    service.record_offers([berlin_unv_cheese], zip_code="10115", recorded_at="2026-06-01T10:00:00")

    berlin_cheese_stats = service.get_product_stats("Alpenkäse 200g", current_price=1.99, zip_code="10115")
    assert berlin_cheese_stats["regional_all_time_low"] is None
    assert berlin_cheese_stats["all_time_low"] is None  # Must NOT leak Munich's 2.49!
    assert berlin_cheese_stats["average_price"] is None
    assert berlin_cheese_stats["avg_price"] is None
    assert berlin_cheese_stats["is_all_time_low"] is False
    assert berlin_cheese_stats["is_regional_all_time_low"] is False
    assert berlin_cheese_stats["history"] == []
    assert len(berlin_cheese_stats["unverified_history"]) == 1

    # Case C: Product with ONLY unverified records anywhere
    only_unv = Offer(
        id="only-unv",
        source_offer_id=4,
        retailer="REWE",
        retailer_slug="rewe",
        title="Nationale Schokolade 100g",
        brand=None,
        category="sweets",
        price=0.99,
        old_price=None,
        unit_price=9.90,
        unit="kg",
        discount_percent=None,
        description="",
        valid_from=None,
        valid_to=None,
        image_url=None,
        source_url="https://rewe.de",
        scope_type="nationwide",
        region_verified=False,
        verification_method="national_catalog",
    )
    service.record_offers([only_unv], zip_code="10115", recorded_at="2026-06-01T10:00:00")
    unv_stats = service.get_product_stats("Nationale Schokolade 100g", current_price=0.99, zip_code="10115")
    assert unv_stats["all_time_low"] is None
    assert unv_stats["national_all_time_low"] is None
    assert unv_stats["is_all_time_low"] is False
    assert unv_stats["is_national_all_time_low"] is False
    assert unv_stats["record_count"] == 0
    assert unv_stats["history"] == []
    assert len(unv_stats["unverified_history"]) == 1

    # Case D: _enrich_atl_if_needed never flags an unverified offer as is_all_time_low=True
    class DummyService:
        def get_all_time_low_map(self, zip_code=None):
            return {normalize_product_key("Bio Butter 250g"): 2.19}

    unv_copy = Offer.from_dict(unv_berlin.to_dict())
    ver_copy = Offer.from_dict(ver_berlin.to_dict())
    _enrich_atl_if_needed([unv_copy, ver_copy], DummyService(), "2026-06-01T10:00:00", zip_code="10115")
    assert unv_copy.is_all_time_low is False
    assert unv_copy.is_regional_all_time_low is False
    assert unv_copy.is_national_all_time_low is False
    assert ver_copy.is_all_time_low is True
    assert ver_copy.is_regional_all_time_low is True
    assert ver_copy.is_national_all_time_low is True


def test_store_verification_mismatched_and_distant_locations_fail_closed_as_unverified():
    # 1. EDEKA tests
    from bs4 import BeautifulSoup
    dialog_html = """
    <dialog id="dialog-angebot-101">
      <h3><span>Angebot:</span> Frische Vollmilch 1l</h3>
      <div class="sr-only">Festpreis von 1.19 €</div>
      <div class="price-note">1 l = 1,19 €</div>
    </dialog>
    """
    soup = BeautifulSoup(dialog_html, "html.parser")
    sample_dialog = soup.find("dialog")

    # Case 1A: Exact matching PLZ (10115) -> verified store offer
    offer_edeka_matched = EdekaProvider._offer_from_dialog(
        sample_dialog,
        fallback_from="2026-06-01",
        fallback_to="2026-06-07",
        source_url="https://edeka.de",
        requested_zip="10115",
        market_name="EDEKA Berlin Mitte",
        store_address="Invalidenstr. 100, 10115 Berlin",
        store_id="10001099",
        location_verified=True,
    )
    assert offer_edeka_matched.scope_type == "store"
    assert offer_edeka_matched.region_verified is True
    assert offer_edeka_matched.verification_method == "store_direct_api"
    assert "verified at PLZ 10115" in offer_edeka_matched.verification_reason

    # Case 1B: Mismatched PLZ (Store in 10407, User in 10115) -> preserved as unverified store observation
    offer_edeka_mismatched = EdekaProvider._offer_from_dialog(
        sample_dialog,
        fallback_from="2026-06-01",
        fallback_to="2026-06-07",
        source_url="https://edeka.de",
        requested_zip="10115",
        market_name="EDEKA Prenzlauer Berg",
        store_address="Greifswalder Str. 50, 10407 Berlin",
        store_id="10002000",
        location_verified=False,
    )
    assert offer_edeka_mismatched.scope_type == "store"
    assert offer_edeka_mismatched.region_verified is False
    assert offer_edeka_mismatched.verification_method == "unverified_store_location"
    assert "does not match requested PLZ 10115" in offer_edeka_mismatched.verification_reason
    assert offer_edeka_mismatched.price == 1.19  # Preserved, not discarded!

    # Case 1C: Missing store identity -> unknown scope
    offer_edeka_no_id = EdekaProvider._offer_from_dialog(
        sample_dialog,
        fallback_from="2026-06-01",
        fallback_to="2026-06-07",
        source_url="https://edeka.de",
        requested_zip="10115",
        market_name=None,
        store_address=None,
        store_id="",
        location_verified=False,
    )
    assert offer_edeka_no_id.scope_type == "unknown"
    assert offer_edeka_no_id.region_verified is False
    assert offer_edeka_no_id.verification_method == "unverified_missing_store_id"

    # 2. Kaufland tests
    kaufland_payload = {
        "component": "OfferTemplate",
        "props": {
            "offerData": {
                "cycles": [
                    {
                        "categories": [
                            {
                                "name": "Milchprodukte",
                                "offers": [
                                    {
                                        "offerId": "KL-555",
                                        "title": "H-Milch 3,5%",
                                        "price": "0.99",
                                        "formattedPrice": "0,99 €",
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
    kp = KauflandProvider()

    # Case 2A: Matching PLZ (80331) -> verified store offer
    offers_kl_matched = kp.offers_from_payload(
        kaufland_payload,
        limit=5,
        store_id="DE1110",
        store_name="Kaufland München Altstadt",
        store_address="Sendlinger Str. 1, 80331 München",
        requested_zip="80331",
    )
    assert len(offers_kl_matched) == 1
    kl_match = offers_kl_matched[0]
    assert kl_match.scope_type == "store"
    assert kl_match.region_verified is True
    assert kl_match.verification_method == "store_cookie_session"

    # Case 2B: Mismatched PLZ (Store in 80339, User in 80331) -> unverified store observation
    offers_kl_mismatched = kp.offers_from_payload(
        kaufland_payload,
        limit=5,
        store_id="DE1111",
        store_name="Kaufland München Westend",
        store_address="Landsberger Str. 100, 80339 München",
        requested_zip="80331",
    )
    assert len(offers_kl_mismatched) == 1
    kl_mismatch = offers_kl_mismatched[0]
    assert kl_mismatch.scope_type == "store"
    assert kl_mismatch.region_verified is False
    assert kl_mismatch.verification_method == "unverified_store_location"
    assert "does not match requested PLZ 80331" in kl_mismatch.verification_reason
    assert kl_mismatch.price == 0.99  # Preserved

    # Case 2C: Fallback when store_id is None -> nationwide unverified fallback
    offers_kl_fallback = kp.offers_from_payload(
        kaufland_payload,
        limit=5,
        store_id=None,
        store_name=None,
        store_address=None,
        requested_zip="80331",
    )
    assert len(offers_kl_fallback) == 1
    kl_fallback = offers_kl_fallback[0]
    assert kl_fallback.scope_type == "nationwide"
    assert kl_fallback.region_verified is False
    assert kl_fallback.verification_method == "national_catalog_fallback"


def test_unsupported_regional_and_nationwide_evidence_demotion():
    # 1. Marktguru: generic postalCodes without campaign evidence
    raw_generic = {
        "id": 901,
        "product": {"name": "Test Butter"},
        "price": 1.49,
        "flyer": {"id": 1234, "postalCodes": ["10115"], "regionId": "region_123"},
    }
    offer_gen = MarktguruProvider._offer_from_api(raw_generic, "rewe", zip_code="10115")
    assert offer_gen.region_verified is False
    assert offer_gen.verification_method == "marktguru_diagnostic_region_match"

    # 2. Marktguru: source-provided boolean campaignApplicabilityVerified without valid scope
    raw_untrusted_bool = {
        "id": 902,
        "product": {"name": "Test Butter 2"},
        "price": 1.49,
        "flyer": {
            "id": 1235,
            "campaignApplicabilityVerified": True,  # Untrusted boolean
            "postalCodes": ["10115"],  # Generic field, not validZipCodes with promotional scope
            "regionId": "region_124",
        },
    }
    offer_untrusted = MarktguruProvider._offer_from_api(raw_untrusted_bool, "rewe", zip_code="10115")
    assert offer_untrusted.region_verified is False
    assert offer_untrusted.verification_method == "marktguru_diagnostic_region_match"

    # 3. Marktguru: isNational: True without registration in VERIFIED_NATIONWIDE_REGISTRY
    raw_unregistered_nat = {
        "id": 903,
        "product": {"name": "Test National Coffee"},
        "price": 3.99,
        "flyer": {"id": 99999, "isNational": True},
    }
    offer_nat_unreg = MarktguruProvider._offer_from_api(raw_unregistered_nat, "rewe", zip_code="10115")
    assert offer_nat_unreg.scope_type == "nationwide"
    assert offer_nat_unreg.region_verified is False
    assert offer_nat_unreg.verification_method == "marktguru_unverified_national"

    # 4. Ingestion into PriceHistoryService demotes unregistered regional flyer and unregistered nationwide flyer
    with tempfile.TemporaryDirectory() as tmp_dir:
        service = PriceHistoryService(Path(tmp_dir))
        unreg_regional = Offer(
            id="unreg-reg-1",
            source_offer_id=904,
            retailer="EDEKA",
            retailer_slug="edeka",
            title="Bio Kaffee",
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
            requested_zip_code="10115",
            scope_type="region",
            source_provider="marktguru",
            source_region_id="unregistered_campaign_666",
            region_verified=True,  # Claims verified upstream
            verification_method="marktguru_regional_flyer_postal_match",
        )
        service.record_offers([unreg_regional], zip_code="10115")

        with service._get_connection() as conn:
            row = conn.execute("SELECT region_verified, verification_method FROM price_history WHERE id = 1").fetchone()
            assert row["region_verified"] == 0
            assert row["verification_method"] == "demoted_unsubstantiated_campaign_evidence"


def test_end_to_end_price_integrity_pipeline():
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        offer_service = OfferService(
            cache_dir=tmp_path / "cache",
            retailers={"edeka": "EDEKA", "kaufland": "Kaufland"},
            cache_ttl_seconds=3600,
            timeout=5,
            verify_tls=True,
            max_offers_per_retailer=10,
        )
        history_service = PriceHistoryService(tmp_path / "history")

        # 1. Simulate provider outputs:
        # A: EDEKA in 10115 (verified store offer at 2.99)
        edeka_offer = Offer(
            id="e-1",
            source_offer_id=101,
            retailer="EDEKA",
            retailer_slug="edeka",
            title="Jacobs Kroenung 500g",
            brand="Jacobs",
            category="coffee",
            price=2.99,
            old_price=4.99,
            unit_price=5.98,
            unit="kg",
            discount_percent=40.0,
            description="Kaffee gemahlen",
            valid_from="2026-06-01",
            valid_to="2026-06-07",
            image_url=None,
            source_url="https://edeka.de",
            requested_zip_code="10115",
            scope_type="store",
            store_id="edeka_10115",
            store_name="EDEKA Berlin Mitte",
            store_address="Invalidenstr. 100, 10115 Berlin",
            source_provider="edeka-official",
            region_verified=True,
            verification_method="store_direct_api",
            verification_evidence=build_store_verification_evidence(
                store_id="edeka_10115",
                retailer_slug="edeka",
                store_address="Invalidenstr. 100, 10115 Berlin",
                requested_plz="10115",
                store_name="EDEKA Berlin Mitte",
                metadata_source="edeka_official_market_api",
                verification_method="store_direct_api",
            ),
        )

        # B: Kaufland with store in 10407 (mismatched store location at 1.99 -> unverified)
        kaufland_offer = Offer(
            id="k-1",
            source_offer_id=102,
            retailer="Kaufland",
            retailer_slug="kaufland",
            title="Jacobs Kroenung 500g",
            brand="Jacobs",
            category="coffee",
            price=1.99,
            old_price=4.99,
            unit_price=3.98,
            unit="kg",
            discount_percent=60.0,
            description="Kaffee gemahlen",
            valid_from="2026-06-01",
            valid_to="2026-06-07",
            image_url=None,
            source_url="https://kaufland.de",
            requested_zip_code="10115",
            scope_type="store",
            store_id="kaufland_10407",
            store_name="Kaufland Berlin Prenzlauer Berg",
            store_address="Greifswalder Str. 80, 10407 Berlin",
            source_provider="kaufland-official",
            region_verified=False,
            verification_method="unverified_store_location",
        )

        # C: Marktguru unverified nationwide offer at 0.99
        unv_national_offer = Offer(
            id="m-1",
            source_offer_id=103,
            retailer="Marktguru REWE",
            retailer_slug="rewe",
            title="Jacobs Kroenung 500g",
            brand="Jacobs",
            category="coffee",
            price=0.99,
            old_price=4.99,
            unit_price=1.98,
            unit="kg",
            discount_percent=80.0,
            description="Kaffee gemahlen",
            valid_from="2026-06-01",
            valid_to="2026-06-07",
            image_url=None,
            source_url="https://marktguru.de",
            requested_zip_code="10115",
            scope_type="nationwide",
            source_provider="marktguru",
            source_region_id="national_flyer:unreg_99",
            region_verified=False,
            verification_method="marktguru_unverified_national",
        )

        raw_offers = [edeka_offer, kaufland_offer, unv_national_offer]

        # 2. Category normalization
        normalized_offers = offer_service._normalize_categories(raw_offers)
        assert len(normalized_offers) == 3

        # 3. Record in price history
        history_service.record_offers(normalized_offers, zip_code="10115")

        # 4. Check get_all_time_low_map
        pkey = normalize_product_key("Jacobs Kroenung 500g", "Jacobs")
        reg_atl_map = history_service.get_all_time_low_map(zip_code="10115")
        nat_atl_map = history_service.get_all_time_low_map(zip_code=None)

        # Verified regional ATL MUST be 2.99 (EDEKA), NEVER contaminated by 1.99 or 0.99!
        assert reg_atl_map[pkey] == 2.99
        assert nat_atl_map[pkey] == 2.99

        # 5. Check get_product_stats
        stats = history_service.get_product_stats("Jacobs Kroenung 500g", current_price=2.99, zip_code="10115", brand="Jacobs")
        assert stats["all_time_low"] == 2.99
        assert stats["regional_all_time_low"] == 2.99
        assert stats["national_all_time_low"] == 2.99
        assert stats["record_count"] == 1
        assert len(stats["history"]) == 1
        assert stats["history"][0]["price"] == 2.99
        assert stats["history"][0]["retailer"] == "EDEKA"

        # Unverified history has both the 1.99 (Kaufland) and 0.99 (Marktguru) observations isolated
        assert len(stats["unverified_history"]) == 2
        unv_prices = {h["price"] for h in stats["unverified_history"]}
        assert unv_prices == {1.99, 0.99}


# ===========================================================================
# 10. Audit of Evidence Trust Boundaries and Persistence Regression Tests
# ===========================================================================


def test_case_1_nationwide_registration_without_evidence_rejected():
    """1. Nationwide campaign registration without explicit, auditable evidence is rejected."""
    clear_campaign_registries()
    assert register_verified_nationwide_campaign("nat_no_evidence") is False
    assert register_verified_nationwide_campaign("nat_no_evidence_none", evidence=None) is False
    assert is_verified_nationwide_campaign("nat_no_evidence") is False
    assert get_verified_nationwide_evidence("nat_no_evidence") is None


def test_case_2_fabricated_generic_evidence_dict_rejected():
    """2. Fabricated or generic evidence dict cannot establish nationwide applicability."""
    clear_campaign_registries()
    # Missing source_url
    assert register_verified_nationwide_campaign(
        "nat_fake_1",
        {
            "campaign_id": "nat_fake_1",
            "evidence_type": "verified_nationwide_distribution_manifest",
            "verification_date": "2026-06-01",
            "scope": "nationwide",
            "promotional_applicability_confirmed": True,
            "limitations": "None",
        },
    ) is False

    # Wrong scope
    assert register_verified_nationwide_campaign(
        "nat_fake_2",
        {
            "campaign_id": "nat_fake_2",
            "source_url": "https://example.com",
            "evidence_type": "verified_nationwide_distribution_manifest",
            "verification_date": "2026-06-01",
            "scope": "regional",
            "promotional_applicability_confirmed": True,
            "limitations": "None",
        },
    ) is False

    # promotional_applicability_confirmed is not True
    assert register_verified_nationwide_campaign(
        "nat_fake_3",
        {
            "campaign_id": "nat_fake_3",
            "source_url": "https://example.com",
            "evidence_type": "verified_nationwide_distribution_manifest",
            "verification_date": "2026-06-01",
            "scope": "nationwide",
            "promotional_applicability_confirmed": False,
            "limitations": "None",
        },
    ) is False

    # Contradictory regional fields in nationwide evidence
    assert register_verified_nationwide_campaign(
        "nat_fake_4",
        {
            "campaign_id": "nat_fake_4",
            "source_url": "https://example.com",
            "evidence_type": "verified_nationwide_distribution_manifest",
            "verification_date": "2026-06-01",
            "scope": "nationwide",
            "promotional_applicability_confirmed": True,
            "limitations": "None",
            "postalCodes": ["10115"],
        },
    ) is False

    # Contradictory regionId
    assert register_verified_nationwide_campaign(
        "nat_fake_5",
        {
            "campaign_id": "nat_fake_5",
            "source_url": "https://example.com",
            "evidence_type": "verified_nationwide_distribution_manifest",
            "verification_date": "2026-06-01",
            "scope": "nationwide",
            "promotional_applicability_confirmed": True,
            "limitations": "None",
            "region_id": "berlin_north",
        },
    ) is False


def test_case_3_campaign_id_alone_cannot_establish_nationwide_applicability():
    """3. Campaign ID alone cannot establish nationwide applicability."""
    clear_campaign_registries()
    camp_id = "national_flyer:arbitrary_camp_9999"
    assert is_verified_nationwide_campaign(camp_id) is False

    is_valid, reason, msg = validate_observation_provenance(
        scope_type="nationwide",
        region_verified=True,
        verification_method="marktguru_national_flyer",
        source_region_id=camp_id,
        source_provider="marktguru",
        source_url="https://marktguru.de",
    )
    assert is_valid is False
    assert reason == "demoted_unsubstantiated_nationwide_claim"

    # Bypass protection: public mapping is read-only
    with pytest.raises((TypeError, AttributeError)):
        VERIFIED_NATIONWIDE_REGISTRY[camp_id] = {"valid": True}  # type: ignore


def test_case_4_regional_registration_without_evidence_rejected():
    """4. Regional registration without validated promotional coverage evidence is rejected."""
    clear_campaign_registries()
    assert register_verified_campaign("reg_no_ev", ["10115"], evidence=None) is False
    assert register_verified_campaign("reg_empty_plz", [], evidence={"some": "data"}) is False
    assert register_verified_campaign("reg_invalid_plz", ["999999"], evidence={"source_url": "https://..."}) is False
    assert get_verified_campaign_plzs("reg_no_ev") is None

    # Bypass protection: public mapping is read-only
    with pytest.raises((TypeError, AttributeError)):
        VERIFIED_CAMPAIGN_REGISTRY["reg_no_ev"] = {"covered_plzs": ["10115"]}  # type: ignore


def test_case_5_valid_evidence_backed_regional_campaign_passes():
    """5. Valid evidence-backed regional campaign passes verification."""
    clear_campaign_registries()
    valid_ev = {
        "campaign_id": "reg_valid_101",
        "source_url": "https://www.marktguru.de/leaflets/reg_valid_101",
        "evidence_type": "verified_campaign_distribution_manifest",
        "verification_date": "2026-06-01",
        "scope": "region",
        "promotional_applicability_confirmed": True,
        "covered_plzs": ["10115", "10117"],
        "limitations": "Berlin Mitte confirmed distribution zone",
        "evidence_artifact_path": str(FIXTURE_EVIDENCE_DIR / "reg_valid_101.json"),
    }
    assert register_verified_campaign("reg_valid_101", ["10115", "10117"], valid_ev) is True
    assert get_verified_campaign_plzs("reg_valid_101") == {"10115", "10117"}

    # PLZ 10115 is covered -> valid
    is_valid, method, _ = validate_observation_provenance(
        scope_type="region",
        region_verified=True,
        verification_method="marktguru_regional_flyer_postal_match",
        source_region_id="reg_valid_101",
        source_provider="marktguru",
        source_url="https://marktguru.de",
        zip_code="10115",
    )
    assert is_valid is True
    assert method == "marktguru_regional_flyer_postal_match"

    # PLZ 80331 (Munich) is not covered -> demoted
    is_valid_munich, reason_munich, _ = validate_observation_provenance(
        scope_type="region",
        region_verified=True,
        verification_method="marktguru_regional_flyer_postal_match",
        source_region_id="reg_valid_101",
        source_provider="marktguru",
        source_url="https://marktguru.de",
        zip_code="80331",
    )
    assert is_valid_munich is False
    assert reason_munich == "demoted_unsubstantiated_campaign_evidence"


def test_case_6_valid_evidence_backed_nationwide_campaign_passes():
    """6. Valid evidence-backed nationwide campaign passes verification."""
    clear_campaign_registries()
    valid_nat_ev = NationwideCoverageEvidence(
        campaign_id="nat_valid_606",
        source_url="https://www.marktguru.de/leaflets/nat_valid_606",
        evidence_type="verified_nationwide_distribution_manifest",
        verification_date="2026-06-01",
        scope="nationwide",
        promotional_applicability_confirmed=True,
        limitations="Verified full nationwide circulation",
        evidence_artifact_path=str(FIXTURE_EVIDENCE_DIR / "nat_valid_606.json"),
    )
    assert register_verified_nationwide_campaign("nat_valid_606", valid_nat_ev) is True
    assert is_verified_nationwide_campaign("nat_valid_606") is True
    assert is_verified_nationwide_campaign("national_flyer:nat_valid_606") is True

    is_valid, method, _ = validate_observation_provenance(
        scope_type="nationwide",
        region_verified=True,
        verification_method="marktguru_national_flyer",
        source_region_id="national_flyer:nat_valid_606",
        source_provider="marktguru",
        source_url="https://marktguru.de",
    )
    assert is_valid is True
    assert method == "marktguru_national_flyer"


def test_case_7_verification_survives_application_restart_and_reload():
    """7. Verification provenance survives application restart and database reload."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        clear_campaign_registries()
        service1 = PriceHistoryService(Path(tmp_dir))

        # Register valid nationwide campaign
        ev = {
            "campaign_id": "restart_nat_707",
            "source_url": "https://www.marktguru.de/leaflets/restart_nat_707",
            "evidence_type": "verified_nationwide_distribution_manifest",
            "verification_date": "2026-06-01",
            "scope": "nationwide",
            "promotional_applicability_confirmed": True,
            "limitations": "Persisted nationwide evidence",
            "evidence_artifact_path": str(FIXTURE_EVIDENCE_DIR / "restart_nat_707.json"),
        }
        register_verified_nationwide_campaign("restart_nat_707", ev)

        offer = Offer(
            id="off-707",
            source_offer_id=707,
            retailer="REWE",
            retailer_slug="rewe",
            title="Bio Kaffee 500g",
            brand="REWE Bio",
            category="coffee",
            price=4.49,
            old_price=5.99,
            unit_price=8.98,
            unit="kg",
            discount_percent=25.0,
            description="",
            valid_from="2026-06-01",
            valid_to="2026-06-07",
            image_url=None,
            source_url="https://marktguru.de/rewe",
            scope_type="nationwide",
            source_provider="marktguru",
            source_region_id="national_flyer:restart_nat_707",
            region_verified=True,
            verification_method="marktguru_national_flyer",
            verification_evidence=ev,
        )
        service1.record_offers([offer], zip_code="10115")

        # Simulate full process restart: clear in-memory registries and class-level init cache
        clear_campaign_registries()
        PriceHistoryService._INITIALIZED_DBS.discard(service1.db_path)
        assert is_verified_nationwide_campaign("restart_nat_707") is False

        # Reload database via new service instance
        service2 = PriceHistoryService(Path(tmp_dir))

        # Persistent registry table has re-registered the campaign on reload
        assert is_verified_nationwide_campaign("restart_nat_707") is True

        # Check stats and ATL are verified and intact
        pkey = normalize_product_key("Bio Kaffee 500g", "REWE Bio")
        atl_map = service2.get_all_time_low_map(zip_code="10115")
        assert atl_map[pkey] == 4.49

        stats = service2.get_product_stats("Bio Kaffee 500g", brand="REWE Bio", current_price=4.49, zip_code="10115")
        assert stats["all_time_low"] == 4.49
        assert stats["record_count"] == 1
        assert len(stats["history"]) == 1
        assert len(stats["unverified_history"]) == 0

        # Migration / revalidation produces 0 demotions
        assert service2.revalidate_historical_records() == 0


def test_case_8_missing_or_invalid_persisted_evidence_causes_demotion():
    """8. Missing or invalid persisted evidence causes observation demotion on startup/migration."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        clear_campaign_registries()
        service = PriceHistoryService(Path(tmp_dir))

        # Insert unbacked nationwide and regional records directly claiming region_verified = 1
        with service._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO price_history (
                    product_key, title, retailer_slug, retailer_name, price, recorded_at,
                    source_url, zip_code, scope_type, region_verified, source_provider,
                    source_region_id, verification_method, verification_evidence
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)
                """,
                (
                    "demote-item",
                    "Demoted Product",
                    "rewe",
                    "REWE",
                    1.99,
                    "2026-06-01T10:00:00",
                    "https://marktguru.de",
                    "DE",
                    "nationwide",
                    "marktguru",
                    "national_flyer:unbacked_999",
                    "marktguru_national_flyer",
                    None,  # missing evidence
                ),
            )
            conn.execute(
                """
                INSERT INTO price_history (
                    product_key, title, retailer_slug, retailer_name, price, recorded_at,
                    source_url, zip_code, scope_type, region_verified, source_provider,
                    source_region_id, verification_method, verification_evidence
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)
                """,
                (
                    "demote-item-2",
                    "Demoted Product 2",
                    "rewe",
                    "REWE",
                    2.49,
                    "2026-06-01T10:00:00",
                    "https://marktguru.de",
                    "DE",
                    "nationwide",
                    "marktguru",
                    "national_flyer:corrupt_ev",
                    "marktguru_national_flyer",
                    json.dumps({"scope": "regional", "promotional_applicability_confirmed": False}),  # contradictory evidence
                ),
            )
            conn.commit()

        # Revalidate: both unsubstantiated records must be demoted
        demoted_count = service.revalidate_historical_records()
        assert demoted_count == 2

        with service._get_connection() as conn:
            r1 = conn.execute("SELECT region_verified, verification_method FROM price_history WHERE product_key = 'demote-item'").fetchone()
            assert r1["region_verified"] == 0
            assert r1["verification_method"] == "demoted_unsubstantiated_nationwide_claim"

            r2 = conn.execute("SELECT region_verified, verification_method FROM price_history WHERE product_key = 'demote-item-2'").fetchone()
            assert r2["region_verified"] == 0
            assert r2["verification_method"] == "demoted_invalid_nationwide_evidence"


def test_case_9_database_migration_is_idempotent():
    """9. Re-running database migrations is idempotent (0 additional demotions)."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        clear_campaign_registries()
        service = PriceHistoryService(Path(tmp_dir))
        # Initial run on empty/seeded DB
        d1 = service.revalidate_historical_records()
        # Second run
        d2 = service.revalidate_historical_records()
        assert d2 == 0


def test_case_10_edeka_store_candidate_prefers_matching_plz():
    """10. Verified EDEKA store is preferred over mismatched candidate."""
    candidates = [
        {"name": "EDEKA Prenzlauer Berg", "id": "store_10407", "coordinates": {"lat": 52.54, "lon": 13.42}, "contact": {"address": {"postalCode": "10407"}}},
        {"name": "EDEKA Berlin Mitte", "id": "store_10115", "coordinates": {"lat": 52.53, "lon": 13.38}, "contact": {"address": {"postalCode": "10115"}}},
    ]
    sorted_markets = _sort_candidate_markets(candidates, "10115")
    assert len(sorted_markets) == 2
    # Exact PLZ match 10115 is ranked first
    assert sorted_markets[0]["contact"]["address"]["postalCode"] == "10115"
    assert sorted_markets[1]["contact"]["address"]["postalCode"] == "10407"


def test_case_11_edeka_continues_checking_candidates_after_unverified(monkeypatch):
    """11. EDEKA continues checking candidates after an unverified candidate returns offers."""
    provider = EdekaProvider()

    c1 = {
        "name": "EDEKA Remote",
        "id": "store_10407",
        "url": "https://edeka.de/remote",
        "coordinates": {"lat": 52.54, "lon": 13.42},
        "contact": {"address": {"postalCode": "10407", "city": "Berlin"}},
    }
    c2 = {
        "name": "EDEKA Exact",
        "id": "store_10115",
        "url": "https://edeka.de/exact",
        "coordinates": {"lat": 52.53, "lon": 13.38},
        "contact": {"address": {"postalCode": "10115", "city": "Berlin"}},
    }

    monkeypatch.setattr(provider, "_candidate_markets", lambda plz: ([c1, c2], False))
    monkeypatch.setattr(provider, "_offers_url", lambda u: (u + "/angebote", False))
    monkeypatch.setattr(provider, "_request_text", lambda u: ("<html>mocked</html>", False))

    def fake_offers_from_html(html, limit, source_url, market_name=None, store_id=None, store_address=None, requested_zip=None, location_verified=False):
        is_verified = (store_id == "store_10115")
        return [
            Offer(
                id=f"e-{store_id}",
                source_offer_id=1,
                retailer="EDEKA",
                retailer_slug="edeka",
                title="Apfel",
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
                source_url=source_url,
                requested_zip_code=requested_zip,
                scope_type="store",
                store_id=store_id,
                store_name=market_name,
                store_address=store_address,
                region_verified=is_verified,
                verification_method="store_direct_api" if is_verified else "store_unverified_fallback",
            )
        ]

    monkeypatch.setattr(provider, "offers_from_html", fake_offers_from_html)

    offers, warnings = provider.fetch_offers("edeka", "10115", 10)
    assert offers is not None
    assert len(offers) == 1
    # Must have preferred and returned the verified candidate c2!
    assert offers[0].store_id == "store_10115"
    assert offers[0].region_verified is True


def test_case_12_no_matching_store_leaves_offers_unverified(monkeypatch):
    """12. If no matching store established, offers remain visible as unverified observations."""
    provider = EdekaProvider()
    c_mismatched = {
        "name": "EDEKA Nearby Only",
        "id": "store_10407",
        "url": "https://edeka.de/nearby",
        "coordinates": {"lat": 52.54, "lon": 13.42},
        "contact": {"address": {"postalCode": "10407", "city": "Berlin"}},
    }

    monkeypatch.setattr(provider, "_candidate_markets", lambda plz: ([c_mismatched], False))
    monkeypatch.setattr(provider, "_offers_url", lambda u: (u + "/angebote", False))
    monkeypatch.setattr(provider, "_request_text", lambda u: ("<html>mocked</html>", False))

    def fake_offers_from_html(html, limit, source_url, market_name=None, store_id=None, store_address=None, requested_zip=None, location_verified=False):
        return [
            Offer(
                id="e-unv",
                source_offer_id=1,
                retailer="EDEKA",
                retailer_slug="edeka",
                title="Birne",
                brand=None,
                category="produce",
                price=2.49,
                old_price=None,
                unit_price=2.49,
                unit="kg",
                discount_percent=None,
                description="",
                valid_from=None,
                valid_to=None,
                image_url=None,
                source_url=source_url,
                requested_zip_code=requested_zip,
                scope_type="store",
                store_id=store_id,
                store_name=market_name,
                store_address=store_address,
                region_verified=False,
                verification_method="store_unverified_fallback",
            )
        ]

    monkeypatch.setattr(provider, "offers_from_html", fake_offers_from_html)

    offers, warnings = provider.fetch_offers("edeka", "10115", 10)
    assert len(offers) == 1
    assert offers[0].region_verified is False
    assert offers[0].price == 2.49


def test_case_13_unverified_regional_and_nationwide_do_not_affect_verified_stats_or_atls():
    """13. Unverified regional and nationwide observations do not affect verified statistics or ATLs."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        clear_campaign_registries()
        service = PriceHistoryService(Path(tmp_dir))

        verified_store_offer = Offer(
            id="v-1",
            source_offer_id=1,
            retailer="EDEKA",
            retailer_slug="edeka",
            title="Milch 1L",
            brand="Weihenstephan",
            category="dairy",
            price=1.29,
            old_price=1.79,
            unit_price=1.29,
            unit="l",
            discount_percent=27.0,
            description="",
            valid_from=None,
            valid_to=None,
            image_url=None,
            source_url="https://edeka.de",
            requested_zip_code="10115",
            scope_type="store",
            store_id="edeka_10115",
            store_name="EDEKA Mitte",
            store_address="Invalidenstraße 100, 10115 Berlin",
            source_provider="edeka-official",
            region_verified=True,
            verification_method="store_direct_api",
            verification_evidence=build_store_verification_evidence(
                store_id="edeka_10115",
                retailer_slug="edeka",
                store_address="Invalidenstraße 100, 10115 Berlin",
                requested_plz="10115",
                store_name="EDEKA Mitte",
                metadata_source="edeka_official_market_api",
                verification_method="store_direct_api",
            ),
        )

        unv_regional = Offer(
            id="u-reg",
            source_offer_id=2,
            retailer="Marktguru ALDI",
            retailer_slug="aldi-nord",
            title="Milch 1L",
            brand="Weihenstephan",
            category="dairy",
            price=0.49,  # Unverified cheap price
            old_price=1.79,
            unit_price=0.49,
            unit="l",
            discount_percent=70.0,
            description="",
            valid_from=None,
            valid_to=None,
            image_url=None,
            source_url="https://marktguru.de",
            requested_zip_code="10115",
            scope_type="region",
            source_region_id="berlin_north",
            region_verified=False,
            verification_method="marktguru_diagnostic_region_match",
        )

        unv_nationwide = Offer(
            id="u-nat",
            source_offer_id=3,
            retailer="Online Shop",
            retailer_slug="penny",
            title="Milch 1L",
            brand="Weihenstephan",
            category="dairy",
            price=0.69,  # Unverified cheap price
            old_price=1.79,
            unit_price=0.69,
            unit="l",
            discount_percent=60.0,
            description="",
            valid_from=None,
            valid_to=None,
            image_url=None,
            source_url="https://penny.de",
            scope_type="nationwide",
            region_verified=False,
            verification_method="national_online_catalog",
        )

        service.record_offers([verified_store_offer, unv_regional, unv_nationwide], zip_code="10115")

        pkey = normalize_product_key("Milch 1L", "Weihenstephan")
        reg_atl = service.get_all_time_low_map(zip_code="10115")
        nat_atl = service.get_all_time_low_map(zip_code=None)

        # Verified ATL MUST be 1.29, not 0.49 or 0.69!
        assert reg_atl[pkey] == 1.29
        assert nat_atl[pkey] == 1.29

        stats = service.get_product_stats("Milch 1L", brand="Weihenstephan", current_price=1.29, zip_code="10115")
        assert stats["all_time_low"] == 1.29
        assert stats["regional_all_time_low"] == 1.29
        assert stats["national_all_time_low"] == 1.29
        assert stats["average_price"] == 1.29
        assert stats["record_count"] == 1
        assert len(stats["history"]) == 1
        assert stats["history"][0]["price"] == 1.29

        # Unverified observations isolated
        assert len(stats["unverified_history"]) == 2
        unv_prices = {h["price"] for h in stats["unverified_history"]}
        assert unv_prices == {0.49, 0.69}


def test_case_14_no_duplicate_observations_when_multiple_store_candidates_checked():
    """14. Duplicate observations are not created when multiple store candidates are checked."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        service = PriceHistoryService(Path(tmp_dir))
        o1 = Offer(
            id="dup-1",
            source_offer_id=1,
            retailer="EDEKA",
            retailer_slug="edeka",
            title="Butter 250g",
            brand="Kerrygold",
            category="dairy",
            price=2.22,
            old_price=3.49,
            unit_price=8.88,
            unit="kg",
            discount_percent=36.0,
            description="",
            valid_from=None,
            valid_to=None,
            image_url=None,
            source_url="https://edeka.de",
            requested_zip_code="10115",
            scope_type="store",
            store_id="store_10115",
            store_name="EDEKA Mitte",
            store_address="Invalidenstraße 100, 10115 Berlin",
            source_provider="edeka-official",
            region_verified=True,
            verification_method="store_direct_api",
            verification_evidence=build_store_verification_evidence(
                store_id="store_10115",
                retailer_slug="edeka",
                store_address="Invalidenstraße 100, 10115 Berlin",
                requested_plz="10115",
                store_name="EDEKA Mitte",
                metadata_source="edeka_official_market_api",
                verification_method="store_direct_api",
            ),
        )
        # Attempt to insert same offer twice in same day
        c1 = service.record_offers([o1], zip_code="10115", recorded_at="2026-06-01T10:00:00")
        c2 = service.record_offers([o1], zip_code="10115", recorded_at="2026-06-01T10:30:00")
        assert c1 == 1
        assert c2 == 0  # Deduped!

        stats = service.get_product_stats("Butter 250g", brand="Kerrygold", zip_code="10115")
        assert stats["record_count"] == 1


def test_case_15_existing_valid_evidence_backed_offers_continue_to_work():
    """15. Existing valid evidence-backed offers continue to pass verification and flow through services."""
    clear_campaign_registries()
    with tempfile.TemporaryDirectory() as tmp_dir:
        cache_dir = Path(tmp_dir)
        service = OfferService(
            retailers=["rewe", "edeka"],
            cache_ttl_seconds=60,
            timeout=5,
            verify_tls=True,
            max_offers_per_retailer=10,
            cache_dir=cache_dir,
        )
        history_service = service.price_history

        # Register valid nationwide campaign
        ev = {
            "campaign_id": "test_nat_15",
            "source_url": "https://www.marktguru.de/leaflets/test_nat_15",
            "evidence_type": "verified_nationwide_distribution_manifest",
            "verification_date": "2026-06-01",
            "scope": "nationwide",
            "promotional_applicability_confirmed": True,
            "limitations": "Full national coverage",
            "evidence_artifact_path": str(FIXTURE_EVIDENCE_DIR / "test_nat_15.json"),
        }
        assert register_verified_nationwide_campaign("test_nat_15", ev) is True

        offers = [
            Offer(
                id="nat-off-15",
                source_offer_id=15,
                retailer="REWE",
                retailer_slug="rewe",
                title="Bio Hafermilch 1L",
                brand="Oatly",
                category="dairy",
                price=1.69,
                old_price=2.49,
                unit_price=1.69,
                unit="l",
                discount_percent=32.0,
                description="",
                valid_from="2026-06-01",
                valid_to="2026-06-07",
                image_url=None,
                source_url="https://marktguru.de/rewe",
                scope_type="nationwide",
                source_provider="marktguru",
                source_region_id="national_flyer:test_nat_15",
                region_verified=True,
                verification_method="marktguru_national_flyer",
                verification_evidence=ev,
            )
        ]

        # Record in history
        history_service.record_offers(offers, zip_code="10115")

        # Stats query
        stats = history_service.get_product_stats("Bio Hafermilch 1L", brand="Oatly", current_price=1.69, zip_code="10115")
        assert stats["all_time_low"] == 1.69
        assert stats["is_all_time_low"] is True
        assert stats["record_count"] == 1
        assert len(stats["history"]) == 1
        assert stats["history"][0]["region_verified"] is True

        # Watchlist match
        wl_result = history_service.match_watchlist_deals(
            [{"id": "w1", "name": "Hafermilch Oatly", "target_price": 2.00}],
            offers,
        )
        assert wl_result["total_active_alerts"] == 1
        assert wl_result["items"][0]["best_deal"]["price"] == 1.69


# ===========================================================================
# 11. Explicit 18 False-Positive Price Verification Scenarios
# ===========================================================================


def test_scenario_1_fabricated_url_with_valid_metadata_is_rejected():
    """1. A fabricated URL with otherwise valid nationwide metadata is rejected."""
    clear_campaign_registries()
    ev = {
        "campaign_id": "fab_nat_1",
        "source_url": "https://www.marktguru.de/leaflets/fabricated_flyer_123",
        "evidence_type": "verified_nationwide_distribution_manifest",
        "verification_date": "2026-06-01",
        "scope": "nationwide",
        "promotional_applicability_confirmed": True,
        "limitations": "Fabricated leaflet URL without artifact",
    }
    assert register_verified_nationwide_campaign("fab_nat_1", ev) is False
    assert is_verified_nationwide_campaign("fab_nat_1") is False


def test_scenario_2_unsupported_or_unavailable_artifact_cannot_establish_nationwide():
    """2. An unsupported or unavailable evidence artifact cannot establish nationwide applicability."""
    clear_campaign_registries()
    ev_missing_file = {
        "campaign_id": "missing_art_2",
        "source_url": "https://www.marktguru.de/leaflets/missing_art_2",
        "evidence_type": "verified_nationwide_distribution_manifest",
        "verification_date": "2026-06-01",
        "scope": "nationwide",
        "promotional_applicability_confirmed": True,
        "limitations": "Artifact does not exist",
        "evidence_artifact_path": "/tmp/nonexistent_artifact_xyz_12345.json",
    }
    assert register_verified_nationwide_campaign("missing_art_2", ev_missing_file) is False


def test_scenario_3_missing_campaign_id_in_evidence_is_rejected():
    """3. A missing campaign ID in evidence is rejected."""
    clear_campaign_registries()
    ev = {
        "campaign_id": "",
        "source_url": "https://www.marktguru.de/leaflets/missing_id",
        "evidence_type": "verified_nationwide_distribution_manifest",
        "verification_date": "2026-06-01",
        "scope": "nationwide",
        "promotional_applicability_confirmed": True,
        "limitations": "Empty campaign ID",
        "evidence_artifact_path": str(FIXTURE_EVIDENCE_DIR / "nat_valid_606.json"),
    }
    assert register_verified_nationwide_campaign("missing_id", ev) is False


def test_scenario_4_invalid_calendar_date_is_rejected():
    """4. An invalid calendar date is rejected."""
    clear_campaign_registries()
    for bad_date in ("2026-02-31", "2026-13-40", "invalid-date", "2026-04-31"):
        ev = {
            "campaign_id": "bad_date_camp",
            "source_url": "https://www.marktguru.de/leaflets/bad_date",
            "evidence_type": "verified_nationwide_distribution_manifest",
            "verification_date": bad_date,
            "scope": "nationwide",
            "promotional_applicability_confirmed": True,
            "limitations": "Invalid calendar date",
            "evidence_artifact_path": str(FIXTURE_EVIDENCE_DIR / "nat_valid_606.json"),
        }
        assert register_verified_nationwide_campaign("bad_date_camp", ev) is False


def test_scenario_5_caller_provided_regional_plz_list_without_artifact_is_rejected():
    """5. A caller-provided regional PLZ list without independently validated promotional evidence is rejected."""
    clear_campaign_registries()
    caller_ev = {
        "campaign_id": "caller_reg_5",
        "source_url": "https://www.marktguru.de/leaflets/caller_reg_5",
        "evidence_type": "verified_campaign_distribution_manifest",
        "verification_date": "2026-06-01",
        "scope": "region",
        "promotional_applicability_confirmed": True,
        "covered_plzs": ["10115", "10117"],
        "limitations": "Caller supplied PLZ list without recorded artifact",
    }
    assert register_verified_campaign("caller_reg_5", ["10115", "10117"], caller_ev) is False
    assert get_verified_campaign_plzs("caller_reg_5") is None


def test_scenario_6_generic_geographic_postal_metadata_does_not_establish_applicability():
    """6. Generic geographic postal-code metadata does not establish campaign applicability."""
    from app.providers.marktguru import _extract_verified_campaign_plzs, verify_marktguru_regional_applicability

    flyer_data = {
        "id": "flyer_geo_6",
        "validZipCodes": ["10115", "10117"],
        "coverageScope": "geographic_service_area",  # generic geographic scope, NOT promotional_campaign
    }
    plzs, reason = _extract_verified_campaign_plzs(flyer_data, "flyer_geo_6")
    assert plzs is None
    assert "does not confirm promotional campaign" in (reason or "")

    # verify_marktguru_regional_applicability fails closed (unverified)
    v, method, _, _ = verify_marktguru_regional_applicability("10115", flyer_data, {})
    assert v is False
    assert method == "marktguru_diagnostic_region_match"


def test_scenario_7_valid_source_backed_regional_and_nationwide_evidence_passes():
    """7. Valid source-backed regional and nationwide evidence still passes."""
    clear_campaign_registries()

    # Regional passes
    reg_ev = {
        "campaign_id": "reg_valid_101",
        "source_url": "https://www.marktguru.de/leaflets/reg_valid_101",
        "evidence_type": "verified_campaign_distribution_manifest",
        "verification_date": "2026-06-01",
        "scope": "region",
        "promotional_applicability_confirmed": True,
        "covered_plzs": ["10115", "10117"],
        "limitations": "Berlin Mitte confirmed distribution zone",
        "evidence_artifact_path": str(FIXTURE_EVIDENCE_DIR / "reg_valid_101.json"),
    }
    assert register_verified_campaign("reg_valid_101", ["10115", "10117"], reg_ev) is True
    assert get_verified_campaign_plzs("reg_valid_101") == {"10115", "10117"}

    # Nationwide passes
    nat_ev = {
        "campaign_id": "nat_valid_606",
        "source_url": "https://www.marktguru.de/leaflets/nat_valid_606",
        "evidence_type": "verified_nationwide_distribution_manifest",
        "verification_date": "2026-06-01",
        "scope": "nationwide",
        "promotional_applicability_confirmed": True,
        "limitations": "Verified full nationwide circulation",
        "evidence_artifact_path": str(FIXTURE_EVIDENCE_DIR / "nat_valid_606.json"),
    }
    assert register_verified_nationwide_campaign("nat_valid_606", nat_ev) is True
    assert is_verified_nationwide_campaign("nat_valid_606") is True


def test_scenario_8_store_id_plus_requested_plz_without_location_evidence_is_rejected():
    """8. Store ID plus requested PLZ, without sufficient store-location evidence, is rejected."""
    is_valid, reason, _ = validate_observation_provenance(
        scope_type="store",
        region_verified=True,
        verification_method="store_direct_api",
        store_id="store_xyz_8",
        zip_code="10115",
        source_provider="edeka-official",
        source_url="https://edeka.de",
        store_address=None,
        verification_evidence=None,
    )
    assert is_valid is False
    assert reason == "demoted_missing_store_location_evidence"


def test_scenario_9_missing_store_address_and_missing_location_evidence_fail_closed():
    """9. Missing store address and missing authoritative location evidence fail closed."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        service = PriceHistoryService(Path(tmp_dir))
        o = Offer(
            id="off-9",
            source_offer_id=9,
            retailer="EDEKA",
            retailer_slug="edeka",
            title="Keks 100g",
            brand=None,
            category="snacks",
            price=0.99,
            old_price=None,
            unit_price=9.90,
            unit="kg",
            discount_percent=None,
            description="",
            valid_from=None,
            valid_to=None,
            image_url=None,
            source_url="https://edeka.de",
            requested_zip_code="10115",
            scope_type="store",
            store_id="store_no_addr",
            store_address=None,
            source_provider="edeka-official",
            region_verified=True,
            verification_method="store_direct_api",
        )
        service.record_offers([o], zip_code="10115")

        with service._get_connection() as conn:
            row = conn.execute("SELECT region_verified, verification_method FROM price_history WHERE id = 1").fetchone()
            assert row["region_verified"] == 0
            assert row["verification_method"] == "demoted_missing_store_location_evidence"


def test_scenario_10_matched_plz_and_store_plz_inconsistency_rejected():
    """10. matched_plz and store_plz inconsistencies are rejected."""
    ev = {
        "scope": "store",
        "store_id": "store_10",
        "store_plz": "10115",
        "matched_plz": "10407",  # Contradicts store_plz!
    }
    is_valid, reason, msg = validate_observation_provenance(
        scope_type="store",
        region_verified=True,
        verification_method="store_direct_api",
        store_id="store_10",
        zip_code="10115",
        source_provider="edeka-official",
        source_url="https://edeka.de",
        store_address="Invalidenstr. 10, 10115 Berlin",
        verification_evidence=ev,
    )
    assert is_valid is False
    assert reason == "demoted_store_evidence_inconsistency"


def test_scenario_11_evidence_for_different_store_or_retailer_rejected():
    """11. Evidence for a different store or retailer is rejected."""
    # 1. Different store_id
    ev_diff_store = {
        "scope": "store",
        "store_id": "store_OTHER",
        "store_plz": "10115",
        "matched_plz": "10115",
    }
    is_valid_store, reason_store, _ = validate_observation_provenance(
        scope_type="store",
        region_verified=True,
        verification_method="store_direct_api",
        store_id="store_11",
        zip_code="10115",
        source_provider="edeka-official",
        source_url="https://edeka.de",
        store_address="Invalidenstr. 10, 10115 Berlin",
        verification_evidence=ev_diff_store,
    )
    assert is_valid_store is False
    assert reason_store == "demoted_store_evidence_mismatch"

    # 2. Different retailer
    ev_diff_retailer = {
        "scope": "store",
        "store_id": "store_11",
        "retailer_slug": "kaufland",
        "store_plz": "10115",
        "matched_plz": "10115",
    }
    is_valid_ret, reason_ret, _ = validate_observation_provenance(
        scope_type="store",
        region_verified=True,
        verification_method="store_direct_api",
        store_id="store_11",
        retailer_slug="edeka",
        zip_code="10115",
        source_provider="edeka-official",
        source_url="https://edeka.de",
        store_address="Invalidenstr. 10, 10115 Berlin",
        verification_evidence=ev_diff_retailer,
    )
    assert is_valid_ret is False
    assert reason_ret == "demoted_retailer_evidence_mismatch"


def test_scenario_12_edeka_and_kaufland_providers_produce_compatible_evidence():
    """12. EDEKA and Kaufland providers produce evidence compatible with the central validator."""
    # EDEKA provider offer
    html = """
    <html>
      <body>
        <section>
          <dialog id="dialog-angebot-butter-12">
            <h3><span>Angebot:</span> EDEKA Butter</h3>
            <div class="sr-only">Rabattierter Preis von 1.79 €</div>
          </dialog>
        </section>
      </body>
    </html>
    """
    edeka_offers = EdekaProvider.offers_from_html(
        html,
        limit=1,
        source_url="https://edeka.de",
        market_name="EDEKA Berlin Mitte",
        store_id="edeka_10115",
        store_address="Invalidenstraße 100, 10115 Berlin",
        requested_zip="10115",
        location_verified=True,
    )
    assert len(edeka_offers) == 1
    edeka_offer = edeka_offers[0]
    assert edeka_offer.region_verified is True
    assert edeka_offer.verification_evidence is not None
    assert edeka_offer.verification_evidence["retailer_slug"] == "edeka"
    assert edeka_offer.verification_evidence["store_plz"] == "10115"
    assert edeka_offer.verification_evidence["matched_plz"] == "10115"

    is_valid_e, method_e, _ = validate_observation_provenance(
        scope_type=edeka_offer.scope_type,
        region_verified=edeka_offer.region_verified,
        verification_method=edeka_offer.verification_method,
        store_id=edeka_offer.store_id,
        store_name=edeka_offer.store_name,
        store_address=edeka_offer.store_address,
        source_provider=edeka_offer.source_provider,
        source_url=edeka_offer.source_url,
        zip_code=edeka_offer.requested_zip_code,
        retailer_slug=edeka_offer.retailer_slug,
        verification_evidence=edeka_offer.verification_evidence,
    )
    assert is_valid_e is True
    assert method_e == "store_direct_api"

    # Kaufland provider offer
    kaufland_item = {
        "offerId": "k_item_12",
        "title": "Kaufland Butter",
        "price": 1.79,
    }
    kaufland_offer = KauflandProvider._offer_from_item(
        kaufland_item,
        category_name="dairy",
        store_id="kaufland_10115",
        store_name="Kaufland Berlin",
        store_address="Alexanderplatz 1, 10115 Berlin",
        requested_zip="10115",
        location_verified=True,
    )
    assert kaufland_offer.region_verified is True
    assert kaufland_offer.verification_evidence is not None
    assert kaufland_offer.verification_evidence["retailer_slug"] == "kaufland"
    assert kaufland_offer.verification_evidence["store_plz"] == "10115"
    assert kaufland_offer.verification_evidence["matched_plz"] == "10115"

    is_valid_k, method_k, _ = validate_observation_provenance(
        scope_type=kaufland_offer.scope_type,
        region_verified=kaufland_offer.region_verified,
        verification_method=kaufland_offer.verification_method,
        store_id=kaufland_offer.store_id,
        store_name=kaufland_offer.store_name,
        store_address=kaufland_offer.store_address,
        source_provider="kaufland-official",
        source_url=kaufland_offer.source_url,
        zip_code=kaufland_offer.requested_zip_code,
        retailer_slug=kaufland_offer.retailer_slug,
        verification_evidence=kaufland_offer.verification_evidence,
    )
    assert is_valid_k is True
    assert method_k == "store_cookie_session"


def test_scenario_13_invalid_persisted_evidence_cannot_be_restored_as_verified_after_restart():
    """13. Invalid persisted evidence cannot be restored as verified after restart."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        clear_campaign_registries()
        db_path = Path(tmp_dir) / "price_history.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            """
            CREATE TABLE verified_campaign_registry (
                campaign_id TEXT PRIMARY KEY,
                scope_type TEXT NOT NULL,
                evidence_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        # Persist invalid evidence (fabricated without artifact)
        invalid_ev = {
            "campaign_id": "invalid_persisted_13",
            "source_url": "https://example.com/fake",
            "evidence_type": "verified_nationwide_distribution_manifest",
            "verification_date": "2026-06-01",
            "scope": "nationwide",
            "promotional_applicability_confirmed": True,
            "limitations": "none",
        }
        conn.execute(
            "INSERT INTO verified_campaign_registry VALUES (?, ?, ?, ?)",
            ("invalid_persisted_13", "nationwide", json.dumps(invalid_ev), "2026-06-01T10:00:00"),
        )
        conn.commit()
        conn.close()

        # Reloading service MUST NOT restore invalid campaign
        service = PriceHistoryService(Path(tmp_dir))
        assert is_verified_nationwide_campaign("invalid_persisted_13") is False


def test_scenario_14_malformed_registry_records_handled_safely_and_diagnostically(caplog):
    """14. Malformed registry records are handled safely and diagnostically."""
    import logging

    with tempfile.TemporaryDirectory() as tmp_dir:
        clear_campaign_registries()
        db_path = Path(tmp_dir) / "price_history.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            """
            CREATE TABLE verified_campaign_registry (
                campaign_id TEXT PRIMARY KEY,
                scope_type TEXT NOT NULL,
                evidence_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        # Corrupt JSON record
        conn.execute(
            "INSERT INTO verified_campaign_registry VALUES (?, ?, ?, ?)",
            ("corrupt_camp_14", "nationwide", "{ not valid json ...", "2026-06-01T10:00:00"),
        )
        conn.commit()
        conn.close()

        with caplog.at_level(logging.WARNING):
            service = PriceHistoryService(Path(tmp_dir))

        assert is_verified_nationwide_campaign("corrupt_camp_14") is False
        assert any("Malformed JSON in verified_campaign_registry" in rec.message for rec in caplog.records)


def test_scenario_15_repeated_migrations_produce_consistent_results():
    """15. Repeated migrations produce consistent results."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        clear_campaign_registries()
        service = PriceHistoryService(Path(tmp_dir))
        with service._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO price_history (
                    product_key, title, retailer_slug, retailer_name, price, recorded_at,
                    source_url, zip_code, scope_type, region_verified, source_provider,
                    source_region_id, verification_method, verification_evidence
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)
                """,
                ("item-15", "Tee", "rewe", "REWE", 1.99, "2026-06-01T10:00:00",
                 "https://rewe.de", "10115", "store", "edeka-official", None, "store_direct_api", None),
            )
            conn.commit()

        d1 = service.revalidate_historical_records()
        d2 = service.revalidate_historical_records()
        d3 = service.revalidate_historical_records()

        assert d1 == 1  # First run demotes the record lacking store location evidence
        assert d2 == 0  # Second run is idempotent
        assert d3 == 0  # Third run is idempotent


def test_scenario_16_unverified_observations_never_contaminate_verified_stats_atls_aggregates():
    """16. Unverified observations never contaminate verified history, ATL, averages, counts, or aggregates."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        clear_campaign_registries()
        service = PriceHistoryService(Path(tmp_dir))

        ver_offer = Offer(
            id="ver-16",
            source_offer_id=161,
            retailer="EDEKA",
            retailer_slug="edeka",
            title="Bio Apfel 1kg",
            brand=None,
            category="produce",
            price=2.99,
            old_price=3.99,
            unit_price=2.99,
            unit="kg",
            discount_percent=25.0,
            description="",
            valid_from=None,
            valid_to=None,
            image_url=None,
            source_url="https://edeka.de",
            requested_zip_code="10115",
            scope_type="store",
            store_id="store_10115",
            store_address="Invalidenstr. 10, 10115 Berlin",
            source_provider="edeka-official",
            region_verified=True,
            verification_method="store_direct_api",
            verification_evidence=build_store_verification_evidence(
                store_id="store_10115",
                retailer_slug="edeka",
                store_address="Invalidenstr. 10, 10115 Berlin",
                requested_plz="10115",
                store_name="EDEKA Berlin",
                metadata_source="edeka_official_market_api",
                verification_method="store_direct_api",
            ),
        )

        unv_cheap = Offer(
            id="unv-16",
            source_offer_id=162,
            retailer="Discounter",
            retailer_slug="penny",
            title="Bio Apfel 1kg",
            brand=None,
            category="produce",
            price=0.29,  # Ridiculously cheap unverified price
            old_price=3.99,
            unit_price=0.29,
            unit="kg",
            discount_percent=92.0,
            description="",
            valid_from=None,
            valid_to=None,
            image_url=None,
            source_url="https://penny.de",
            requested_zip_code="10115",
            scope_type="unknown",
            region_verified=False,
            verification_method="legacy_unverified",
        )

        service.record_offers([ver_offer, unv_cheap], zip_code="10115")

        pkey = normalize_product_key("Bio Apfel 1kg")
        # Regional and national ATLs MUST be 2.99, NOT 0.29
        assert service.get_all_time_low_map(zip_code="10115")[pkey] == 2.99
        assert service.get_all_time_low_map(zip_code=None)[pkey] == 2.99

        stats = service.get_product_stats("Bio Apfel 1kg", current_price=2.99, zip_code="10115")
        assert stats["all_time_low"] == 2.99
        assert stats["regional_all_time_low"] == 2.99
        assert stats["national_all_time_low"] == 2.99
        assert stats["average_price"] == 2.99
        assert stats["record_count"] == 1
        assert len(stats["history"]) == 1
        assert stats["history"][0]["price"] == 2.99

        # Unverified history is separated
        assert len(stats["unverified_history"]) == 1
        assert stats["unverified_history"][0]["price"] == 0.29


def test_scenario_17_valid_evidence_backed_observations_continue_to_work():
    """17. Valid, evidence-backed observations continue to work."""
    clear_campaign_registries()
    with tempfile.TemporaryDirectory() as tmp_dir:
        service = PriceHistoryService(Path(tmp_dir))
        ev = {
            "campaign_id": "test_nat_15",
            "source_url": "https://www.marktguru.de/leaflets/test_nat_15",
            "evidence_type": "verified_nationwide_distribution_manifest",
            "verification_date": "2026-06-01",
            "scope": "nationwide",
            "promotional_applicability_confirmed": True,
            "limitations": "Full national coverage",
            "evidence_artifact_path": str(FIXTURE_EVIDENCE_DIR / "test_nat_15.json"),
        }
        assert register_verified_nationwide_campaign("test_nat_15", ev) is True

        offer = Offer(
            id="v-17",
            source_offer_id=17,
            retailer="REWE",
            retailer_slug="rewe",
            title="Hafermilch 1L",
            brand="Oatly",
            category="dairy",
            price=1.49,
            old_price=2.19,
            unit_price=1.49,
            unit="l",
            discount_percent=32.0,
            description="",
            valid_from="2026-06-01",
            valid_to="2026-06-07",
            image_url=None,
            source_url="https://marktguru.de/rewe",
            scope_type="nationwide",
            source_provider="marktguru",
            source_region_id="national_flyer:test_nat_15",
            region_verified=True,
            verification_method="marktguru_national_flyer",
            verification_evidence=ev,
        )
        service.record_offers([offer], zip_code="10115")

        stats = service.get_product_stats("Hafermilch 1L", brand="Oatly", current_price=1.49, zip_code="10115")
        assert stats["all_time_low"] == 1.49
        assert stats["record_count"] == 1
        assert len(stats["history"]) == 1
        assert stats["history"][0]["region_verified"] is True


def test_scenario_18_unverified_offers_remain_available_rather_than_discarded():
    """18. Unverified offers remain available to the user rather than being silently discarded."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        service = PriceHistoryService(Path(tmp_dir))
        unv_offer = Offer(
            id="unv-18",
            source_offer_id=18,
            retailer="Penny",
            retailer_slug="penny",
            title="Schokolade 100g",
            brand="Milka",
            category="sweets",
            price=0.79,
            old_price=1.49,
            unit_price=7.90,
            unit="kg",
            discount_percent=47.0,
            description="Tafel Schokolade",
            valid_from="2026-06-01",
            valid_to="2026-06-07",
            image_url=None,
            source_url="https://penny.de",
            requested_zip_code="10115",
            scope_type="unknown",
            region_verified=False,
            verification_method="legacy_unverified",
        )
        # Record unverified offer
        count = service.record_offers([unv_offer], zip_code="10115")
        assert count == 1

        # Query stats
        stats = service.get_product_stats("Schokolade 100g", brand="Milka", current_price=0.79, zip_code="10115")
        # Verified history is empty, but unverified history preserves the observation!
        assert len(stats["history"]) == 0
        assert len(stats["unverified_history"]) == 1
        assert stats["unverified_history"][0]["price"] == 0.79
        assert stats["unverified_history"][0]["retailer"] == "Penny"
        assert stats["unverified_history"][0]["region_verified"] is False


def test_scenario_19_artifact_missing_or_mismatched_campaign_id_rejected():
    """19. Artifact missing campaign_id or having mismatched campaign_id is strictly rejected."""
    clear_campaign_registries()
    art_no_id = FIXTURE_EVIDENCE_DIR / "test_artifact_no_id.json"
    art_mismatch_id = FIXTURE_EVIDENCE_DIR / "test_artifact_mismatch_id.json"
    try:
        # Artifact missing campaign_id
        art_no_id.write_text(
            json.dumps({
                "source_url": "https://www.marktguru.de/leaflets/camp_19",
                "scope": "nationwide",
                "promotional_campaign_applicability": True,
                "verification_date": "2026-06-01",
                "evidence_type": "verified_nationwide_distribution_manifest",
            }),
            encoding="utf-8",
        )
        ev_no_id = {
            "campaign_id": "camp_19",
            "source_url": "https://www.marktguru.de/leaflets/camp_19",
            "evidence_type": "verified_nationwide_distribution_manifest",
            "verification_date": "2026-06-01",
            "scope": "nationwide",
            "promotional_applicability_confirmed": True,
            "limitations": "Missing artifact campaign_id",
            "evidence_artifact_path": str(art_no_id),
        }
        assert register_verified_nationwide_campaign("camp_19", ev_no_id) is False

        # Artifact with mismatched campaign_id
        art_mismatch_id.write_text(
            json.dumps({
                "campaign_id": "different_campaign_id",
                "source_url": "https://www.marktguru.de/leaflets/camp_19",
                "scope": "nationwide",
                "promotional_campaign_applicability": True,
                "verification_date": "2026-06-01",
                "evidence_type": "verified_nationwide_distribution_manifest",
            }),
            encoding="utf-8",
        )
        ev_mismatch = {
            "campaign_id": "camp_19",
            "source_url": "https://www.marktguru.de/leaflets/camp_19",
            "evidence_type": "verified_nationwide_distribution_manifest",
            "verification_date": "2026-06-01",
            "scope": "nationwide",
            "promotional_applicability_confirmed": True,
            "limitations": "Mismatched artifact campaign_id",
            "evidence_artifact_path": str(art_mismatch_id),
        }
        assert register_verified_nationwide_campaign("camp_19", ev_mismatch) is False
    finally:
        art_no_id.unlink(missing_ok=True)
        art_mismatch_id.unlink(missing_ok=True)


def test_scenario_20_artifact_missing_or_mismatched_source_url_rejected():
    """20. Artifact missing source_url or having mismatched source_url is strictly rejected."""
    clear_campaign_registries()
    art_no_url = FIXTURE_EVIDENCE_DIR / "test_artifact_no_url.json"
    art_mismatch_url = FIXTURE_EVIDENCE_DIR / "test_artifact_mismatch_url.json"
    try:
        # Missing source_url
        art_no_url.write_text(
            json.dumps({
                "campaign_id": "camp_20",
                "scope": "nationwide",
                "promotional_campaign_applicability": True,
                "verification_date": "2026-06-01",
                "evidence_type": "verified_nationwide_distribution_manifest",
            }),
            encoding="utf-8",
        )
        ev_no_url = {
            "campaign_id": "camp_20",
            "source_url": "https://www.marktguru.de/leaflets/camp_20",
            "evidence_type": "verified_nationwide_distribution_manifest",
            "verification_date": "2026-06-01",
            "scope": "nationwide",
            "promotional_applicability_confirmed": True,
            "limitations": "Missing artifact source_url",
            "evidence_artifact_path": str(art_no_url),
        }
        assert register_verified_nationwide_campaign("camp_20", ev_no_url) is False

        # Mismatched source_url
        art_mismatch_url.write_text(
            json.dumps({
                "campaign_id": "camp_20",
                "source_url": "https://www.marktguru.de/leaflets/different_url",
                "scope": "nationwide",
                "promotional_campaign_applicability": True,
                "verification_date": "2026-06-01",
                "evidence_type": "verified_nationwide_distribution_manifest",
            }),
            encoding="utf-8",
        )
        ev_mismatch_url = {
            "campaign_id": "camp_20",
            "source_url": "https://www.marktguru.de/leaflets/camp_20",
            "evidence_type": "verified_nationwide_distribution_manifest",
            "verification_date": "2026-06-01",
            "scope": "nationwide",
            "promotional_applicability_confirmed": True,
            "limitations": "Mismatched artifact source_url",
            "evidence_artifact_path": str(art_mismatch_url),
        }
        assert register_verified_nationwide_campaign("camp_20", ev_mismatch_url) is False
    finally:
        art_no_url.unlink(missing_ok=True)
        art_mismatch_url.unlink(missing_ok=True)


def test_scenario_21_artifact_scope_meaning_bypass_rejected():
    """21. Artifact specifying scope_meaning without promotional_campaign_applicability: True is rejected."""
    clear_campaign_registries()
    art_scope_meaning = FIXTURE_EVIDENCE_DIR / "test_artifact_scope_meaning.json"
    try:
        art_scope_meaning.write_text(
            json.dumps({
                "campaign_id": "camp_21",
                "source_url": "https://www.marktguru.de/leaflets/camp_21",
                "scope": "nationwide",
                "scope_meaning": "promotional_campaign",  # Alternative field without boolean flag
                "verification_date": "2026-06-01",
                "evidence_type": "verified_nationwide_distribution_manifest",
            }),
            encoding="utf-8",
        )
        ev = {
            "campaign_id": "camp_21",
            "source_url": "https://www.marktguru.de/leaflets/camp_21",
            "evidence_type": "verified_nationwide_distribution_manifest",
            "verification_date": "2026-06-01",
            "scope": "nationwide",
            "promotional_applicability_confirmed": True,
            "limitations": "scope_meaning without boolean flag",
            "evidence_artifact_path": str(art_scope_meaning),
        }
        assert register_verified_nationwide_campaign("camp_21", ev) is False
    finally:
        art_scope_meaning.unlink(missing_ok=True)


def test_scenario_22_artifact_from_unapproved_directory_rejected(tmp_path):
    """22. An artifact created outside approved evidence directories is rejected as unapproved."""
    clear_campaign_registries()
    fake_art = tmp_path / "fake_self_asserted.json"
    fake_art.write_text(
        json.dumps({
            "campaign_id": "unapproved_camp_22",
            "source_url": "https://www.marktguru.de/leaflets/unapproved_camp_22",
            "scope": "nationwide",
            "promotional_campaign_applicability": True,
            "verification_date": "2026-06-01",
            "evidence_type": "verified_nationwide_distribution_manifest",
            "limitations": "Self asserted in tmp_path",
        }),
        encoding="utf-8",
    )
    ev = {
        "campaign_id": "unapproved_camp_22",
        "source_url": "https://www.marktguru.de/leaflets/unapproved_camp_22",
        "evidence_type": "verified_nationwide_distribution_manifest",
        "verification_date": "2026-06-01",
        "scope": "nationwide",
        "promotional_applicability_confirmed": True,
        "limitations": "Self asserted in tmp_path",
        "evidence_artifact_path": str(fake_art),
    }
    assert register_verified_nationwide_campaign("unapproved_camp_22", ev) is False


def test_scenario_23_store_verification_missing_store_address_with_store_plz_rejected():
    """23. Missing store address cannot be bypassed by store_plz or matched_plz in evidence."""
    ev = {
        "scope": "store",
        "store_id": "store_23",
        "store_plz": "10115",
        "matched_plz": "10115",
    }
    is_valid, reason, _ = validate_observation_provenance(
        scope_type="store",
        region_verified=True,
        verification_method="store_direct_api",
        store_id="store_23",
        zip_code="10115",
        source_provider="edeka-official",
        source_url="https://edeka.de",
        store_address=None,  # Missing store address!
        verification_evidence=ev,
    )
    assert is_valid is False
    assert reason == "demoted_missing_store_location_evidence"


def test_scenario_24_store_verification_mismatched_retailer_rejected():
    """24. Evidence indicating a different retailer than observation retailer is rejected."""
    ev = {
        "scope": "store",
        "store_id": "store_24",
        "retailer_slug": "rewe",  # Contradicts edeka observation
        "store_address": "Invalidenstr. 10, 10115 Berlin",
        "store_plz": "10115",
        "matched_plz": "10115",
    }
    is_valid, reason, _ = validate_observation_provenance(
        scope_type="store",
        region_verified=True,
        verification_method="store_direct_api",
        store_id="store_24",
        retailer_slug="edeka",
        zip_code="10115",
        source_provider="edeka-official",
        source_url="https://edeka.de",
        store_address="Invalidenstr. 10, 10115 Berlin",
        verification_evidence=ev,
    )
    assert is_valid is False
    assert reason == "demoted_retailer_evidence_mismatch"


def test_scenario_25_tampered_or_unregistered_json_in_approved_directory_fails():
    """25. Placing a fabricated or tampered JSON file inside an approved evidence directory fails validation."""
    clear_campaign_registries()
    clear_dynamic_ingestion_catalog()

    # 1. An unregistered JSON file inside FIXTURE_EVIDENCE_DIR
    unreg_path = FIXTURE_EVIDENCE_DIR / "unregistered_fabricated.json"
    try:
        unreg_path.write_text(
            json.dumps({
                "campaign_id": "fab_25",
                "source_url": "https://www.marktguru.de/leaflets/fab_25",
                "scope": "nationwide",
                "promotional_campaign_applicability": True,
                "verification_date": "2026-06-01",
                "evidence_type": "verified_nationwide_distribution_manifest",
            }),
            encoding="utf-8",
        )
        ev_unreg = {
            "campaign_id": "fab_25",
            "source_url": "https://www.marktguru.de/leaflets/fab_25",
            "evidence_type": "verified_nationwide_distribution_manifest",
            "verification_date": "2026-06-01",
            "scope": "nationwide",
            "promotional_applicability_confirmed": True,
            "limitations": "none",
            "evidence_artifact_path": str(unreg_path),
        }
        # Fails because unregistered artifact has no recorded cryptographic digest
        assert register_verified_nationwide_campaign("fab_25", ev_unreg) is False
    finally:
        unreg_path.unlink(missing_ok=True)

    # 2. A registered artifact whose bytes on disk were tampered with
    # Use copy of valid fixture with tampered content
    tampered_path = FIXTURE_EVIDENCE_DIR / "test_nat_15_tampered.json"
    try:
        tampered_path.write_text(
            json.dumps({
                "campaign_id": "test_nat_15",
                "source_url": "https://www.marktguru.de/leaflets/test_nat_15",
                "scope": "nationwide",
                "promotional_campaign_applicability": True,
                "verification_date": "2026-06-01",
                "evidence_type": "verified_nationwide_distribution_manifest",
            }),
            encoding="utf-8",
        )
        # Register in dynamic catalog with an expected hash
        ingest_trusted_evidence_artifact(tampered_path)
        # Now tamper the file content on disk
        tampered_path.write_text(
            json.dumps({
                "campaign_id": "test_nat_15",
                "source_url": "https://www.marktguru.de/leaflets/test_nat_15",
                "scope": "nationwide",
                "promotional_campaign_applicability": True,
                "verification_date": "2026-06-01",
                "evidence_type": "verified_nationwide_distribution_manifest",
                "tampered_extra_key": "injected",
            }),
            encoding="utf-8",
        )
        ev_tampered = {
            "campaign_id": "test_nat_15",
            "source_url": "https://www.marktguru.de/leaflets/test_nat_15",
            "evidence_type": "verified_nationwide_distribution_manifest",
            "verification_date": "2026-06-01",
            "scope": "nationwide",
            "promotional_applicability_confirmed": True,
            "limitations": "none",
            "evidence_artifact_path": str(tampered_path),
        }
        # Registration strictly fails cryptographic digest mismatch
        assert register_verified_nationwide_campaign("test_nat_15", ev_tampered) is False
    finally:
        tampered_path.unlink(missing_ok=True)
        clear_dynamic_ingestion_catalog()


def test_scenario_26_register_root_in_production_mode_forbidden(monkeypatch, tmp_path):
    """26. Attempting to register an unapproved evidence root in production mode is rejected."""
    clear_dynamic_approved_roots()
    monkeypatch.setenv("APP_ENV", "production")

    with pytest.raises(PermissionError, match="Registration of custom evidence roots is forbidden in production"):
        register_approved_evidence_root(tmp_path)


def test_scenario_27_store_address_with_requested_plz_without_authoritative_metadata_fails():
    """27. Supplying a store_address with the requested PLZ without authoritative store metadata fails verification."""
    is_valid, reason, msg = validate_observation_provenance(
        scope_type="store",
        region_verified=True,
        verification_method="store_direct_api",
        store_id="store_27",
        retailer_slug="edeka",
        zip_code="10115",
        source_provider="edeka-official",
        source_url="https://edeka.de",
        store_address="Invalidenstraße 100, 10115 Berlin",
        verification_evidence=None,  # Caller supplied address, but no authoritative metadata evidence
    )
    assert is_valid is False
    assert reason == "demoted_missing_store_evidence"
    assert "caller-supplied address string alone is rejected" in msg


def test_scenario_28_store_address_without_verified_store_id_link_fails():
    """28. Supplying a store_address without a verified store_id link in evidence fails verification."""
    ev = {
        "scope": "store",
        # Missing store_id link!
        "retailer_slug": "edeka",
        "store_address": "Invalidenstraße 100, 10115 Berlin",
        "store_plz": "10115",
        "matched_plz": "10115",
        "metadata_source": "edeka_official_market_api",
    }
    is_valid, reason, _ = validate_observation_provenance(
        scope_type="store",
        region_verified=True,
        verification_method="store_direct_api",
        store_id="store_28",
        retailer_slug="edeka",
        zip_code="10115",
        source_provider="edeka-official",
        source_url="https://edeka.de",
        store_address="Invalidenstraße 100, 10115 Berlin",
        verification_evidence=ev,
    )
    assert is_valid is False
    assert reason == "demoted_missing_store_id_evidence"


def test_scenario_29_missing_or_mismatched_store_id_or_retailer_in_store_evidence_fails():
    """29. Missing or mismatched store_id or retailer in store evidence fails verification."""
    # 1. Missing retailer identity
    ev_no_ret = {
        "scope": "store",
        "store_id": "store_29",
        "store_address": "Invalidenstraße 100, 10115 Berlin",
        "store_plz": "10115",
        "matched_plz": "10115",
        "metadata_source": "edeka_official_market_api",
    }
    is_valid_1, reason_1, _ = validate_observation_provenance(
        scope_type="store",
        region_verified=True,
        verification_method="store_direct_api",
        store_id="store_29",
        retailer_slug="edeka",
        zip_code="10115",
        source_provider="edeka-official",
        source_url="https://edeka.de",
        store_address="Invalidenstraße 100, 10115 Berlin",
        verification_evidence=ev_no_ret,
    )
    assert is_valid_1 is False
    assert reason_1 == "demoted_missing_retailer_evidence"

    # 2. Mismatched store_id
    ev_mismatch_id = {
        "scope": "store",
        "store_id": "other_store_id",
        "retailer_slug": "edeka",
        "store_address": "Invalidenstraße 100, 10115 Berlin",
        "store_plz": "10115",
        "matched_plz": "10115",
        "metadata_source": "edeka_official_market_api",
    }
    is_valid_2, reason_2, _ = validate_observation_provenance(
        scope_type="store",
        region_verified=True,
        verification_method="store_direct_api",
        store_id="store_29",
        retailer_slug="edeka",
        zip_code="10115",
        source_provider="edeka-official",
        source_url="https://edeka.de",
        store_address="Invalidenstraße 100, 10115 Berlin",
        verification_evidence=ev_mismatch_id,
    )
    assert is_valid_2 is False
    assert reason_2 == "demoted_store_evidence_mismatch"

    # 3. Mismatched retailer
    ev_mismatch_ret = {
        "scope": "store",
        "store_id": "store_29",
        "retailer_slug": "lidl",
        "store_address": "Invalidenstraße 100, 10115 Berlin",
        "store_plz": "10115",
        "matched_plz": "10115",
        "metadata_source": "edeka_official_market_api",
    }
    is_valid_3, reason_3, _ = validate_observation_provenance(
        scope_type="store",
        region_verified=True,
        verification_method="store_direct_api",
        store_id="store_29",
        retailer_slug="edeka",
        zip_code="10115",
        source_provider="edeka-official",
        source_url="https://edeka.de",
        store_address="Invalidenstraße 100, 10115 Berlin",
        verification_evidence=ev_mismatch_ret,
    )
    assert is_valid_3 is False
    assert reason_3 == "demoted_retailer_evidence_mismatch"


def test_scenario_30_invalid_calendar_dates_fail_closed_across_all_regional_paths(tmp_path):
    """30. Supplying invalid calendar dates (e.g. February 30) fails closed across all regional paths."""
    from app.providers.marktguru import (
        validate_regional_coverage_evidence,
        validate_regional_campaign_evidence,
    )

    # 1. RegionalCoverageEvidence with February 30
    ev_feb30 = RegionalCoverageEvidence(
        region_key="reg_feb30",
        covered_plzs=frozenset({"10115"}),
        source_url="https://www.marktguru.de/leaflets/101",
        evidence_type="verified_campaign_distribution_manifest",
        verification_date="2026-02-30",  # Invalid calendar date!
        limitations="none",
        promotional_applicability_confirmed=True,
    )
    ok_1, reason_1 = validate_regional_coverage_evidence(ev_feb30)
    assert ok_1 is False
    assert "Invalid or unparseable calendar date" in reason_1

    # 2. Regional campaign evidence with invalid month
    invalid_dict = {
        "campaign_id": "reg_camp_30",
        "source_url": "https://www.marktguru.de/leaflets/30",
        "evidence_type": "verified_campaign_distribution_manifest",
        "verification_date": "2026-13-45",  # Invalid calendar date!
        "scope": "region",
        "promotional_campaign_applicability": True,
        "covered_plzs": ["10115"],
    }
    ok_2, reason_2, _ = validate_regional_campaign_evidence("reg_camp_30", ["10115"], invalid_dict)
    assert ok_2 is False
    assert "Invalid or unparseable calendar date" in reason_2


def test_scenario_31_modifying_or_deleting_evidence_artifact_purges_registration(tmp_path):
    """31. Modifying or deleting an evidence artifact after registration causes subsequent queries to return False or None and purges the registration."""
    clear_campaign_registries()
    clear_dynamic_ingestion_catalog()
    clear_dynamic_approved_roots()

    register_approved_evidence_root(tmp_path)

    art_file = tmp_path / "dynamic_campaign_31.json"
    art_file.write_text(
        json.dumps({
            "campaign_id": "camp_31",
            "source_url": "https://www.marktguru.de/leaflets/camp_31",
            "scope": "nationwide",
            "promotional_campaign_applicability": True,
            "verification_date": "2026-06-01",
            "evidence_type": "verified_nationwide_distribution_manifest",
        }),
        encoding="utf-8",
    )
    ingest_trusted_evidence_artifact(art_file)

    ev = {
        "campaign_id": "camp_31",
        "source_url": "https://www.marktguru.de/leaflets/camp_31",
        "evidence_type": "verified_nationwide_distribution_manifest",
        "verification_date": "2026-06-01",
        "scope": "nationwide",
        "promotional_applicability_confirmed": True,
        "limitations": "none",
        "evidence_artifact_path": str(art_file),
    }

    # Successfully registered
    assert register_verified_nationwide_campaign("camp_31", ev) is True
    assert is_verified_nationwide_campaign("camp_31") is True

    # 1. Tamper file content -> query must return False and purge registration
    art_file.write_text(
        json.dumps({
            "campaign_id": "camp_31",
            "source_url": "https://www.marktguru.de/leaflets/camp_31",
            "scope": "nationwide",
            "tampered": True,
        }),
        encoding="utf-8",
    )
    assert is_verified_nationwide_campaign("camp_31") is False
    assert "camp_31" not in VERIFIED_NATIONWIDE_REGISTRY

    # 2. Re-register and delete file -> query must return False and purge registration
    art_file.write_text(
        json.dumps({
            "campaign_id": "camp_31",
            "source_url": "https://www.marktguru.de/leaflets/camp_31",
            "scope": "nationwide",
            "promotional_campaign_applicability": True,
            "verification_date": "2026-06-01",
            "evidence_type": "verified_nationwide_distribution_manifest",
        }),
        encoding="utf-8",
    )
    ingest_trusted_evidence_artifact(art_file)
    assert register_verified_nationwide_campaign("camp_31", ev) is True
    assert is_verified_nationwide_campaign("camp_31") is True

    art_file.unlink()
    assert is_verified_nationwide_campaign("camp_31") is False
    assert "camp_31" not in VERIFIED_NATIONWIDE_REGISTRY

    clear_dynamic_approved_roots()
    clear_dynamic_ingestion_catalog()


def test_scenario_32_public_registration_and_lookup_paths_reject_invalid_evidence():
    """32. Every public registration and lookup path rejects invalid evidence."""
    clear_campaign_registries()

    # Empty campaign ID
    assert register_verified_nationwide_campaign("", {}) is False
    assert register_verified_campaign("", ["10115"], {}) is False
    assert is_verified_nationwide_campaign("") is False
    assert get_verified_campaign_plzs("") is None
    assert get_verified_nationwide_evidence("") is None
    assert get_verified_campaign_evidence("") is None

    # None inputs
    assert register_verified_nationwide_campaign(None, None) is False
    assert register_verified_campaign(None, None, None) is False
    assert is_verified_nationwide_campaign(None) is False
    assert get_verified_campaign_plzs(None) is None
    assert get_verified_nationwide_evidence(None) is None
    assert get_verified_campaign_evidence(None) is None

    # Unregistered IDs
    assert is_verified_nationwide_campaign("unregistered_99999") is False
    assert get_verified_campaign_plzs("unregistered_99999") is None
    assert get_verified_nationwide_evidence("unregistered_99999") is None
    assert get_verified_campaign_evidence("unregistered_99999") is None


def test_scenario_33_legitimate_store_and_campaign_offers_remain_verified(tmp_path):
    """33. Legitimate store offers with verified store metadata and campaigns with verified artifacts remain verified."""
    clear_campaign_registries()
    service = PriceHistoryService(tmp_path)

    # 1. Legitimate nationwide campaign from audited fixture
    register_verified_nationwide_campaign(
        "901",
        {
            "campaign_id": "901",
            "source_url": "https://www.marktguru.de/leaflets/901",
            "evidence_type": "verified_nationwide_distribution_manifest",
            "verification_date": "2026-06-01",
            "scope": "nationwide",
            "promotional_applicability_confirmed": True,
            "limitations": "Audited nationwide promotion",
            "evidence_artifact_path": str(FIXTURE_EVIDENCE_DIR / "901.json"),
        },
    )
    assert is_verified_nationwide_campaign("901") is True

    # 2. Legitimate store offer with complete authoritative store metadata
    store_ev = build_store_verification_evidence(
        store_id="edeka_berlin_mitte",
        retailer_slug="edeka",
        store_address="Invalidenstraße 100, 10115 Berlin",
        requested_plz="10115",
        store_name="EDEKA Berlin Mitte",
        metadata_source="edeka_official_market_api",
        verification_method="store_direct_api",
    )
    valid_store_offer = Offer(
        id="valid-store-offer",
        source_offer_id=331,
        retailer="EDEKA",
        retailer_slug="edeka",
        title="Bio Butter 250g",
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
        store_id="edeka_berlin_mitte",
        store_name="EDEKA Berlin Mitte",
        store_address="Invalidenstraße 100, 10115 Berlin",
        source_provider="edeka-official",
        region_verified=True,
        verification_method="store_direct_api",
        verification_evidence=store_ev,
    )

    rec_count = service.record_offers([valid_store_offer], zip_code="10115")
    assert rec_count == 1

    stats = service.get_product_stats("Bio Butter 250g", brand="Kerrygold", zip_code="10115")
    assert stats["record_count"] == 1
    assert stats["all_time_low"] == 1.99
    assert len(stats["history"]) == 1
    assert stats["history"][0]["region_verified"] is True
    assert stats["history"][0]["retailer_slug"] == "edeka"

    with service._get_connection() as c:
        row = c.execute("SELECT region_verified, verification_method FROM price_history WHERE id = 1").fetchone()
        assert row["region_verified"] == 1
        assert row["verification_method"] == "store_direct_api"


def test_scenario_34_unverified_observations_never_contaminate_verified_stats_atls_charts(tmp_path):
    """34. Unverified observations never contaminate verified statistics, all-time lows, or historical charts."""
    clear_campaign_registries()
    service = PriceHistoryService(tmp_path)

    # Legitimate verified store offer at 3.99 EUR
    ver_offer = Offer(
        id="ver-34",
        source_offer_id=341,
        retailer="EDEKA",
        retailer_slug="edeka",
        title="Schokolade 100g",
        brand="Lindt",
        category="sweets",
        price=3.99,
        old_price=4.99,
        unit_price=39.90,
        unit="kg",
        discount_percent=20.0,
        description="Feinherb",
        valid_from="2026-06-01",
        valid_to="2026-06-07",
        image_url=None,
        source_url="https://edeka.de",
        requested_zip_code="10115",
        scope_type="store",
        store_id="edeka_10115",
        store_name="EDEKA Mitte",
        store_address="Invalidenstraße 100, 10115 Berlin",
        source_provider="edeka-official",
        region_verified=True,
        verification_method="store_direct_api",
        verification_evidence=build_store_verification_evidence(
            store_id="edeka_10115",
            retailer_slug="edeka",
            store_address="Invalidenstraße 100, 10115 Berlin",
            requested_plz="10115",
            store_name="EDEKA Mitte",
            metadata_source="edeka_official_market_api",
            verification_method="store_direct_api",
        ),
    )

    # 1. Unverified offer with fake address string
    unv_store_fake_addr = Offer(
        id="unv-store-fake",
        source_offer_id=342,
        retailer="Discounter",
        retailer_slug="penny",
        title="Schokolade 100g",
        brand="Lindt",
        category="sweets",
        price=0.29,  # Unverified low price
        old_price=4.99,
        unit_price=2.90,
        unit="kg",
        discount_percent=94.0,
        description="Feinherb",
        valid_from="2026-06-01",
        valid_to="2026-06-07",
        image_url=None,
        source_url="https://penny.de",
        requested_zip_code="10115",
        scope_type="store",
        store_id="penny_fake",
        store_address="Fake Address, 10115 Berlin",
        source_provider="penny",
        region_verified=False,
        verification_method="unverified_store",
    )

    # 2. Unverified nationwide offer
    unv_nat = Offer(
        id="unv-nat",
        source_offer_id=343,
        retailer="Online",
        retailer_slug="penny",
        title="Schokolade 100g",
        brand="Lindt",
        category="sweets",
        price=0.49,
        old_price=4.99,
        unit_price=4.90,
        unit="kg",
        discount_percent=90.0,
        description="Feinherb",
        valid_from="2026-06-01",
        valid_to="2026-06-07",
        image_url=None,
        source_url="https://penny.de",
        scope_type="nationwide",
        region_verified=False,
        verification_method="unverified_national",
    )

    service.record_offers([ver_offer, unv_store_fake_addr, unv_nat], zip_code="10115")

    # Product stats
    stats = service.get_product_stats("Schokolade 100g", brand="Lindt", current_price=3.99, zip_code="10115")
    # Verified statistics MUST strictly isolate unverified prices:
    assert stats["all_time_low"] == 3.99
    assert stats["average_price"] == 3.99
    assert stats["record_count"] == 1
    assert len(stats["history"]) == 1
    assert stats["history"][0]["price"] == 3.99
    assert stats["history"][0]["region_verified"] is True

    # Unverified history holds the 2 unverified records
    assert len(stats["unverified_history"]) == 2
    unv_prices = {h["price"] for h in stats["unverified_history"]}
    assert unv_prices == {0.29, 0.49}

    # All-time low maps
    pkey = normalize_product_key("Schokolade 100g", "Lindt")
    reg_atl_map = service.get_all_time_low_map(zip_code="10115")
    nat_atl_map = service.get_all_time_low_map(zip_code=None)
    assert reg_atl_map[pkey] == 3.99
    assert nat_atl_map[pkey] == 3.99







