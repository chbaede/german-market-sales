from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any

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
from app.services.location import is_aldi_nord_territory


class AldiNordProvider(OfferProvider):
    retailer_name = "ALDI Nord"
    retailer_slug = "aldi-nord"
    offers_url = "https://www.aldi-nord.de/angebote.html"

    def fetch_offers(self, retailer_slug: str, zip_code: str, limit: int) -> tuple[list[Offer], list[str]]:
        self._validate_retailer_slug(retailer_slug)

        if not is_aldi_nord_territory(zip_code):
            return [], [f"ALDI Nord 매장은 해당 지역(PLZ {zip_code})에 존재하지 않습니다 (ALDI Süd 관할 지역)."]

        html, used_insecure = self._request_text(self.offers_url)
        warnings: list[str] = []
        if used_insecure:
            warnings.append("ALDI Nord 요청은 TLS 인증서 검증 실패로 한 번 비검증 모드로 재시도했습니다.")
        return self.offers_from_html(html, limit=limit, requested_zip_code=zip_code), warnings

    @classmethod
    def offers_from_html(cls, html: str, limit: int, requested_zip_code: str | None = None) -> list[Offer]:
        soup = BeautifulSoup(html, "html.parser")
        script = soup.find("script", id="__NEXT_DATA__")
        if not script:
            raise ValueError("ALDI Nord page data was not found.")
        next_data = json.loads(script.string or script.get_text())
        return cls.offers_from_next_data(next_data, limit=limit, requested_zip_code=requested_zip_code)

    @classmethod
    def offers_from_next_data(cls, next_data: dict, limit: int, requested_zip_code: str | None = None) -> list[Offer]:
        offer_data = cls._offer_data(next_data)
        products = offer_data.get("algoliaDataMap") or {}
        categories = offer_data.get("categories") or []

        offers: list[Offer] = []
        seen: set[str] = set()
        for action in categories:
            fallback_from = date_to_berlin_iso(action.get("startDate"), end_of_day=False)
            fallback_to = date_to_berlin_iso(action.get("endDate"), end_of_day=True)
            for section in action.get("content") or []:
                section_title = clean_text(section.get("title") or section.get("teaserTitle"))
                for product_id in section.get("productIds") or []:
                    object_id = str(product_id)
                    if object_id in seen:
                        continue
                    product = products.get(object_id)
                    if not product or product.get("isRecall") or not product.get("isAvailable", True):
                        continue
                    offer = cls._offer_from_product(
                        product,
                        object_id,
                        section_title,
                        fallback_from,
                        fallback_to,
                        requested_zip_code=requested_zip_code,
                    )
                    if offer is None:
                        continue
                    offers.append(offer)
                    seen.add(object_id)
                    if len(offers) >= limit:
                        return offers
        return offers

    @staticmethod
    def _offer_data(next_data: dict) -> dict:
        api_data = next_data.get("props", {}).get("pageProps", {}).get("apiData")
        if isinstance(api_data, str):
            api_data = json.loads(api_data)
        if not isinstance(api_data, list):
            raise ValueError("ALDI Nord API data has an unexpected format.")

        for entry in api_data:
            if isinstance(entry, list) and len(entry) == 2 and entry[0] == "OFFER_GET":
                payload = entry[1]
                result = payload.get("res") if isinstance(payload, dict) else None
                if isinstance(result, dict):
                    return result
        raise ValueError("ALDI Nord offer payload was not found.")

    @classmethod
    def _offer_from_product(
        cls,
        product: dict,
        object_id: str,
        section_title: str,
        fallback_from: str | None,
        fallback_to: str | None,
        requested_zip_code: str | None = None,
    ) -> Offer | None:
        title = clean_text(product.get("name"))
        if not title:
            return None

        brand = clean_text(product.get("brandName")) or None
        short_description = clean_text(product.get("shortDescription"))
        sales_unit = clean_text(product.get("salesUnit"))
        description = join_text(section_title, sales_unit, short_description)
        price_data = _price_data(product)
        price = parse_positive_float(price_data.get("priceValue"))
        old_price = _strike_price(price_data)
        unit_price, unit = _base_price(price_data)
        discount_percent = _discount_percent(price, old_price, price_data)

        return Offer(
            id=f"aldi-nord-{object_id}",
            source_offer_id=source_offer_id_from_key(object_id),
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
            valid_from=_price_date(price_data, "validFrom") or fallback_from,
            valid_to=_price_date(price_data, "validUntil") or fallback_to,
            image_url=_primary_image(product.get("assets") or []),
            source_url=cls.offers_url,
            requested_zip_code=requested_zip_code,
            scope_type="region",
            store_name="ALDI Nord (Norddeutschland)",
            source_provider="aldi-nord-official",
            source_region_id="aldi-nord-territory",
            region_verified=False,
            verification_method="retailer_territory_unverified",
            verification_reason="PLZ is in ALDI Nord territory, but web catalog lacks offer-level regional leaflet evidence",
        )


def _price_data(product: dict) -> dict:
    current = product.get("currentPrice")
    if isinstance(current, dict) and current.get("priceValue") is not None:
        return current
    promotions = product.get("promotionPrices") or []
    for promotion in promotions:
        if isinstance(promotion, dict) and promotion.get("priceValue") is not None:
            return promotion
    return {}


def _strike_price(price_data: dict) -> float | None:
    strike_price = price_data.get("strikePrice")
    if not isinstance(strike_price, dict):
        return None
    return parse_positive_float(strike_price.get("strikePriceValue"))


def _base_price(price_data: dict) -> tuple[float | None, str | None]:
    base_prices = price_data.get("basePrice") or []
    if not base_prices:
        return None, None
    first = base_prices[0]
    if not isinstance(first, dict):
        return None, None
    return parse_positive_float(first.get("basePriceValue")), clean_text(first.get("basePriceScale")) or None


def _discount_percent(price: float | None, old_price: float | None, price_data: dict) -> float | None:
    label = (price_data.get("priceTagLabels") or {}).get("promoText1")
    if label:
        match = re.search(r"(\d+(?:[,.]\d+)?)", str(label))
        if match:
            return float(match.group(1).replace(",", "."))
    return compute_discount_percent(price, old_price)


def _price_date(price_data: dict, key: str) -> str | None:
    value = price_data.get(key)
    if value is None:
        local_key = "validFromLocalDate" if key == "validFrom" else "validUntilLocalDate"
        return date_to_berlin_iso(price_data.get(local_key), end_of_day=key == "validUntil")
    try:
        timestamp = int(value)
    except (TypeError, ValueError):
        return None
    return datetime.fromtimestamp(timestamp, BERLIN_TZ).isoformat(timespec="seconds")


def _primary_image(assets: list[dict]) -> str | None:
    for asset in assets:
        if asset.get("type") == "primary" and asset.get("url"):
            return asset["url"]
    for asset in assets:
        if asset.get("url"):
            return asset["url"]
    return None
