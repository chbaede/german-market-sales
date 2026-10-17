from app.providers.edeka import EdekaProvider


def test_edeka_offer_page_maps_dialogs():
    html = """
    <html>
      <body>
        <section>
          <p>Gültig vom 25.05.2026 bis zum 30.05.2026.</p>
          <dialog id="dialog-angebot-0143283e-0d4c-4732-b288-5018982d085d">
            <h3><span>Angebot:</span> Kerrygold Original Irische Butter oder Extra</h3>
            <strong>Gültig ab 24.05.2026</strong>
            <strong>-60 %</strong>
            <div class="sr-only">Rabattierter Preis von 1.39 € (Insgesamt -60 % Rabatt)</div>
            <p class="line-clmap-2">versch. Sorten, 250g</p>
            <p>Grundpreis: 1kg = € 5,56</p>
            <img src="https://www.edeka.de/static/media/butter.png" />
          </dialog>
        </section>
      </body>
    </html>
    """

    offers = EdekaProvider.offers_from_html(
        html,
        limit=10,
        source_url="https://www.edeka.de/maerkte/801006/angebote/",
        market_name="EDEKA Clayallee",
    )

    assert len(offers) == 1
    offer = offers[0]
    assert offer.id == "edeka-0143283e-0d4c-4732-b288-5018982d085d"
    assert offer.retailer == "EDEKA"
    assert offer.title == "Kerrygold Original Irische Butter oder Extra"
    assert offer.price == 1.39
    assert offer.discount_percent == 60.0
    assert offer.unit_price == 5.56
    assert offer.unit == "1kg"
    assert offer.valid_from == "2026-05-24T00:00:00+02:00"
    assert offer.valid_to == "2026-05-30T23:59:59+02:00"
    assert offer.image_url == "https://www.edeka.de/static/media/butter.png"
    assert "EDEKA Clayallee" in offer.description


def test_edeka_app_price_uses_lower_price_and_regular_price_as_old_price():
    html = """
    <html>
      <body>
        <p>Gültig vom 25.05.2026 bis zum 30.05.2026.</p>
        <dialog id="dialog-angebot-app">
          <h3><span>Angebot:</span> Ehrmann Almighurt</h3>
          <strong>Gültig ab 26.05.2026</strong>
          <div class="sr-only">App-Preis von 0.72 €</div>
          <div class="sr-only">Festpreis von 0.89 €</div>
          <p class="line-clmap-2">versch. Sorten, 100/150g Becher</p>
          <p>Grundpreis mit App: 1kg=7,20/4,80</p>
        </dialog>
      </body>
    </html>
    """

    offer = EdekaProvider.offers_from_html(html, limit=10, source_url="https://example.test")[0]

    assert offer.price == 0.72
    assert offer.old_price == 0.89
    assert offer.discount_percent == 19.1
    assert offer.unit_price == 7.2


def test_edeka_fetch_offers_skips_empty_markets_and_uses_candidate_with_offers(monkeypatch):
    provider = EdekaProvider()

    def mock_candidate_markets(zip_code):
        return [
            {"name": "Market 1", "url": "https://www.edeka.de/m1/ "},
            {"name": "Market 2", "url": "https://www.edeka.de/m2/"},
        ], False

    def mock_offers_url(url):
        return f"{url.strip()}/angebote/", False

    def mock_request_text(url):
        if "m1" in url:
            return "<html><body><section><p>No dialogs here</p></section></body></html>", False
        return """
        <html>
          <body>
            <p>Gültig vom 25.05.2026 bis zum 30.05.2026.</p>
            <dialog id="dialog-angebot-1">
              <h3><span>Angebot:</span> Bio Apfel</h3>
              <div class="sr-only">Festpreis von 1.99 €</div>
            </dialog>
          </body>
        </html>
        """, False

    monkeypatch.setattr(provider, "_candidate_markets", mock_candidate_markets)
    monkeypatch.setattr(provider, "_offers_url", mock_offers_url)
    monkeypatch.setattr(provider, "_request_text", mock_request_text)

    offers, warnings = provider.fetch_offers("edeka", "50667", limit=10)
    assert len(offers) == 1
    assert offers[0].title == "Bio Apfel"
    assert "Market 2" in offers[0].description


def test_edeka_request_falls_back_to_browser_on_403(monkeypatch):
    import requests
    provider = EdekaProvider()

    mock_resp = requests.Response()
    mock_resp.status_code = 403
    monkeypatch.setattr(provider.session, "request", lambda *args, **kwargs: mock_resp)

    browser_called = False

    def mock_browser_request(method, url, verify, **kwargs):
        nonlocal browser_called
        browser_called = True
        resp = requests.Response()
        resp.status_code = 200
        return resp

    monkeypatch.setattr(provider, "_browser_request", mock_browser_request)

    response, used_insecure = provider._request("GET", "https://www.edeka.de/test")
    assert browser_called is True
    assert response.status_code == 200


