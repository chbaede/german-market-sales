import json

from app.providers.aldi_nord import AldiNordProvider


def test_aldi_nord_next_data_maps_current_offers():
    api_data = [
        [
            "OFFER_GET",
            {
                "req": {"locale": "de", "week": "current"},
                "res": {
                    "algoliaDataMap": {
                        "1024": {
                            "isAvailable": True,
                            "brandName": "WAGNER",
                            "currentPrice": {
                                "priceValue": 1.99,
                                "strikePrice": {"strikePriceValue": 3.69, "strikePriceLabel": "UVP"},
                                "basePrice": [{"basePriceValue": 7.37, "basePriceScale": "kg"}],
                                "priceTagLabels": {"promoText1": "-46 %"},
                                "validFrom": 1779746400,
                                "validUntil": 1780178399,
                            },
                            "shortDescription": "9 Stück",
                            "salesUnit": "270-g-Packung",
                            "assets": [
                                {
                                    "type": "primary",
                                    "url": "https://s7g10.scene7.com/is/image/aldinord/1024_22_2026",
                                }
                            ],
                            "name": "Piccolinis Drei Käse",
                            "objectID": "1024",
                        }
                    },
                    "categories": [
                        {
                            "title": "Aktion Di. 26.5.",
                            "startDate": "2026-05-25",
                            "endDate": "2026-05-30",
                            "content": [{"title": "Wochen-Angebote", "productIds": ["1024"]}],
                        }
                    ],
                },
            },
        ]
    ]
    next_data = {"props": {"pageProps": {"apiData": json.dumps(api_data)}}}

    offers = AldiNordProvider.offers_from_next_data(next_data, limit=10)

    assert len(offers) == 1
    offer = offers[0]
    assert offer.id == "aldi-nord-1024"
    assert offer.retailer == "ALDI Nord"
    assert offer.retailer_slug == "aldi-nord"
    assert offer.title == "Piccolinis Drei Käse"
    assert offer.brand == "WAGNER"
    assert offer.price == 1.99
    assert offer.old_price == 3.69
    assert offer.unit_price == 7.37
    assert offer.unit == "kg"
    assert offer.discount_percent == 46.0
    assert offer.valid_from == "2026-05-26T00:00:00+02:00"
    assert offer.valid_to == "2026-05-30T23:59:59+02:00"
    assert offer.image_url.endswith("1024_22_2026")
