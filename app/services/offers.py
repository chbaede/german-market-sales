from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import tempfile

from app.models import Offer, OfferResult
from app.providers.aldi_nord import AldiNordProvider
from app.providers.aldi_sued import AldiSuedProvider
from app.providers.base import BERLIN_TZ
from app.providers.dm import DmProvider
from app.providers.edeka import EdekaProvider
from app.providers.kaufland import KauflandProvider
from app.providers.marktguru import MarktguruProvider
from app.providers.rewe import ReweProvider
from app.providers.rossmann import RossmannProvider
from app.services.categories import categorize_offer
from app.services.formatting import parse_offer_date
from app.services.price_history import PriceHistoryService, normalize_product_key

CACHE_SCHEMA_VERSION = 12
UPSTREAM_FETCH_MULTIPLIER = 10
MIN_UPSTREAM_FETCH_LIMIT = 500
DEFAULT_CACHE_ZIP = "14195"


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
        self.cache_dir = cache_dir
        self.cache_file = cache_dir / cache_name
        self.retailers = retailers
        self.cache_ttl = timedelta(seconds=cache_ttl_seconds)
        self.timeout = timeout
        self.verify_tls = verify_tls
        self.provider = MarktguruProvider(timeout=timeout, verify_tls=verify_tls)
        self.specialized_providers = {
            "aldi-nord": AldiNordProvider(timeout=timeout, verify_tls=verify_tls),
            "aldi-sued": AldiSuedProvider(timeout=timeout, verify_tls=verify_tls),
            "dm-drogerie-markt": DmProvider(timeout=timeout, verify_tls=verify_tls),
            "edeka": EdekaProvider(timeout=timeout, verify_tls=verify_tls),
            "kaufland": KauflandProvider(timeout=timeout, verify_tls=verify_tls),
            "rewe": ReweProvider(timeout=timeout, verify_tls=verify_tls),
            "rossmann": RossmannProvider(marketguru=self.provider, timeout=timeout, verify_tls=verify_tls),
        }
        self.max_offers_per_retailer = max_offers_per_retailer
        self.upstream_fetch_limit = max(
            max_offers_per_retailer * UPSTREAM_FETCH_MULTIPLIER,
            MIN_UPSTREAM_FETCH_LIMIT,
        )
        self._memory_cache: dict[str, tuple[int, str | None, list[str], list[Offer]]] = {}
        self.price_history = PriceHistoryService(cache_dir=cache_dir)
        try:
            self.price_history.seed_initial_history()
        except Exception:
            pass

    def _cache_file_for_zip(self, zip_code: str) -> Path:
        cleaned = (zip_code or "").strip()
        if not cleaned or cleaned == DEFAULT_CACHE_ZIP:
            return self.cache_file
        return self.cache_file.with_name(f"{self.cache_file.stem}_{cleaned}{self.cache_file.suffix}")

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
        workers = max(1, min(len(self.retailers), 8))
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
        try:
            self.price_history.record_offers(offers, zip_code=zip_code, recorded_at=fetched_at)
        except Exception:
            pass
        return OfferResult(offers=offers, fetched_at=fetched_at, warnings=warnings)

    def _read_cache(self, zip_code: str) -> dict | None:
        target_file = self._cache_file_for_zip(zip_code)
        candidates = [target_file]
        if zip_code == DEFAULT_CACHE_ZIP and target_file != self.cache_file:
            candidates.append(self.cache_file)
        for candidate in candidates:
            if not candidate.exists():
                continue
            try:
                with candidate.open("r", encoding="utf-8") as handle:
                    data = json.load(handle)
            except (OSError, json.JSONDecodeError):
                continue
            if data.get("schema_version") != CACHE_SCHEMA_VERSION:
                continue
            if data.get("zip_code") != zip_code:
                continue
            return data
        return None

    def _write_cache(self, zip_code: str, fetched_at: str, offers: list[Offer], warnings: list[str]) -> None:
        payload = {
            "schema_version": CACHE_SCHEMA_VERSION,
            "zip_code": zip_code,
            "fetched_at": fetched_at,
            "warnings": warnings,
            "offers": [offer.to_dict() for offer in offers],
        }
        target_file = self._cache_file_for_zip(zip_code)
        target_file.parent.mkdir(parents=True, exist_ok=True)
        temp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=target_file.parent,
                prefix=f"{target_file.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temp_path = Path(handle.name)
                json.dump(payload, handle, ensure_ascii=False, indent=2)
            temp_path.replace(target_file)
            try:
                mtime_ns = target_file.stat().st_mtime_ns
                self._memory_cache[zip_code] = (mtime_ns, fetched_at, list(warnings), [replace(o) for o in offers])
            except OSError:
                self._memory_cache.pop(zip_code, None)
        except Exception:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)
            raise

    def get_cached_offers(self, zip_code: str) -> OfferResult | None:
        target_file = self._cache_file_for_zip(zip_code)
        if target_file.exists():
            active_file = target_file
        elif zip_code == DEFAULT_CACHE_ZIP and self.cache_file.exists():
            active_file = self.cache_file
        else:
            return None
        try:
            mtime_ns = active_file.stat().st_mtime_ns
        except OSError:
            return None

        mem_entry = self._memory_cache.get(zip_code)
        if mem_entry is not None:
            cached_mtime, fetched_at, warnings, base_offers = mem_entry
            if cached_mtime == mtime_ns:
                offers = self._non_expired_offers([replace(o) for o in base_offers])
                return OfferResult(
                    offers=offers,
                    fetched_at=fetched_at,
                    warnings=list(warnings),
                    from_cache=True,
                )

        cached = self._read_cache(zip_code)
        if not cached:
            return None
        offers = self._cached_offers(cached)
        fetched_at = cached["fetched_at"]
        warnings = cached.get("warnings", [])
        self._memory_cache[zip_code] = (mtime_ns, fetched_at, list(warnings), [replace(o) for o in offers])
        return OfferResult(
            offers=offers,
            fetched_at=fetched_at,
            warnings=warnings,
            from_cache=True,
        )

    def get_all_time_low_map(self, zip_code: str | None = None) -> dict[str, float]:
        try:
            return self.price_history.get_all_time_low_map(zip_code=zip_code)
        except Exception:
            return {}

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
        today = datetime.now(BERLIN_TZ).date()
        current: list[Offer] = []
        for offer in offers:
            if not offer.valid_to:
                current.append(offer)
                continue
            valid_to = parse_offer_date(offer.valid_to)
            if valid_to is None or valid_to >= today:
                current.append(offer)
        return current

    def _normalize_categories(self, offers: list[Offer]) -> list[Offer]:
        for offer in offers:
            if offer.retailer_slug in self.retailers:
                offer.retailer = self.retailers[offer.retailer_slug]
            offer.category = categorize_offer(offer.title, offer.brand, offer.description)
            if not offer.product_key:
                offer.product_key = normalize_product_key(offer.title, offer.brand)
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
            offers, warnings = provider.fetch_offers(
                retailer_slug,
                zip_code=zip_code,
                limit=self.upstream_fetch_limit,
            )
            if retailer_slug in self.specialized_providers and not offers:
                if any("해당 지역" in w for w in warnings):
                    return [], warnings
                retailer_name = self.retailers.get(retailer_slug, retailer_slug)
                fallback_offers, fallback_warnings = self._try_marketguru_fallback(retailer_slug, zip_code)
                if fallback_offers:
                    fallback_offers = self._merge_cached_retailer_offers(zip_code, retailer_slug, fallback_offers)
                    return (
                        fallback_offers,
                        [
                            *warnings,
                            *fallback_warnings,
                            f"{retailer_name} 공식 데이터가 비어 있어 Marktguru 데이터로 대체했습니다.",
                        ],
                    )
            return offers, warnings
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
        return self.provider

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

    def get_regional_diagnostics(self, zip_code: str, retailer_slug: str | None = None) -> dict[str, Any]:
        """
        Diagnose how a given PLZ resolves for each retailer:
        - requested PLZ
        - resolved store ID, name, address
        - provider used (official vs fallback)
        - scope type (store, region, nationwide, unknown)
        - offer count
        - sample titles
        - validity dates
        - regional verification status
        - warnings
        """
        target_retailers = [retailer_slug] if retailer_slug and retailer_slug in self.retailers else list(self.retailers.keys())
        diagnostics: dict[str, Any] = {}
        for slug in target_retailers:
            retailer_name = self.retailers.get(slug, slug)
            try:
                offers, warnings = self._fetch_retailer_offers(slug, zip_code)
                store_id = next((o.store_id for o in offers if o.store_id), None)
                store_name = next((o.store_name for o in offers if o.store_name), None)
                store_address = next((o.store_address for o in offers if o.store_address), None)
                source_provider = next((o.source_provider for o in offers if o.source_provider), "unknown")
                scope_type = next((o.scope_type for o in offers if o.scope_type), "unknown")
                region_verified = any(o.region_verified for o in offers)
                valid_from = next((o.valid_from for o in offers if o.valid_from), None)
                valid_to = next((o.valid_to for o in offers if o.valid_to), None)
                diagnostics[slug] = {
                    "retailer": retailer_name,
                    "retailer_slug": slug,
                    "requested_zip_code": zip_code,
                    "store_id": store_id,
                    "store_name": store_name,
                    "store_address": store_address,
                    "source_provider": source_provider,
                    "scope_type": scope_type,
                    "region_verified": region_verified,
                    "offer_count": len(offers),
                    "valid_from": valid_from,
                    "valid_to": valid_to,
                    "sample_titles": [o.title for o in offers[:3]],
                    "warnings": warnings,
                    "status": "ok" if offers else ("unsupported_territory" if any("해당 지역" in w for w in warnings) else "no_offers"),
                }
            except Exception as exc:
                diagnostics[slug] = {
                    "retailer": retailer_name,
                    "retailer_slug": slug,
                    "requested_zip_code": zip_code,
                    "error": str(exc),
                    "status": "error",
                }
        return {
            "zip_code": zip_code,
            "timestamp": datetime.now(UTC).isoformat(timespec="seconds"),
            "retailers": diagnostics,
        }
