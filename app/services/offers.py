from __future__ import annotations

import json
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from app.models import Offer, OfferResult
from app.providers.aldi_nord import AldiNordProvider
from app.providers.dm import DmProvider
from app.providers.edeka import EdekaProvider
from app.providers.kaufland import KauflandProvider
from app.providers.marktguru import MarktguruProvider
from app.providers.rewe import ReweProvider
from app.providers.rossmann import RossmannProvider
from app.services.categories import categorize_offer


CACHE_SCHEMA_VERSION = 10
UPSTREAM_FETCH_MULTIPLIER = 10
MIN_UPSTREAM_FETCH_LIMIT = 500


class OfferService:
    def __init__(
        self,
        cache_dir: Path,
        retailers: dict[str, str],
        cache_ttl_seconds: int,
        timeout: int,
        verify_tls: str | bool,
        max_offers_per_retailer: int,
        cache_name: str = "offers.json",
    ) -> None:
        self.cache_file = cache_dir / cache_name
        self.retailers = retailers
        self.cache_ttl = timedelta(seconds=cache_ttl_seconds)
        self.timeout = timeout
        self.verify_tls = verify_tls
        self.provider = MarktguruProvider(timeout=timeout, verify_tls=verify_tls)
        self.specialized_providers = {
            "aldi-nord": AldiNordProvider(timeout=timeout, verify_tls=verify_tls),
            "dm-drogerie-markt": DmProvider(timeout=timeout, verify_tls=verify_tls),
            "edeka": EdekaProvider(timeout=timeout, verify_tls=verify_tls),
            "kaufland": KauflandProvider(timeout=timeout, verify_tls=verify_tls),
            "rewe": ReweProvider(timeout=timeout, verify_tls=verify_tls),
            "rossmann": RossmannProvider(timeout=timeout, verify_tls=verify_tls),
        }
        self.max_offers_per_retailer = max_offers_per_retailer
        self.upstream_fetch_limit = max(max_offers_per_retailer * UPSTREAM_FETCH_MULTIPLIER, MIN_UPSTREAM_FETCH_LIMIT)

    def get_offers(self, zip_code: str, refresh: bool = False) -> OfferResult:
        if not refresh:
            cached = self._read_cache(zip_code)
            if cached and not self._is_stale(cached["fetched_at"]):
                return OfferResult(
                    offers=self._cached_offers(cached),
                    fetched_at=cached["fetched_at"],
                    warnings=cached.get("warnings", []),
                    from_cache=True,
                )

        warnings: list[str] = []
        offers: list[Offer] = []
        workers = min(len(self.retailers), 8)
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="offer-provider") as executor:
            futures = {
                executor.submit(self._fetch_retailer_offers, retailer_slug, zip_code): retailer_slug
                for retailer_slug in self.retailers
            }
            for future in as_completed(futures):
                fetched, provider_warnings = future.result()
                offers.extend(fetched)
                warnings.extend(provider_warnings)

        warnings = list(dict.fromkeys(warnings))

        if not offers:
            cached = self._read_cache(zip_code)
            if cached:
                warnings.append("새 데이터를 가져오지 못해 캐시를 표시합니다.")
                return OfferResult(
                    offers=self._cached_offers(cached),
                    fetched_at=cached["fetched_at"],
                    warnings=warnings,
                    from_cache=True,
                )
            raise RuntimeError("No offers could be fetched from any provider.")

        fetched_at = datetime.now(UTC).isoformat(timespec="seconds")
        offers = self._normalize_categories(offers)
        offers = self._non_expired_offers(offers)
        offers = sorted(offers, key=lambda offer: (offer.retailer, offer.title.casefold()))
        self._write_cache(zip_code, fetched_at, offers, warnings)
        return OfferResult(offers=offers, fetched_at=fetched_at, warnings=warnings)

    def _read_cache(self, zip_code: str) -> dict | None:
        if not self.cache_file.exists():
            return None
        try:
            with self.cache_file.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, json.JSONDecodeError):
            return None
        if data.get("schema_version") != CACHE_SCHEMA_VERSION:
            return None
        if data.get("zip_code") != zip_code:
            return None
        return data

    def _write_cache(self, zip_code: str, fetched_at: str, offers: list[Offer], warnings: list[str]) -> None:
        payload = {
            "schema_version": CACHE_SCHEMA_VERSION,
            "zip_code": zip_code,
            "fetched_at": fetched_at,
            "warnings": warnings,
            "offers": [offer.to_dict() for offer in offers],
        }
        self.cache_file.parent.mkdir(parents=True, exist_ok=True)
        temp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=self.cache_file.parent,
                prefix=f"{self.cache_file.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temp_path = Path(handle.name)
                json.dump(payload, handle, ensure_ascii=False, indent=2)
            temp_path.replace(self.cache_file)
        except Exception:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)
            raise

    def get_cached_offers(self, zip_code: str) -> OfferResult | None:
        cached = self._read_cache(zip_code)
        if not cached:
            return None
        return OfferResult(
            offers=self._cached_offers(cached),
            fetched_at=cached["fetched_at"],
            warnings=cached.get("warnings", []),
            from_cache=True,
        )

    def _cached_offers(self, cached: dict) -> list[Offer]:
        offers = [Offer.from_dict(item) for item in cached["offers"]]
        return self._non_expired_offers(self._normalize_categories(offers))

    def _is_stale(self, fetched_at: str | None) -> bool:
        if not fetched_at:
            return True
        try:
            timestamp = datetime.fromisoformat(fetched_at)
        except ValueError:
            return True
        return datetime.now(UTC) - timestamp > self.cache_ttl

    def _non_expired_offers(self, offers: list[Offer]) -> list[Offer]:
        today = datetime.now(ZoneInfo("Europe/Berlin")).date()
        current: list[Offer] = []
        for offer in offers:
            if not offer.valid_to:
                current.append(offer)
                continue
            try:
                valid_to = _local_date(offer.valid_to)
            except ValueError:
                current.append(offer)
                continue
            if valid_to >= today:
                current.append(offer)
        return current

    def _normalize_categories(self, offers: list[Offer]) -> list[Offer]:
        for offer in offers:
            offer.category = categorize_offer(offer.title, offer.brand, offer.description)
        return offers

    def _try_marketguru_fallback(self, retailer_slug: str, zip_code: str) -> tuple[list[Offer], list[str]]:
        try:
            return self._marketguru_provider().fetch_offers(
                retailer_slug,
                zip_code=zip_code,
                limit=self.upstream_fetch_limit,
            )
        except Exception:
            return [], []

    def _fetch_retailer_offers(self, retailer_slug: str, zip_code: str) -> tuple[list[Offer], list[str]]:
        provider = self.specialized_providers.get(retailer_slug) or self._marketguru_provider()
        try:
            return provider.fetch_offers(
                retailer_slug,
                zip_code=zip_code,
                limit=self.upstream_fetch_limit,
            )
        except Exception as exc:
            retailer_name = self.retailers.get(retailer_slug, retailer_slug)
            fallback_offers, fallback_warnings = self._try_marketguru_fallback(retailer_slug, zip_code)
            if retailer_slug in self.specialized_providers and fallback_offers:
                fallback_offers = self._merge_cached_retailer_offers(zip_code, retailer_slug, fallback_offers)
                return (
                    fallback_offers,
                    [
                        *fallback_warnings,
                        f"{retailer_name} 공식 데이터를 가져오지 못해 Marktguru 데이터로 대체했습니다: {exc}",
                    ],
                )
            return [], [f"{retailer_name} 데이터를 가져오지 못했습니다: {exc}"]

    def _marketguru_provider(self) -> MarktguruProvider:
        if type(self.provider) is not MarktguruProvider:
            return self.provider
        return MarktguruProvider(timeout=self.timeout, verify_tls=self.verify_tls)

    def _merge_cached_retailer_offers(
        self,
        zip_code: str,
        retailer_slug: str,
        offers: list[Offer],
    ) -> list[Offer]:
        cached = self._read_cache(zip_code)
        if not cached:
            return offers

        merged = list(offers)
        seen = {offer.id for offer in merged}
        cached_offers = [offer for offer in self._cached_offers(cached) if offer.retailer_slug == retailer_slug]
        for offer in cached_offers:
            if offer.id in seen:
                continue
            merged.append(offer)
            seen.add(offer.id)
        return merged


def _local_date(value: str) -> date:
    timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if timestamp.tzinfo is not None:
        timestamp = timestamp.astimezone(ZoneInfo("Europe/Berlin"))
    return timestamp.date()
