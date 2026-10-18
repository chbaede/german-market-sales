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

    # 1. Valid authoritative coverage:
    # A. Explicit source postalCodes match
    v, method, reason, reg_id = verify_marktguru_regional_applicability(
        "10115",
        {"postalCodes": ["10115", "10117"], "regionId": 10},
        {},
    )
    assert v is True
    assert method == "marktguru_regional_flyer_postal_match"
    assert reg_id == "10"

    # B. Documented authoritative region mapping ("berlin_north" covers 10115)
    v, method, reason, reg_id = verify_marktguru_regional_applicability(
        "10115",
        {"regionId": "berlin_north"},
        {},
    )
    assert v is True
    assert method == "marktguru_authoritative_region_mapping"
    assert reg_id == "berlin_north"

    # 2. Nearby-but-uncovered PLZs fail closed:
    # A. Flyer covers 10115 and 10117; user is in neighboring 10119 in Berlin Mitte (~1-2km away)
    v, method, reason, reg_id = verify_marktguru_regional_applicability(
        "10119",
        {"postalCodes": ["10115", "10117"], "regionId": 10},
        {},
    )
    assert v is False
    assert method == "marktguru_unverified_flyer"
    assert "postal codes do not cover" in reason

    # B. Flyer has coordinates nearby (e.g. 1.5km away) but lacks postal codes and authoritative mapping
    v, method, reason, reg_id = verify_marktguru_regional_applicability(
        "10115",
        {"coordinates": {"lat": 52.53, "lon": 13.38}, "id": 555},
        {},
    )
    assert v is False
    assert method == "marktguru_unverified_flyer"

    # 3. Misleading city names and prefix heuristics fail closed:
    # A. City name "Berlin Special Promo" or "Berlin Mitte" without explicit postal codes
    v, method, reason, reg_id = verify_marktguru_regional_applicability(
        "10115",
        {"regionName": "Berlin Special Promo"},
        {},
    )
    assert v is False
    assert method == "marktguru_unverified_flyer"

    # B. Two-digit PLZ prefix heuristic ("10") without full 5-digit coverage
    v, method, reason, reg_id = verify_marktguru_regional_applicability(
        "10115",
        {"region": {"plzPrefixes": ["10", "12"]}},
        {},
    )
    assert v is False
    assert method == "marktguru_unverified_flyer"

    # 4. Mismatched regions:
    # A. Leaflet postalCodes do not cover requested PLZ (Munich leaflet for Berlin user)
    v, method, reason, reg_id = verify_marktguru_regional_applicability(
        "10115",
        {"postalCodes": ["80331", "80333"], "regionId": "munich_central"},
        {},
    )
    assert v is False
    assert method == "marktguru_unverified_flyer"
    assert "postal codes do not cover" in reason

    # B. Leaflet region name/id explicitly for another city (Munich region for Berlin user)
    v, method, reason, reg_id = verify_marktguru_regional_applicability(
        "10115",
        {"regionId": "munich_south", "regionName": "München Süd"},
        {},
    )
    assert v is False
    assert method == "marktguru_unverified_flyer"
    assert "mismatches requested PLZ" in reason

    # 5. Missing mapping metadata:
    # Arbitrary regionId exists, but no postal codes, no coordinates, no mapping table entry
    v, method, reason, reg_id = verify_marktguru_regional_applicability(
        "10115",
        {"regionId": 42},
        {},
    )
    assert v is False
    assert method == "marktguru_unverified_flyer"
    assert reg_id == "42"
    assert "lacks authoritative geographic coverage mapping" in reason

    # Leaflet with only an arbitrary ID and no geographic metadata
    v, method, reason, reg_id = verify_marktguru_regional_applicability(
        "10115",
        {"id": 9999},
        {},
    )
    assert v is False
    assert method == "marktguru_unverified_flyer"


