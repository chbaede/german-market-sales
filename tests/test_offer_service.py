import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Barrier

from app.models import Offer
from app.services.offers import OfferService
import app.services.offers as offers_module


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


def test_offer_service_normalizes_cached_categories(tmp_path):
    cache_file = tmp_path / "offers.json"
    cache_file.write_text(
        json.dumps(
            {
                "schema_version": 10,
                "zip_code": "14195",
                "fetched_at": datetime.now(UTC).isoformat(timespec="seconds"),
                "warnings": [],
                "offers": [
                    {
                        "id": "cached-socks",
                        "source_offer_id": 1,
                        "retailer": "dm",
                        "retailer_slug": "dm-drogerie-markt",
                        "title": "Socken aus Grobstrick",
                        "brand": None,
                        "category": "produce",
                        "price": 1.99,
                        "old_price": None,
                        "unit_price": None,
                        "unit": None,
                        "discount_percent": None,
                        "description": "",
                        "valid_from": "2026-06-01T00:00:00+02:00",
                        "valid_to": "2099-06-07T23:59:59+02:00",
                        "image_url": None,
                        "source_url": "https://example.test",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    service = OfferService(
        cache_dir=tmp_path,
        retailers={"dm-drogerie-markt": "dm"},
        cache_ttl_seconds=3600,
        timeout=1,
        verify_tls=False,
        max_offers_per_retailer=60,
    )

    result = service.get_offers("14195")

    assert result.from_cache is True
    assert result.offers[0].category == "nonfood"


def test_offer_service_can_return_stale_cache_for_fast_start(tmp_path):
    cache_file = tmp_path / "offers.json"
    cache_file.write_text(
        json.dumps(
            {
                "schema_version": 10,
                "zip_code": "14195",
                "fetched_at": "2020-01-01T00:00:00+00:00",
                "warnings": ["old warning"],
                "offers": [
                    {
                        "id": "cached-milk",
                        "source_offer_id": 1,
                        "retailer": "EDEKA",
                        "retailer_slug": "edeka",
                        "title": "Frische Milch",
                        "brand": None,
                        "category": "other",
                        "price": 1.19,
                        "old_price": None,
                        "unit_price": None,
                        "unit": None,
                        "discount_percent": None,
                        "description": "",
                        "valid_from": "2026-06-01T00:00:00+02:00",
                        "valid_to": "2099-06-07T23:59:59+02:00",
                        "image_url": None,
                        "source_url": "https://example.test",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    service = OfferService(
        cache_dir=tmp_path,
        retailers={"edeka": "EDEKA"},
        cache_ttl_seconds=1,
        timeout=1,
        verify_tls=False,
        max_offers_per_retailer=60,
    )

    result = service.get_cached_offers("14195")

    assert result is not None
    assert result.from_cache is True
    assert result.fetched_at == "2020-01-01T00:00:00+00:00"
    assert result.warnings == ["old warning"]
    assert result.offers[0].title == "Frische Milch"


def test_offer_service_cache_writes_are_safe_for_concurrent_refreshes(tmp_path, monkeypatch):
    service = OfferService(
        cache_dir=tmp_path,
        retailers={"edeka": "EDEKA"},
        cache_ttl_seconds=3600,
        timeout=1,
        verify_tls=False,
        max_offers_per_retailer=60,
    )
    barrier = Barrier(2)
    original_dump = offers_module.json.dump

    def synchronized_dump(payload, handle, **kwargs):
        original_dump(payload, handle, **kwargs)
        handle.flush()
        barrier.wait(timeout=5)

    monkeypatch.setattr(offers_module.json, "dump", synchronized_dump)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(service._write_cache, "14195", "2026-06-08T00:00:00+00:00", [_offer("Milch")], []),
            executor.submit(service._write_cache, "14195", "2026-06-08T00:00:01+00:00", [_offer("Butter")], []),
        ]
        for future in futures:
            future.result()

    assert service._read_cache("14195") is not None


def _offer(title: str) -> Offer:
    return Offer(
        id=title,
        source_offer_id=1,
        retailer="EDEKA",
        retailer_slug="edeka",
        title=title,
        brand=None,
        category="dairy",
        price=1.0,
        old_price=None,
        unit_price=None,
        unit=None,
        discount_percent=None,
        description="",
        valid_from="2026-06-01T00:00:00+02:00",
        valid_to="2099-06-07T23:59:59+02:00",
        image_url=None,
        source_url="https://example.test",
    )


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
                valid_to="2099-06-14T23:59:59+02:00",
                image_url=None,
                source_url="https://example.test",
            )
        ], []
