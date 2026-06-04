from __future__ import annotations

import re
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import requests
from requests import Session
from requests.exceptions import SSLError
from urllib3.exceptions import InsecureRequestWarning
import urllib3

from app.models import Offer
from app.providers.base import OfferProvider
from app.services.categories import categorize_offer


class DmProvider(OfferProvider):
    search_url = "https://product-search.services.dmtech.com/de/search"
    source_url = "https://www.dm.de/ausverkauf"

    def __init__(self, session: Session | None = None, timeout: int = 15, verify_tls: str | bool = "auto") -> None:
        self.session = session or requests.Session()
        self.timeout = timeout
        self.verify_tls = verify_tls

    def fetch_offers(self, retailer_slug: str, zip_code: str, limit: int) -> tuple[list[Offer], list[str]]:
        warnings: list[str] = []
        payload, used_insecure_tls = self._request_search(limit=limit)
        if used_insecure_tls:
            warnings.append("TLS 인증서 검증 실패로 dm 요청을 한 번 비검증 모드로 재시도했습니다.")
        products = payload.get("products") or []
        offers = [
            self._offer_from_product(product, retailer_slug)
            for product in products
            if product.get("tileData", {}).get("title", {}).get("tileHeadline") or product.get("title")
        ]
        return offers, warnings

    def _request_search(self, limit: int) -> tuple[dict, bool]:
        headers = {
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X) AppleWebKit/537.36",
            "x-dm-product-search-tags": "channel:web;presentation:grid;search-type:search",
            "x-dm-product-search-token": str(27 * int(datetime.now().timestamp() * 1000)),
        }
        verify = self.verify_tls
        if verify == "auto":
            verify = True

        request_kwargs = {
            "params": {
                "query": "angebot",
                "pageSize": max(1, min(limit, 90)),
                "searchType": "search-static",
                "sort": "editorial_relevance",
                "enablePharmacy": "true",
            },
            "headers": headers,
            "timeout": self.timeout,
        }
        try:
            response = self.session.get(self.search_url, verify=verify, **request_kwargs)
            used_insecure_tls = False
        except SSLError:
            if self.verify_tls != "auto":
                raise
            urllib3.disable_warnings(InsecureRequestWarning)
            response = self.session.get(self.search_url, verify=False, **request_kwargs)
            used_insecure_tls = True
        response.raise_for_status()
        return response.json(), used_insecure_tls

    @classmethod
    def _offer_from_product(cls, product: dict[str, Any], retailer_slug: str) -> Offer:
        tile = product.get("tileData") or {}
        title_data = tile.get("title") or {}
        brand_data = tile.get("brand") or {}
        price_data = (tile.get("price") or {}).get("price") or {}
        current_price = _parse_price((price_data.get("current") or {}).get("value"))
        old_price = _parse_price((price_data.get("previous") or {}).get("value"))
        unit_price, unit = _parse_unit_price((tile.get("price") or {}).get("tileInfos") or [])
        discount_percent = None
        if old_price and current_price and old_price > current_price:
            discount_percent = round((old_price - current_price) / old_price * 100, 1)

        title = (title_data.get("tileHeadline") or product.get("title") or "").strip()
        brand = (brand_data.get("name") or product.get("brandName") or "").strip() or None
        categories = ", ".join((tile.get("trackingData") or {}).get("categories") or [])
        eyecatchers = ", ".join(
            item.get("alt", "")
            for item in tile.get("eyecatchers") or []
            if item.get("alt")
        )
        description = ". ".join(part for part in (categories, eyecatchers) if part)
        image_url = _first_image(tile)
        dan = int(product.get("dan") or tile.get("dan") or 0)
        source_url = f"https://www.dm.de{tile.get('self')}" if tile.get("self") else cls.source_url

        return Offer(
            id=f"dm-{dan or product.get('gtin') or title}",
            source_offer_id=dan,
            retailer="dm",
            retailer_slug=retailer_slug,
            title=title,
            brand=brand,
            category=categorize_offer(title, brand, description),
            price=current_price,
            old_price=old_price if old_price else None,
            unit_price=unit_price,
            unit=unit,
            discount_percent=discount_percent,
            description=description,
            valid_from=datetime.now(ZoneInfo("Europe/Berlin")).date().isoformat(),
            valid_to=None,
            image_url=image_url,
            source_url=source_url,
        )


def _parse_price(value: str | None) -> float | None:
    if not value:
        return None
    normalized = value.replace("\xa0", " ").replace(".", "").replace(",", ".")
    match = re.search(r"(\d+(?:\.\d+)?)", normalized)
    if not match:
        return None
    return float(match.group(1))


def _parse_unit_price(tile_infos: list[str]) -> tuple[float | None, str | None]:
    for info in tile_infos:
        match = re.search(r"\(([\d.,]+)\s*€\s+je\s+1\s+([^)]+)\)", info)
        if match:
            return _parse_price(match.group(1)), match.group(2).strip()
    return None, None


def _first_image(tile: dict[str, Any]) -> str | None:
    images = tile.get("images") or []
    if not images:
        return None
    return images[0].get("tileSrc")
