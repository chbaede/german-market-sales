from app.providers.rewe import ReweProvider


def test_rewe_extracts_current_week_offer_requests_only():
    html = """
    <html>
      <body>
        <div id="sos-categories-current">
          <div class="sos-offer" data-offer-nan="9490219" data-offer-id="current-a"></div>
          <div class="sos-offer" data-offer-nan="9490219" data-offer-id="current-a-copy"></div>
        </div>
        <div id="sos-categories-next">
          <div class="sos-offer" data-offer-nan="1111111" data-offer-id="next-a"></div>
        </div>
      </body>
    </html>
    """

    requests = ReweProvider.offer_requests_from_html(html, limit=10)

    assert requests == [{"id": "current-a", "nan": "9490219", "ww_ident": ""}]


def test_rewe_extracts_next_week_offer_requests():
    html = """
    <html>
      <body>
        <div id="sos-categories-current">
          <div class="sos-offer" data-offer-nan="9490219" data-offer-id="current-a"></div>
        </div>
        <div id="sos-categories-next">
          <div class="sos-offer" data-offer-nan="1111111" data-offer-id="next-a"></div>
        </div>
      </body>
    </html>
    """

    requests = ReweProvider.offer_requests_from_html(html, limit=10, week="next")

    assert requests == [{"id": "next-a", "nan": "1111111", "ww_ident": ""}]


def test_rewe_frontend_include_maps_offer_tile():
    content = """
    <article class="cor-offer-renderer-tile">
      <div class="cor-offer-renderer-tile__content">
        <img data-testid="offer-image" data-src="https://img.rewe-static.de/REWE22_2026/4047826/image.png" />
        <div class="cor-offer-information">
          <h3 class="cor-offer-information__title">
            <a data-offer-id="43fa23d55cd61323"
               data-offer-nan="4047826"
               data-offer-title="Dallmayr Prodomo"
               aria-label="Dallmayr Prodomo, Aktionspreis 6,79 €">Dallmayr Prodomo</a>
          </h3>
          <span class="cor-offer-information__additional">versch. Sorten, gemahlener Bohnenkaffee</span>
          <span class="cor-offer-information__additional">, je 500-g-Pckg.</span>
          <span class="cor-offer-information__additional">, (1 kg = 13,58 €)</span>
        </div>
      </div>
      <div class="cor-offer-price">
        <div class="cor-offer-price__tag-price">6,79 €</div>
      </div>
    </article>
    """

    offers = ReweProvider.offers_from_include_contents(
        [content],
        valid_from="2026-05-25T00:00:00+02:00",
        valid_to="2026-05-31T23:59:59+02:00",
        source_url="https://www.rewe.de/angebote/nationale-angebote/",
        limit=10,
    )

    assert len(offers) == 1
    offer = offers[0]
    assert offer.id == "rewe-43fa23d55cd61323"
    assert offer.retailer == "REWE"
    assert offer.retailer_slug == "rewe"
    assert offer.title == "Dallmayr Prodomo"
    assert offer.price == 6.79
    assert offer.unit_price == 13.58
    assert offer.unit == "1 kg"
    assert offer.valid_from == "2026-05-25T00:00:00+02:00"
    assert offer.valid_to == "2026-05-31T23:59:59+02:00"
    assert offer.image_url == "https://img.rewe-static.de/REWE22_2026/4047826/image.png"
    assert "500-g-Pckg." in offer.description


def test_rewe_uses_browser_request_when_standard_request_is_forbidden():
    class ForbiddenResponse:
        status_code = 403

    class BrowserResponse:
        status_code = 200

    class FakeSession:
        def request(self, *args, **kwargs):
            return ForbiddenResponse()

    provider = ReweProvider(session=FakeSession())
    called = {}

    def fake_browser_request(method, url, verify, **kwargs):
        called["method"] = method
        called["url"] = url
        called["verify"] = verify
        return BrowserResponse()

    provider._browser_request = fake_browser_request

    response, used_insecure = provider._request("GET", "https://www.rewe.de/angebote/nationale-angebote/")

    assert response.status_code == 200
    assert used_insecure is False
    assert called == {
        "method": "GET",
        "url": "https://www.rewe.de/angebote/nationale-angebote/",
        "verify": True,
    }
