from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, date, datetime, timedelta
import json
import re
from typing import Any
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from app.models import Offer
from app.providers.base import (
    BERLIN_TZ,
    OfferProvider,
    clean_text,
    compute_discount_percent,
    date_to_berlin_iso,
    join_text,
    parse_positive_float,
    source_offer_id_from_key,
)
from app.services.categories import categorize_offer
from app.services.location import is_aldi_sued_territory

ALDI_SUED_BASE_URL = "https://www.aldi-sued.de"
ALDI_SUED_OFFERS_URL = f"{ALDI_SUED_BASE_URL}/de/angebote.html"


class AldiSuedProvider(OfferProvider):
    retailer_name = "ALDI Süd"
    retailer_slug = "aldi-sued"
    offers_url = ALDI_SUED_OFFERS_URL
    default_headers = {"Accept-Language": "de-DE,de;q=0.9,en;q=0.8"}
    browser_impersonation = "chrome136"

    def fetch_offers(self, retailer_slug: str, zip_code: str, limit: int) -> tuple[list[Offer], list[str]]:
        self._validate_retailer_slug(retailer_slug)

        if not is_aldi_sued_territory(zip_code):
            return [], [f"ALDI Süd 매장은 해당 지역(PLZ {zip_code})에 존재하지 않습니다 (ALDI Nord 관할 지역)."]

        html, used_insecure = self._request_text(self.offers_url)
        warnings: list[str] = []
        if used_insecure:
            warnings.append("ALDI Süd 요청은 TLS 인증서 검증 실패로 한 번 비검증 모드로 재시도했습니다.")

        offers_by_id: dict[str, Offer] = {}
        for offer in self.offers_from_html(html, limit=limit, source_url=self.offers_url, requested_zip_code=zip_code):
            offers_by_id[offer.id] = offer

        date_urls = self._extract_date_urls(html)
        if date_urls and len(offers_by_id) < limit:
            workers = max(1, min(len(date_urls), 4))
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="aldi-sued-day") as executor:
                futures = {executor.submit(self._fetch_day_page, url, limit, zip_code): url for url in date_urls}
                for future in as_completed(futures):
                    try:
                        day_offers, day_insecure = future.result()
                        if day_insecure:
                            warnings.append("ALDI Süd 요청은 TLS 인증서 검증 실패로 한 번 비검증 모드로 재시도했습니다.")
                        for offer in day_offers:
                            if offer.id not in offers_by_id:
                                offers_by_id[offer.id] = offer
                    except Exception:
                        continue

        offers = list(offers_by_id.values())[:limit]
        if not offers:
            raise ValueError("ALDI Süd official offers were empty.")
        return offers, list(dict.fromkeys(warnings))

    def _fetch_day_page(self, url: str, limit: int, requested_zip_code: str | None = None) -> tuple[list[Offer], bool]:
        html, used_insecure = self._request_text(url)
        date_match = re.search(r"/angebote/(\d{4}-\d{2}-\d{2})", url)
        fallback_date = date_match.group(1) if date_match else None
        return (
            self.offers_from_html(
                html,
                limit=limit,
                source_url=url,
                fallback_date=fallback_date,
                requested_zip_code=requested_zip_code,
            ),
            used_insecure,
        )

    @classmethod
    def _extract_date_urls(cls, html: str) -> list[str]:
        matches = re.findall(
            r'href=["\']((?:/de)?/angebote/\d{4}-\d{2}-\d{2}|(?:/de)?/produkte/wochenangebote/[^"\'?#]+)["\']',
            html,
        )
        seen: list[str] = []
        for path in matches:
            full_url = urljoin(ALDI_SUED_BASE_URL, path)
            if full_url not in seen:
                seen.append(full_url)
        return seen[:12]

    @classmethod
    def offers_from_html(
        cls,
        html: str,
        limit: int,
        source_url: str | None = None,
        fallback_date: str | None = None,
        requested_zip_code: str | None = None,
    ) -> list[Offer]:
        soup = BeautifulSoup(html, "html.parser")
        script = soup.find("script", id="__NUXT_DATA__")
        if not script:
            raise ValueError("ALDI Süd Nuxt data was not found.")
        nuxt_data = json.loads(script.string or script.get_text())
        if not isinstance(nuxt_data, list):
            raise ValueError("ALDI Süd Nuxt data has an unexpected format.")
        return cls.offers_from_nuxt_data(
            nuxt_data,
            limit=limit,
            source_url=source_url or cls.offers_url,
            fallback_date=fallback_date,
            requested_zip_code=requested_zip_code,
        )

    @classmethod
    def offers_from_nuxt_data(
        cls,
        nuxt_data: list[Any],
        limit: int,
        source_url: str | None = None,
        fallback_date: str | None = None,
        requested_zip_code: str | None = None,
    ) -> list[Offer]:
        offers: list[Offer] = []
        seen: set[str] = set()

        for index, item in enumerate(nuxt_data):
            if not isinstance(item, dict):
                continue
            if "sku" not in item or "name" not in item or "price" not in item:
                continue
            resolved = _deref_nuxt_index(nuxt_data, index)
            if not isinstance(resolved, dict):
                continue
            offer = cls._offer_from_product(
                resolved,
                source_url=source_url or cls.offers_url,
                fallback_date=fallback_date,
                requested_zip_code=requested_zip_code,
            )
            if offer is None or offer.id in seen:
                continue
            seen.add(offer.id)
            offers.append(offer)
            if len(offers) >= limit:
                break
        return offers

    @classmethod
    def _offer_from_product(
        cls,
        product: dict[str, Any],
        source_url: str,
        fallback_date: str | None = None,
        requested_zip_code: str | None = None,
    ) -> Offer | None:
        sku = clean_text(product.get("sku") or product.get("objectID"))
        title = clean_text(product.get("name"))
        if not sku or not title:
            return None

        brand = clean_text(product.get("brandName")) or None
        price_info = product.get("price") if isinstance(product.get("price"), dict) else {}
        price = (
            _parse_eur_string(price_info.get("amountRelevantDisplay"))
            or _parse_eur_string(price_info.get("currentPriceValue"))
            or _parse_amount_value(price_info.get("amountRelevant"))
            or _parse_amount_value(price_info.get("amount"))
        )
        if price is None:
            return None

        old_price = (
            _parse_eur_string(price_info.get("wasPriceDisplay"))
            or _parse_amount_value(price_info.get("wasPriceAmount"))
        )
        if old_price is not None and old_price <= price:
            old_price = None

        discount_percent = (
            _parse_discount_string(price_info.get("savingsDisplay") or price_info.get("reduction"))
            or compute_discount_percent(price, old_price)
        )
        unit_price, unit = _parse_unit_price(
            price_info.get("perUnitDisplay") or price_info.get("comparisonDisplay")
        )

        valid_from_date = _parse_on_sale_date(product.get("onSaleDate")) or _parse_iso_date(fallback_date)
        valid_from: str | None = None
        valid_to: str | None = None
        if valid_from_date:
            valid_from = date_to_berlin_iso(valid_from_date.isoformat(), end_of_day=False)
            days_until_saturday = (5 - valid_from_date.weekday()) % 7
            valid_to_date = valid_from_date + timedelta(days=days_until_saturday)
            valid_to = date_to_berlin_iso(valid_to_date.isoformat(), end_of_day=True)

        categories = [
            clean_text(cat.get("name"))
            for cat in (product.get("categories") or [])
            if isinstance(cat, dict) and cat.get("name")
        ]
        selling_unit = clean_text(
            price_info.get("sellingSizeDisplay")
            or product.get("sellingUnitDisplay")
            or product.get("sellingSize")
        )
        description = join_text(selling_unit, *categories)

        slug = clean_text(product.get("urlSlugText"))
        item_url = (
            f"{ALDI_SUED_BASE_URL}/de/p.{slug}.{sku}.html"
            if slug and sku
            else source_url
        )

        return Offer(
            id=f"aldi-sued-{sku}",
            source_offer_id=source_offer_id_from_key(sku, hash_fallback=True),
            retailer=cls.retailer_name,
            retailer_slug=cls.retailer_slug,
            title=title,
            brand=brand,
            category=categorize_offer(title, brand, description),
            price=price,
            old_price=old_price,
            unit_price=unit_price,
            unit=unit,
            discount_percent=discount_percent,
            description=description,
            valid_from=valid_from,
            valid_to=valid_to,
            image_url=_primary_image(product.get("assets") or []),
            source_url=item_url,
            requested_zip_code=requested_zip_code,
            scope_type="region",
            store_name="ALDI Süd (Süddeutschland)",
            source_provider="aldi-sued-official",
            source_region_id="aldi-sued-territory",
            region_verified=False,
            verification_method="retailer_territory_unverified",
            verification_reason="PLZ is in ALDI Süd territory, but web catalog lacks offer-level regional leaflet evidence",
        )


