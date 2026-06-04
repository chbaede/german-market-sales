from __future__ import annotations

import json
import re
from datetime import date, datetime, time
from typing import Any
from zoneinfo import ZoneInfo

import requests
import urllib3
from bs4 import BeautifulSoup
from requests import Session
from requests.exceptions import SSLError
from urllib3.exceptions import InsecureRequestWarning

from app.models import Offer
from app.providers.base import OfferProvider
from app.services.categories import categorize_offer


BERLIN_TZ = ZoneInfo("Europe/Berlin")


class AldiNordProvider(OfferProvider):
    retailer_name = "ALDI Nord"
    retailer_slug = "aldi-nord"
    offers_url = "https://www.aldi-nord.de/angebote.html"

    def __init__(
        self,
        session: Session | None = None,
        timeout: int = 15,
        verify_tls: str | bool = "auto",
    ) -> None:
        self.session = session or requests.Session()
        self.timeout = timeout
        self.verify_tls = verify_tls

    def fetch_offers(self, retailer_slug: str, zip_code: str, limit: int) -> tuple[list[Offer], list[str]]:
        if retailer_slug != self.retailer_slug:
            raise ValueError(f"{retailer_slug} is not supported by AldiNordProvider.")

        html, used_insecure = self._request_text(self.offers_url)
        warnings: list[str] = []
        if used_insecure:
            warnings.append("ALDI Nord 요청은 TLS 인증서 검증 실패로 한 번 비검증 모드로 재시도했습니다.")
        return self.offers_from_html(html, limit=limit), warnings

    @classmethod
    def offers_from_html(cls, html: str, limit: int) -> list[Offer]:
        soup = BeautifulSoup(html, "html.parser")
        script = soup.find("script", id="__NEXT_DATA__")
        if not script:
            raise ValueError("ALDI Nord page data was not found.")
        next_data = json.loads(script.string or script.get_text())
        return cls.offers_from_next_data(next_data, limit=limit)

    @classmethod
    def offers_from_next_data(cls, next_data: dict, limit: int) -> list[Offer]:
        offer_data = cls._offer_data(next_data)
        products = offer_data.get("algoliaDataMap") or {}
        categories = offer_data.get("categories") or []

        offers: list[Offer] = []
        seen: set[str] = set()
        for action in categories:
            fallback_from = _local_date_iso(action.get("startDate"), end_of_day=False)
            fallback_to = _local_date_iso(action.get("endDate"), end_of_day=True)
            for section in action.get("content") or []:
                section_title = _clean_text(section.get("title") or section.get("teaserTitle"))
                for product_id in section.get("productIds") or []:
                    object_id = str(product_id)
                    if object_id in seen:
                        continue
                    product = products.get(object_id)
                    if not product or product.get("isRecall") or not product.get("isAvailable", True):
                        continue
                    offer = cls._offer_from_product(product, object_id, section_title, fallback_from, fallback_to)
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
    ) -> Offer | None:
        title = _clean_text(product.get("name"))
        if not title:
            return None

        brand = _clean_text(product.get("brandName")) or None
        short_description = _clean_text(product.get("shortDescription"))
        sales_unit = _clean_text(product.get("salesUnit"))
        description = _join_text(section_title, sales_unit, short_description)
        price_data = _price_data(product)
        price = _number_or_none(price_data.get("priceValue"))
        old_price = _strike_price(price_data)
        unit_price, unit = _base_price(price_data)
        discount_percent = _discount_percent(price, old_price, price_data)

        return Offer(
            id=f"aldi-nord-{object_id}",
            source_offer_id=_source_offer_id(object_id),
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
        )

    def _request_text(self, url: str) -> tuple[str, bool]:
        response, used_insecure = self._request("GET", url)
        response.raise_for_status()
        return response.text, used_insecure

    def _request(self, method: str, url: str, **kwargs: Any) -> tuple[requests.Response, bool]:
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X) AppleWebKit/537.36",
            **kwargs.pop("headers", {}),
        }
        request_kwargs = {
            "timeout": self.timeout,
            "headers": headers,
            **kwargs,
        }

        verify = _verify_value(self.verify_tls)
        try:
            return self.session.request(method, url, verify=verify, **request_kwargs), False
        except SSLError:
            if self.verify_tls != "auto":
                raise
            urllib3.disable_warnings(InsecureRequestWarning)
            return self.session.request(method, url, verify=False, **request_kwargs), True


def _verify_value(value: str | bool) -> bool:
    if isinstance(value, bool):
        return value
    if value.casefold() in {"false", "0", "no"}:
        return False
    return True


def _price_data(product: dict) -> dict:
    current = product.get("currentPrice")
    if isinstance(current, dict) and current.get("priceValue") is not None:
        return current
    promotions = product.get("promotionPrices") or []
    for promotion in promotions:
        if isinstance(promotion, dict) and promotion.get("priceValue") is not None:
            return promotion
    return {}


def _number_or_none(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _strike_price(price_data: dict) -> float | None:
    strike_price = price_data.get("strikePrice")
    if not isinstance(strike_price, dict):
        return None
    return _number_or_none(strike_price.get("strikePriceValue"))


def _base_price(price_data: dict) -> tuple[float | None, str | None]:
    base_prices = price_data.get("basePrice") or []
    if not base_prices:
        return None, None
    first = base_prices[0]
    if not isinstance(first, dict):
        return None, None
    return _number_or_none(first.get("basePriceValue")), _clean_text(first.get("basePriceScale")) or None


def _discount_percent(price: float | None, old_price: float | None, price_data: dict) -> float | None:
    label = (price_data.get("priceTagLabels") or {}).get("promoText1")
    if label:
        match = re.search(r"(\d+(?:[,.]\d+)?)", str(label))
        if match:
            return float(match.group(1).replace(",", "."))
    if old_price and price and old_price > price:
        return round((old_price - price) / old_price * 100, 1)
    return None


def _price_date(price_data: dict, key: str) -> str | None:
    value = price_data.get(key)
    if value is None:
        local_key = "validFromLocalDate" if key == "validFrom" else "validUntilLocalDate"
        return _local_date_iso(price_data.get(local_key), end_of_day=key == "validUntil")
    try:
        timestamp = int(value)
    except (TypeError, ValueError):
        return None
    return datetime.fromtimestamp(timestamp, BERLIN_TZ).isoformat(timespec="seconds")


def _local_date_iso(value: str | None, end_of_day: bool) -> str | None:
    if not value:
        return None
    try:
        day = date.fromisoformat(value)
    except ValueError:
        return None
    clock = time.max if end_of_day else time.min
    return datetime.combine(day, clock, BERLIN_TZ).isoformat(timespec="seconds")


def _primary_image(assets: list[dict]) -> str | None:
    for asset in assets:
        if asset.get("type") == "primary" and asset.get("url"):
            return asset["url"]
    for asset in assets:
        if asset.get("url"):
            return asset["url"]
    return None


def _source_offer_id(object_id: str) -> int:
    digits = re.sub(r"\D", "", object_id)
    return int(digits[:12] or 0)


def _join_text(*parts: str | None) -> str:
    return " · ".join(part for part in parts if part)


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()
