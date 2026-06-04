from app.models import Offer
from app.services.offers import OfferService


def test_offer_service_fetches_more_than_display_limit(tmp_path):
    provider = RecordingProvider()
    service = OfferService(
        cache_dir=tmp_path,
        retailers={"lidl": "Lidl"},
        cache_ttl_seconds=3600,
        timeout=1,
        verify_tls=False,
        max_offers_per_retailer=60,
    )
    service.provider = provider
    service.specialized_providers = {}

    result = service.get_offers("14195", refresh=True)

    assert len(result.offers) == 1
    assert provider.calls == [("lidl", "14195", 600)]


class RecordingProvider:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, int]] = []

    def fetch_offers(self, retailer_slug: str, zip_code: str, limit: int) -> tuple[list[Offer], list[str]]:
        self.calls.append((retailer_slug, zip_code, limit))
        return [
            Offer(
                id="test-offer",
                source_offer_id=1,
                retailer="Lidl",
                retailer_slug=retailer_slug,
                title="Future offer",
                brand=None,
                category="pantry",
                price=1.99,
                old_price=None,
                unit_price=None,
                unit=None,
                discount_percent=None,
                description="",
                valid_from="2026-06-10T00:00:00+02:00",
                valid_to="2026-06-14T23:59:59+02:00",
                image_url=None,
                source_url="https://example.test",
            )
        ], []
