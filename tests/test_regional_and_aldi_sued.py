from __future__ import annotations

import json
from pathlib import Path

from app import create_app
from app.models import Offer
from app.providers.aldi_sued import AldiSuedProvider
from app.providers.edeka import EdekaProvider
from app.providers.kaufland import KauflandProvider
from app.services.location import get_location_info, normalize_german_plz
from app.services.offers import OfferService


def test_aldi_sued_parses_nuxt_data_without_boolean_index_collision() -> None:
    nuxt_payload = [
        ["ShallowReactive", 1],
        {"products": 2},
        [3],
        {
            "sku": 4,
            "name": 5,
            "brandName": 6,
            "isBio": 7,
            "onSaleDate": 8,
            "urlSlugText": 9,
            "price": 10,
            "assets": 16,
        },
        "00012345",
        "Bio-Alpenmilch 3,8%",
        "NUR NUR NATUR",
        False,
        1791763200000,  # 2026-10-12T00:00:00Z
        "bio-alpenmilch",
        {
            "amountRelevant": 11,
            "wasPriceDisplay": 12,
            "savingsDisplay": 13,
            "perUnitDisplay": 14,
            "sellingSizeDisplay": 15,
        },
        1.19,
        "€ 1,49",
        "-20%",
        "1 l = € 1,19",
        "1-l-Packung",
        [{"url": 17}],
        "https://dm.emea.cms.aldi.cx/is/image/aldiprodeu/product/jpg/scaleWidth/500/test.jpg",
    ]
    html = f'<html><body><script id="__NUXT_DATA__" type="application/json">{json.dumps(nuxt_payload)}</script></body></html>'

    offers = AldiSuedProvider.offers_from_html(html, limit=10)
    assert len(offers) == 1
    offer = offers[0]
    assert offer.id == "aldi-sued-00012345"
    assert offer.retailer == "ALDI Süd"
    assert offer.retailer_slug == "aldi-sued"
    assert offer.title == "Bio-Alpenmilch 3,8%"
    assert offer.brand == "NUR NUR NATUR"
    assert offer.price == 1.19
    assert offer.old_price == 1.49
    assert offer.discount_percent == 20.0
    assert offer.unit_price == 1.19
    assert offer.unit == "1 l"
    assert offer.valid_from == "2026-10-12T00:00:00+02:00"
    assert offer.valid_to == "2026-10-17T23:59:59+02:00"
    assert offer.source_url == "https://www.aldi-sued.de/de/p.bio-alpenmilch.00012345.html"


def test_aldi_sued_extract_date_urls_includes_wochenangebote() -> None:
    sample_html = (
        '<a href="/angebote/2026-10-19">Mo</a>'
        '<a href="/produkte/wochenangebote/k/1588161426582123">Wochenangebote</a>'
        '<a href="/produkte/wochenangebote/frischeprodukte-im-angebot/k/1588161427299187">Frische</a>'
    )
    urls = AldiSuedProvider._extract_date_urls(sample_html)
    assert "https://www.aldi-sued.de/angebote/2026-10-19" in urls
    assert "https://www.aldi-sued.de/produkte/wochenangebote/k/1588161426582123" in urls
    assert "https://www.aldi-sued.de/produkte/wochenangebote/frischeprodukte-im-angebot/k/1588161427299187" in urls


def test_normalize_postfach_plz_and_location_info() -> None:
    assert normalize_german_plz("80313") == "80331"
    assert normalize_german_plz(None, city_hint="München") == "80331"
    info = get_location_info("80313")
    assert info["zip_code"] == "80331"
    assert info["city"] == "München"


def test_edeka_candidate_markets_falls_back_when_postfach_plz_is_empty(monkeypatch) -> None:
    provider = EdekaProvider(timeout=5, verify_tls=True)
    queried: list[str] = []

    def fake_request_json(url: str, params: dict | None = None):
        search = (params or {}).get("searchstring", "")
        queried.append(search)
        if search == "80313":
            return {"markets": []}, False
        if search == "80331":
            return {
                "markets": [
                    {
                        "name": "EDEKA van Dungen",
                        "url": "https://www.edeka.de/eh/suedbayern/edeka-van-dungen-otto-seidl-strasse-7/index.jsp",
                        "isEdekaHomepageMarket": True,
                    }
                ]
            }, False
        return {"markets": []}, False

    monkeypatch.setattr(provider, "_request_json", fake_request_json)
    markets, _ = provider._candidate_markets("80313")
    assert len(markets) == 1
    assert markets[0]["name"] == "EDEKA van Dungen"
    assert queried == ["80313", "80331"]


