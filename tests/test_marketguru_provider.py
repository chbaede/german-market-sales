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
