import struct

from app.models import Offer
from app.providers.rossmann import RossmannProvider


def test_rossmann_extracts_catalog_id_and_dates():
    html = """
    <script>
    document.bk_parameter = {
      catalog: "2026_kw23_beilage::catalog",
      startDate: "30.05.26 01:00",
      endDate: "05.06.26 23:59"
    };
    </script>
    """

    assert RossmannProvider.extract_catalog_id(html) == "2026_kw23_beilage"
    assert RossmannProvider.extract_parameter(html, "startDate") == "30.05.26 01:00"
    assert RossmannProvider.extract_parameter(html, "endDate") == "05.06.26 23:59"


def test_rossmann_parses_local_search_index_terms():
    data = _index(
        [
            ("q10", [(8, [(459, 657, 484, 670)])]),
            ("augen-pads", [(8, [(417, 626, 493, 638)])]),
        ]
    )

    assert RossmannProvider.parse_search_index_terms(data) == {"q10", "augen-pads"}


def test_rossmann_matches_marktguru_offer_against_official_terms():
    offer = _offer(title="Q10 Augen-Pads", brand="Isana")

    assert RossmannProvider.offer_matches_terms(offer, {"q10", "augen-pads"})
    assert not RossmannProvider.offer_matches_terms(offer, {"kaffeepads"})


def _index(entries):
    payload = bytearray()
    payload.extend(struct.pack("<i", len(entries)))
    for term, pages in entries:
        payload.extend(term.encode("utf-8"))
        payload.append(0)
        payload.extend(_u16(len(pages)))
        for page_id, rects in pages:
            payload.extend(_u16(page_id))
            payload.extend(_u16(len(rects)))
            for rect in rects:
                for value in rect:
                    payload.extend(_u16(value))
    return bytes(payload)


def _u16(value: int) -> bytes:
    return bytes((value & 255, value >> 8 & 255))


def _offer(title: str, brand: str | None) -> Offer:
    return Offer(
        id="rossmann-test",
        source_offer_id=1,
        retailer="ROSSMANN",
        retailer_slug="rossmann",
        title=title,
        brand=brand,
        category="personal_care",
        price=1.99,
        old_price=None,
        unit_price=None,
        unit=None,
        discount_percent=None,
        description="",
        valid_from="2026-06-01T00:00:00+02:00",
        valid_to="2026-06-05T23:59:00+02:00",
        image_url=None,
        source_url="https://www.marktguru.de/r/rossmann",
    )
