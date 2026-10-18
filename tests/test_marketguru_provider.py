import json

from app.providers.marktguru import MarktguruProvider
from app.services.categories import categorize_offer


def test_extract_bootstrap_json():
    payload = {"config": {"apiHostAddress": "api.example.test"}}
    html = f'<html><script type="application/json">{json.dumps(payload)}</script></html>'

    assert MarktguruProvider.extract_bootstrap_json(html) == payload


def test_offer_from_api_maps_core_fields():
    item = {
        "id": 123,
        "description": "250-g-Becher",
        "product": {"name": "Naschtomate"},
        "retailer": {"name": "ALDI SÜD", "uniqueName": "aldi-sued"},
        "brand": {"name": "thisisnobrand123"},
        "unit": {"shortName": "kg"},
        "price": 1.49,
        "oldPrice": 1.99,
        "referencePrice": 5.96,
        "validFrom": "2026-05-25T22:00:00Z",
        "validTo": "2026-05-30T21:59:00Z",
        "images": {"count": 1},
    }

    offer = MarktguruProvider._offer_from_api(item, "aldi-sued")

    assert offer.id == "marktguru-123"
    assert offer.title == "Naschtomate"
    assert offer.brand is None
    assert offer.category == "produce"
    assert offer.discount_percent == 25.1
    assert offer.image_url.endswith("/123/images/default/0/medium.webp")


def test_category_detection_for_household_items():
    assert categorize_offer("Universal Reiniger", description="Küche und Bad") == "household"
    assert categorize_offer("Pampers Windeln") == "baby"
    assert categorize_offer("Sneaker Aktion") == "nonfood"
    assert categorize_offer("Socken aus Grobstrick") == "nonfood"
    assert categorize_offer("Regenjacke gefüttert mit Schmetterling-Muster") == "nonfood"
    assert categorize_offer("Speiseteller NORA aus Kunststoff") == "nonfood"
    assert categorize_offer("Duftlichter frostige Erdbeere", description="Deko Limited Edition") == "nonfood"
    assert categorize_offer("Foundation Buttermelt Glaze mit LSF 30") == "personal_care"
    assert categorize_offer("Swirl Staubfilterbeutel MicroPor Plus", description="Rabattierter Preis") == "household"
    assert categorize_offer("Eis-Box") == "pantry"


def test_aldi_sued_preserves_requested_zip_without_rewriting_to_80331(monkeypatch):
    from app.providers.marktguru import MarktguruConfig

    provider = MarktguruProvider()
    provider._config = MarktguruConfig(api_host="api.example.test", api_key="k", client_key="c")
    captured_params = {}

    def fake_request_json(url, params, headers):
        captured_params.update(params)
        return {"results": []}

    monkeypatch.setattr(provider, "_request_json", fake_request_json)
    provider.fetch_offers("aldi-sued", zip_code="14195", limit=50)

    # Requested PLZ must be strictly preserved without silent replacement
    assert captured_params["zipCode"] == "14195"


