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
    assert '<meta property="og:title"' in html
    assert '<link rel="canonical"' in html
    assert 'application/ld+json' in html


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