def _deref_nuxt_index(arr: list[Any], idx: Any, depth: int = 0) -> Any:
    if depth > 7 or type(idx) is not int or idx < 0 or idx >= len(arr):
        return None
    val = arr[idx]
    if isinstance(val, list) and len(val) == 2 and val[0] in ("Reactive", "ShallowReactive", "Ref", "EmptyRef"):
        return _deref_nuxt_index(arr, val[1], depth + 1)
    if isinstance(val, dict):
        return {k: _deref_nuxt_index(arr, v, depth + 1) for k, v in val.items()}
    if isinstance(val, list):
        return [_deref_nuxt_index(arr, el, depth + 1) for el in val]
    return val


def _parse_amount_value(value: Any) -> float | None:
    if value is None:
        return None
    if type(value) is int and value > 0:
        # ALDI Süd Nuxt stores raw integer amounts in cents (e.g. 399 -> 3.99 EUR)
        return round(value / 100.0, 2)
    if type(value) is float and value > 0:
        return round(value, 2)
    return _parse_eur_string(value)


def _parse_eur_string(value: Any) -> float | None:
    if value is None:
        return None
    text = str(value).replace("UVP", "").replace("€", "").strip()
    text = text.replace(".", "").replace(",", ".") if "," in text else text
    match = re.search(r"\d+(?:\.\d+)?", text)
    if not match:
        return None
    return parse_positive_float(match.group(0))