def test_kaufland_resolves_regional_store_id_by_plz(monkeypatch) -> None:
    provider = KauflandProvider(timeout=5, verify_tls=True)
    monkeypatch.setattr(
        provider,
        "_load_stores",
        lambda: [
            {"n": "DE1110", "pc": "10407", "t": "Berlin"},
            {"n": "DE1633", "pc": "80339", "t": "München"},
            {"n": "DE2400", "pc": "60314", "t": "Frankfurt"},
        ],
    )
    assert provider._resolve_store_id("80331") == "DE1633"
    assert provider._resolve_store_id("10115") == "DE1110"
    assert provider._resolve_store_id("60311") == "DE2400"


def test_offer_service_caches_multiple_zip_codes_independently(tmp_path: Path, monkeypatch) -> None:
    service = OfferService(
        cache_dir=tmp_path,
        retailers={"edeka": "EDEKA"},
        cache_ttl_seconds=3600,
        timeout=5,
        verify_tls=True,
        max_offers_per_retailer=10,
    )

    def fake_fetch(retailer_slug: str, zip_code: str):
        return (
            [
                Offer(
                    id=f"edeka-{zip_code}",
                    source_offer_id=1,
                    retailer="EDEKA",
                    retailer_slug="edeka",
                    title=f"Regional Milch {zip_code}",
                    brand="EDEKA",
                    category="dairy",
                    price=0.99 if zip_code == "80331" else 1.09,
                    old_price=1.29,
                    unit_price=None,
                    unit=None,
                    discount_percent=23.0,
                    description=f"PLZ {zip_code}",
                    valid_from=None,
                    valid_to=None,
                    image_url=None,
                    source_url="https://www.edeka.de",
                )
            ],
            [],
        )

    monkeypatch.setattr(service, "_fetch_retailer_offers", fake_fetch)

    berlin = service.get_offers("14195", refresh=True)
    munich = service.get_offers("80331", refresh=True)

    assert berlin.offers[0].price == 1.09
    assert munich.offers[0].price == 0.99
    assert (tmp_path / "offers.json").exists()
    assert (tmp_path / "offers_80331.json").exists()

    # Both caches remain warm without overwriting each other
    cached_berlin = service.get_cached_offers("14195")
    cached_munich = service.get_cached_offers("80331")
    assert cached_berlin is not None and cached_berlin.offers[0].price == 1.09
    assert cached_munich is not None and cached_munich.offers[0].price == 0.99


def test_locate_api_and_plz_cookie_persistence(tmp_path: Path, monkeypatch) -> None:
    from app.config import Config
    from app.models import OfferResult
    import app.routes as routes

    class TestConfig(Config):
        TESTING = True
        CACHE_DIR = tmp_path
        AUTO_SCHEDULER_ENABLED = False

    class StubService:
        def get_offers(self, zip_code: str, refresh: bool = False) -> OfferResult:
            return OfferResult(
                offers=[
                    Offer(
                        id=f"edeka-{zip_code}",
                        source_offer_id=1,
                        retailer="EDEKA",
                        retailer_slug="edeka",
                        title="Regionaler Käse",
                        brand="EDEKA",
                        category="dairy",
                        price=1.99,
                        old_price=2.49,
                        unit_price=None,
                        unit=None,
                        discount_percent=20.0,
                        description=f"PLZ {zip_code}",
                        valid_from=None,
                        valid_to=None,
                        image_url=None,
                        source_url="https://www.edeka.de",
                    )
                ],
                fetched_at="2026-10-18T10:00:00+00:00",
                warnings=[],
                from_cache=True,
            )

    monkeypatch.setattr(routes, "offer_service", lambda: StubService())
    app = create_app(TestConfig)
    client = app.test_client()

    # Postfach PLZ 80313 from IP geolocation is normalized to 80331 München
    res = client.get("/api/locate?zip_code=80313&city=München")
    assert res.status_code == 200
    data = res.get_json()
    assert data["zip_code"] == "80331"
    assert data["city"] == "München"
    assert data["detected"] is True

    # Subsequent visit to "/" without ?zip_code uses the saved plz=80331 cookie
    page_res = client.get("/")
    assert page_res.status_code == 200
    html = page_res.get_data(as_text=True)
    assert 'data-current-plz="80331"' in html
    assert "München" in html
