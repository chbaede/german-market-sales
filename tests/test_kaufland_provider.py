from datetime import date

from app.providers.kaufland import KauflandProvider


def test_kaufland_extracts_offer_template_payload():
    html = """
    <html>
      <script>
        window.SSR = window.SSR || {};
        window.SSR['abc'] = {"component":"Other","props":{}};
      </script>
      <script>
        window.SSR['def'] = {"component":"OfferTemplate","props":{"offerData":{"cycles":[]}}};
      </script>
    </html>
    """

    payload = KauflandProvider.extract_offer_template_payload(html)

    assert payload["component"] == "OfferTemplate"


def test_kaufland_maps_current_and_next_offers_from_official_payload():
    payload = {
        "component": "OfferTemplate",
        "props": {
            "offerData": {
                "cycles": [
                    {
                        "categories": [
                            {
                                "displayName": "Obst, Gemüse, Pflanzen",
                                "offers": [
                                    {
                                        "offerId": "ART.303476_KAV.3446397",
                                        "dateFrom": "2026-05-28",
                                        "dateTo": "2026-06-03",
                                        "detailTitle": "Ecuador./kolumb. Bananen, lose",
                                        "price": 0.99,
                                        "discount": 23,
                                        "unit": "je kg",
                                        "detailDescription": "Kl. I",
                                        "formattedOldPrice": "1.29",
                                        "formattedBasePrice": "(1 kg = 0.99)",
                                        "detailImages": ["https://kaufland.media.schwarz/is/image/schwarz/00397356FRa-1"],
                                    },
                                    {
                                        "offerId": "ART.311601_KAV.3446397",
                                        "dateFrom": "2026-06-04",
                                        "dateTo": "2026-06-10",
                                        "detailTitle": "Span./ital. Möhren",
                                        "formattedPrice": "1.99",
                                        "formattedOldPrice": "2.49",
                                        "unit": "je 2-kg-Beutel",
                                        "basePrice": "(1 kg = 1.00)",
                                        "listImage": "https://kaufland.media.schwarz/is/image/schwarz/00949534FR",
                                    },
                                ],
                            }
                        ]
                    }
                ]
            }
        },
    }

    offers = KauflandProvider.offers_from_payload(payload, limit=10, today=date(2026, 5, 30))

    assert len(offers) == 2
    current, next_offer = offers
    assert current.retailer == "Kaufland"
    assert current.retailer_slug == "kaufland"
    assert current.title == "Ecuador./kolumb. Bananen, lose"
    assert current.price == 0.99
    assert current.old_price == 1.29
    assert current.discount_percent == 23
    assert current.unit_price == 0.99
    assert current.unit == "1 kg"
    assert current.valid_from == "2026-05-28T00:00:00+02:00"
    assert current.valid_to == "2026-06-03T23:59:59+02:00"
    assert current.image_url == "https://kaufland.media.schwarz/is/image/schwarz/00397356FRa-1"
    assert "Obst, Gemüse, Pflanzen" in current.description

    assert next_offer.title == "Span./ital. Möhren"
    assert next_offer.price == 1.99
    assert next_offer.discount_percent == 20.1
    assert next_offer.valid_from == "2026-06-04T00:00:00+02:00"