def test_marktguru_regional_verification_mappings():
    from app.providers.marktguru import verify_marktguru_regional_applicability

    # 1. Valid authoritative campaign coverage:
    # A. Explicit validZipCodes + confirmed promotional campaign scope + flyer ID
    v, method, reason, reg_id = verify_marktguru_regional_applicability(
        "10115",
        {
            "id": 888,
            "validZipCodes": ["10115", "10117"],
            "coverageScope": "promotional_campaign",
            "regionId": 10,
        },
        {},
    )
    assert v is True
    assert method == "marktguru_regional_flyer_postal_match"
    assert reg_id == "10"

    # B. Explicit campaignDistribution manifest with confirmed applicability and validZipCodes
    v, method, reason, reg_id = verify_marktguru_regional_applicability(
        "10115",
        {
            "id": 889,
            "campaignDistribution": {
                "campaignId": "camp-889",
                "scope": "promotional_campaign",
                "validZipCodes": ["10115", "10117"],
            },
        },
        {},
    )
    assert v is True
    assert method == "marktguru_regional_flyer_postal_match"
    assert reg_id == "flyer:889"

    # 2. Generic postal-code metadata without confirmed campaign applicability -> diagnostic match ONLY (region_verified=False)
    for field_name in ("postalCodes", "zipCodes", "coveragePostalCodes", "validZipCodes"):
        v, method, reason, reg_id = verify_marktguru_regional_applicability(
            "10115",
            {"id": 900, field_name: ["10115", "10117"], "regionId": 10},
            {},
        )
        assert v is False
        assert method == "marktguru_diagnostic_region_match"
        assert reg_id == "10"
        assert "Diagnostic PLZ match" in reason

    # Nested region.postalCodes also treated as diagnostic only
    v, method, reason, reg_id = verify_marktguru_regional_applicability(
        "10115",
        {"id": 901, "region": {"id": "r10", "postalCodes": ["10115"]}},
        {},
    )
    assert v is False
    assert method == "marktguru_diagnostic_region_match"
    assert reg_id == "r10"

    # 3. Unsupported "berlin_north" mapping removed -> fails closed with region_verified=False
    v, method, reason, reg_id = verify_marktguru_regional_applicability(
        "10115",
        {"regionId": "berlin_north"},
        {},
    )
    assert v is False
    assert method == "marktguru_unverified_flyer"
    assert reg_id == "berlin_north"
    assert "lacks authoritative geographic coverage mapping" in reason

    # 4. Nearby-but-uncovered PLZs fail closed:
    # A. Confirmed campaign flyer covers 10115 and 10117; user is in neighboring 10119
    v, method, reason, reg_id = verify_marktguru_regional_applicability(
        "10119",
        {
            "id": 888,
            "validZipCodes": ["10115", "10117"],
            "coverageScope": "promotional_campaign",
            "regionId": 10,
        },
        {},
    )
    assert v is False
    assert method == "marktguru_unverified_flyer"
    assert "postal codes do not cover" in reason

    # B. Flyer has coordinates nearby (e.g. 1.5km away) but lacks confirmed campaign coverage
    v, method, reason, reg_id = verify_marktguru_regional_applicability(
        "10115",
        {"coordinates": {"lat": 52.53, "lon": 13.38}, "id": 555},
        {},
    )
    assert v is False
    assert method == "marktguru_unverified_flyer"

    # 5. Misleading city names and prefix heuristics fail closed:
    v, method, reason, reg_id = verify_marktguru_regional_applicability(
        "10115",
        {"regionName": "Berlin Special Promo"},
        {},
    )
    assert v is False
    assert method == "marktguru_unverified_flyer"

    v, method, reason, reg_id = verify_marktguru_regional_applicability(
        "10115",
        {"region": {"plzPrefixes": ["10", "12"]}},
        {},
    )
    assert v is False
    assert method == "marktguru_unverified_flyer"

    # 6. Mismatched regions:
    v, method, reason, reg_id = verify_marktguru_regional_applicability(
        "10115",
        {"postalCodes": ["80331", "80333"], "regionId": "munich_central"},
        {},
    )
    assert v is False
    assert method == "marktguru_unverified_flyer"
    assert "postal codes do not cover" in reason

    v, method, reason, reg_id = verify_marktguru_regional_applicability(
        "10115",
        {"regionId": "munich_south", "regionName": "München Süd"},
        {},
    )
    assert v is False
    assert method == "marktguru_unverified_flyer"
    assert "mismatches requested PLZ" in reason

    # 7. Missing mapping metadata:
    v, method, reason, reg_id = verify_marktguru_regional_applicability(
        "10115",
        {"regionId": 42},
        {},
    )
    assert v is False
    assert method == "marktguru_unverified_flyer"
    assert reg_id == "42"
    assert "lacks authoritative geographic coverage mapping" in reason

    v, method, reason, reg_id = verify_marktguru_regional_applicability(
        "10115",
        {"id": 9999},
        {},
    )
    assert v is False
    assert method == "marktguru_unverified_flyer"


