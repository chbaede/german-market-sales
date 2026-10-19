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


def test_kaufland_sanitizes_html_line_separators_and_entities_in_title():
    from app.models import Offer
    from app.providers.base import clean_text
    from app.services.price_history import normalize_product_key

    # 1. clean_text replaces &#x2028;, $#2028;, $#x2028;, etc. with spaces
    assert clean_text("VIP BY SID MAURER&#x2028;Herren-T-Shirt") == "VIP BY SID MAURER Herren-T-Shirt"
    assert clean_text("VIP BY SID MAURER$#2028;Herren-T-Shirt") == "VIP BY SID MAURER Herren-T-Shirt"
    assert clean_text("VIP BY SID MAURER$#x2028;Herren-T-Shirt") == "VIP BY SID MAURER Herren-T-Shirt"
    assert clean_text("7 DAYS&#x2028;Croissants") == "7 DAYS Croissants"
    assert clean_text("Adventskalender&#x2028;»PAW Patrol«") == "Adventskalender »PAW Patrol«"
    assert clean_text("&amp;#x2028;VISS&#x2028;Scheuermilch") == "VISS Scheuermilch"
    assert clean_text("Bio &amp; Regional &quot;Milch&quot;") == 'Bio & Regional "Milch"'

    # 2. Offer dataclass cleans title automatically upon creation
    offer = Offer(
        id="test-1",
        source_offer_id=1,
        retailer="Kaufland",
        retailer_slug="kaufland",
        title="VIP BY SID MAURER&#x2028;Herren-T-Shirt",
        brand=None,
        category="nonfood",
        price=4.99,
        old_price=None,
        unit_price=None,
        unit=None,
        discount_percent=None,
        description="VISS&#x2028;Scheuermilch",
        valid_from=None,
        valid_to=None,
        image_url=None,
        source_url="https://kaufland.de",
    )
    assert offer.title == "VIP BY SID MAURER Herren-T-Shirt"
    assert offer.description == "VISS Scheuermilch"

    # 3. normalize_product_key does not include x2028 in product key
    pkey = normalize_product_key("VIP BY SID MAURER&#x2028;Herren-T-Shirt")
    assert "2028" not in pkey
    assert pkey == "by-herren-maurer-sid-vip"

    # 4. Parsing Kaufland payload with &#x2028; in title
    payload = {
        "component": "OfferTemplate",
        "props": {
            "offerData": {
                "cycles": [
                    {
                        "categories": [
                            {
                                "name": "Textilien",
                                "offers": [
                                    {
                                        "offerId": "ART.1188250",
                                        "detailTitle": "VIP BY SID MAURER&#x2028;Herren-T-Shirt",
                                        "price": "4.99",
                                        "dateFrom": "2026-05-28",
                                        "dateTo": "2026-06-03",
                                    }
                                ],
                            }
                        ]
                    }
                ]
            }
        },
    }
    offers = KauflandProvider.offers_from_payload(payload, limit=5, today=date(2026, 5, 30))
    assert len(offers) == 1
    assert offers[0].title == "VIP BY SID MAURER Herren-T-Shirt"
    assert "2028" not in offers[0].title