def _parse_discount_string(value: Any) -> float | None:
    if not value:
        return None
    match = re.search(r"(\d+(?:[.,]\d+)?)\s*%", str(value))
    if not match:
        return None
    return parse_positive_float(match.group(1).replace(",", "."))


def _parse_unit_price(value: Any) -> tuple[float | None, str | None]:
    text = clean_text(value)
    if not text:
        return None, None
    # e.g., "1 kg = € 9,95" or "1 l = 1,49 €"
    match_eq = re.search(r"([0-9]+\s*[A-Za-zÄÖÜäöüß]+|[A-Za-zÄÖÜäöüß]+)\s*=\s*€?\s*([0-9]+(?:[.,][0-9]+)?)", text)
    if match_eq:
        return _parse_eur_string(match_eq.group(2)), clean_text(match_eq.group(1))
    # e.g., "€ 9,95 / kg"
    match_slash = re.search(r"([0-9]+(?:[.,][0-9]+)?)\s*€?\s*/\s*([0-9]*\s*[A-Za-zÄÖÜäöüß]+)", text)
    if match_slash:
        return _parse_eur_string(match_slash.group(1)), clean_text(match_slash.group(2))
    return None, None


def _parse_on_sale_date(value: Any) -> date | None:
    if value is None:
        return None
    if type(value) in (int, float) and value > 1_000_000_000:
        seconds = value / 1000.0 if value > 100_000_000_000 else float(value)
        try:
            return datetime.fromtimestamp(seconds, UTC).astimezone(BERLIN_TZ).date()
        except (OSError, OverflowError, ValueError):
            return None
    return _parse_iso_date(str(value))


def _parse_iso_date(value: str | None) -> date | None:
    if not value:
        return None
    text = value.strip()[:10]
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _primary_image(assets: list[Any]) -> str | None:
    for asset in assets:
        if isinstance(asset, dict) and asset.get("url"):
            return str(asset["url"])
    return None
