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