def test_audited_regional_coverage_registry_traceability(tmp_path, monkeypatch):
    from app.providers.marktguru import (
        AUTHORITATIVE_REGION_REGISTRY,
        AUTHORITATIVE_REGION_COVERAGE_MAP,
        RegionalCoverageEvidence,
        validate_regional_coverage_evidence,
        verify_marktguru_regional_applicability,
    )

    # 1. Default registry is empty because no external Marktguru distribution-zone API
    #    endpoint is backed by accessible local manifest artifacts in the repo.
    #    Specifically, "berlin_north" must NOT be registered.
    assert "berlin_north" not in AUTHORITATIVE_REGION_REGISTRY
    assert "berlin_north" not in AUTHORITATIVE_REGION_COVERAGE_MAP

    # 2. Unsupported keys fail closed
    unsupported_keys = [
        "berlin_north",
        "muenchen_zentrum",
        "koeln_innenstadt",
        "hamburg_mitte",
        "frankfurt_innenstadt",
    ]
    for unverified_key in unsupported_keys:
        assert unverified_key not in AUTHORITATIVE_REGION_REGISTRY
        v, method, reason, _ = verify_marktguru_regional_applicability(
            "10115",
            {"regionId": unverified_key},
            {},
        )
        assert v is False
        assert method == "marktguru_unverified_flyer"

    # 3. Registry entry with a plausible URL but missing local artifact file MUST fail validation
    missing_artifact_ev = RegionalCoverageEvidence(
        region_key="test_zone_missing_file",
        covered_plzs=frozenset({"10115", "10117"}),
        source_url="https://api.marktguru.de/api/v1/distribution-zones/fake",
        evidence_type="verified_campaign_distribution_manifest",
        verification_date="2026-06-01",
        limitations="Missing artifact file",
        evidence_artifact_path=str(tmp_path / "nonexistent_manifest.json"),
        promotional_applicability_confirmed=True,
    )
    ok, err = validate_regional_coverage_evidence(missing_artifact_ev)
    assert ok is False
    assert "does not exist" in err

    monkeypatch.setitem(AUTHORITATIVE_REGION_REGISTRY, "test_zone_missing_file", missing_artifact_ev)
    v, method, reason, _ = verify_marktguru_regional_applicability(
        "10115",
        {"regionId": "test_zone_missing_file"},
        {},
    )
    assert v is False
    assert method == "marktguru_unverified_flyer"
    assert "mapping rejected because evidence is invalid" in reason

    # 4. Registry entry with an existing, valid manifest artifact succeeds for covered PLZ and fails for uncovered PLZ
    manifest_file = tmp_path / "valid_manifest.json"
    manifest_file.write_text(
        json.dumps(
            {
                "region_key": "verified_test_zone",
                "source_url": "https://www.marktguru.de/r/edeka",
                "campaign_id": "camp-verified-100",
                "scope_meaning": "promotional_campaign_applicability",
                "covered_plzs": ["10115", "10117"],
            }
        ),
        encoding="utf-8",
    )
    valid_ev = RegionalCoverageEvidence(
        region_key="verified_test_zone",
        covered_plzs=frozenset({"10115", "10117"}),
        source_url="https://www.marktguru.de/r/edeka",
        evidence_type="verified_campaign_distribution_manifest",
        verification_date="2026-06-01",
        limitations="Applies only to campaign camp-verified-100",
        evidence_artifact_path=str(manifest_file),
        promotional_applicability_confirmed=True,
    )
    ok_valid, err_valid = validate_regional_coverage_evidence(valid_ev)
    assert ok_valid is True
    assert err_valid == "Verified promotional campaign distribution manifest"

    monkeypatch.setitem(AUTHORITATIVE_REGION_REGISTRY, "verified_test_zone", valid_ev)
    v_cov, method_cov, _, reg_id_cov = verify_marktguru_regional_applicability(
        "10115",
        {"regionId": "verified_test_zone"},
        {},
    )
    assert v_cov is True
    assert method_cov == "marktguru_authoritative_region_mapping"
    assert reg_id_cov == "verified_test_zone"

    v_uncov, method_uncov, reason_uncov, _ = verify_marktguru_regional_applicability(
        "10178",
        {"regionId": "verified_test_zone"},
        {},
    )
    assert v_uncov is False
    assert method_uncov == "marktguru_unverified_flyer"
    assert "authoritative coverage does not include PLZ 10178" in reason_uncov



