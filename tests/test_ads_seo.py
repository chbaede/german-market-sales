from app import create_app
from app.models import OfferResult


def test_ads_txt_route():
    app = create_app()
    client = app.test_client()
    response = client.get("/ads.txt")

    assert response.status_code == 200
    assert "text/plain" in response.content_type
    assert "google.com, pub-6854824605420161, DIRECT, f08c47fec0942fa0" in response.text


def test_robots_txt_route():
    app = create_app()
    client = app.test_client()
    response = client.get("/robots.txt")

    assert response.status_code == 200
    assert "text/plain" in response.content_type
    assert "User-agent: *" in response.text
    assert "Allow: /" in response.text
    assert "Sitemap: http://localhost/sitemap.xml" in response.text


def test_sitemap_xml_route():
    app = create_app()
    client = app.test_client()
    response = client.get("/sitemap.xml")

    assert response.status_code == 200
    assert "application/xml" in response.content_type
    assert "<urlset" in response.text
    assert "http://localhost/" in response.text
    assert "http://localhost/drogerie" in response.text
    assert "<priority>1.0</priority>" in response.text


def test_supermarket_page_seo_and_ads(monkeypatch):
    import app.routes as routes

    class FakeOfferService:
        def get_offers(self, zip_code: str, refresh: bool = False) -> OfferResult:
            return OfferResult(
                offers=[],
                fetched_at="2026-06-04T08:00:00+00:00",
                warnings=[],
            )

    monkeypatch.setattr(routes, "offer_service", lambda: FakeOfferService())
    app = create_app()
    client = app.test_client()
    response = client.get("/")

    assert response.status_code == 200
    html = response.text

    # Google AdSense in head
    assert "https://pagead2.googlesyndication.com/pagead/js/adsbygoogle.js?client=ca-pub-6854824605420161" in html

    # Ad slots
    assert 'data-ad-client="ca-pub-6854824605420161"' in html
    assert 'data-ad-slot="9426228178"' in html
    assert "Anzeige" in html

    # Menu alignment with topbar-inner
    assert '<div class="topbar-inner">' in html

    # SEO tags
    assert '<meta name="description"' in html
    assert '<meta name="robots" content="index, follow">' in html
    assert '<meta name="google-site-verification" content="2VmKKN5VGVIL9P-KiYywyJoeJQiKVCxEhRM4japgnzE">' in html
    assert '<meta property="og:title"' in html
    assert '<link rel="canonical"' in html
    assert 'application/ld+json' in html

    # Public portal features
    assert "data-share-page" in html
    assert "back-to-top" in html
    assert "site-footer" in html
    assert "whatsapp" not in html.lower()
    assert "https://main.yocto.co.kr/" in html
    assert "hub-link" in html


def test_drogerie_page_seo_and_ads(monkeypatch):
    import app.routes as routes

    class FakeOfferService:
        def get_offers(self, zip_code: str, refresh: bool = False) -> OfferResult:
            return OfferResult(
                offers=[],
                fetched_at="2026-06-04T08:00:00+00:00",
                warnings=[],
            )

    monkeypatch.setattr(routes, "drugstore_offer_service", lambda: FakeOfferService())
    app = create_app()
    client = app.test_client()
    response = client.get("/drogerie")

    assert response.status_code == 200
    html = response.text

    # Google AdSense & SEO in Drogerie page
    assert "ca-pub-6854824605420161" in html
    assert 'data-ad-slot="9426228178"' in html
    assert '<div class="topbar-inner">' in html
    assert '<meta name="description"' in html
    assert "data-share-page" in html
    assert "site-footer" in html
    assert "whatsapp" not in html.lower()
    assert "https://main.yocto.co.kr/" in html
    assert "hub-link" in html


def test_english_mode_has_no_korean_ui_strings(monkeypatch):
    import re
    import app.routes as routes
    from app.models import Offer

    sample_offers = [
        Offer(
            id="edeka-text-offer",
            source_offer_id=101,
            retailer="EDEKA",
            retailer_slug="edeka",
            title="Frische Vollmilch 1L",
            brand="Gut & Günstig",
            category="dairy",
            price=0.99,
            old_price=1.29,
            unit_price=0.99,
            unit="1l",
            discount_percent=23.0,
            description="versch. Sorten",
            valid_from="2026-10-20T00:00:00+02:00",
            valid_to="2099-12-31T23:59:59+01:00",
            image_url=None,
            source_url="https://www.edeka.de/angebot",
            scope_type="store",
            region_verified=True,
            source_provider="marktguru",
        ),
        Offer(
            id="rewe-comp-offer",
            source_offer_id=102,
            retailer="REWE",
            retailer_slug="rewe",
            title="Frische Vollmilch 1L",
            brand="Gut & Günstig",
            category="dairy",
            price=1.09,
            old_price=1.29,
            unit_price=1.09,
            unit="1l",
            discount_percent=15.0,
            description="",
            valid_from="2026-10-20T00:00:00+02:00",
            valid_to="2099-12-31T23:59:59+01:00",
            image_url="https://example.com/milk.jpg",
            source_url="https://www.rewe.de/angebot",
            scope_type="nationwide",
            region_verified=True,
            source_provider="rewe",
        ),
    ]

    class FakeOfferService:
        def get_offers(self, zip_code: str, refresh: bool = False) -> OfferResult:
            return OfferResult(
                offers=sample_offers,
                fetched_at="2026-10-25T08:00:00+00:00",
                warnings=[
                    "ALDI Süd 매장은 해당 지역(PLZ 14195)에 존재하지 않습니다 (ALDI Nord 관할 지역).",
                    "EDEKA 공식 데이터가 비어 있어 Marktguru 데이터로 대체했습니다.",
                ],
            )

    monkeypatch.setattr(routes, "offer_service", lambda: FakeOfferService())
    monkeypatch.setattr(routes, "drugstore_offer_service", lambda: FakeOfferService())
    app = create_app()
    client = app.test_client()
    client.set_cookie("lang", "en")

    res_super = client.get("/?retailer=edeka&retailer=rewe")
    assert res_super.status_code == 200
    html_super = res_super.get_data(as_text=True)
    assert re.findall(r"[가-힣]+", html_super) == []
    assert "offer-card--text" in html_super
    assert "ALDI Süd stores do not operate in this area" in html_super
    assert "EDEKA official data was empty; substituted with Marktguru data." in html_super

    res_drug = client.get("/drogerie")
    assert res_drug.status_code == 200
    html_drug = res_drug.get_data(as_text=True)
    assert re.findall(r"[가-힣]+", html_drug) == []


def test_mobile_date_and_ad_suppression_assets():
    app = create_app()
    client = app.test_client()

    css_res = client.get("/static/css/main.css")
    assert css_res.status_code == 200
    css = css_res.get_data(as_text=True)
    assert ".offer-card.offer-card--text" in css
    assert ".offer-card > .card-body" in css
    assert "flex: 0 1 auto;" in css
    assert ".google-auto-placed" in css

    js_res = client.get("/static/js/shopping_list.js")
    assert js_res.status_code == 200
    js = js_res.get_data(as_text=True)
    assert "pruneIntrusiveAutoAds" in js
    assert 'locationBar.dataset.hasSavedPlz === "0"' not in js
