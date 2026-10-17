from __future__ import annotations

import json
import re
from datetime import date, datetime
from typing import Any

from app.models import Offer
from app.providers.base import (
    BERLIN_TZ,
    OfferProvider,
    clean_text,
    compute_discount_percent,
    date_to_berlin_iso,
    join_text,
    source_offer_id_from_key,
)
from app.services.categories import categorize_offer

KAUFLAND_OFFERS_URL = "https://filiale.kaufland.de/angebote/uebersicht.html?kloffer-week=current"


class KauflandProvider(OfferProvider):
    retailer_name = "Kaufland"
    retailer_slug = "kaufland"
    offers_url = KAUFLAND_OFFERS_URL
    default_headers = {"Accept-Language": "de-DE,de;q=0.9,en;q=0.8"}

    def fetch_offers(self, retailer_slug: str, zip_code: str, limit: int) -> tuple[list[Offer], list[str]]:
        self._validate_retailer_slug(retailer_slug)

        html, used_insecure = self._request_text(self.offers_url)
        warnings: list[str] = []
        if used_insecure:
            warnings.append("Kaufland 공식 페이지 요청은 TLS 인증서 검증 실패로 한 번 비검증 모드로 재시도했습니다.")

        payload = self.extract_offer_template_payload(html)
        offers = self.offers_from_payload(payload, limit=limit)
        if not offers:
            raise ValueError("Kaufland official offers were empty.")
        return offers, warnings

    @classmethod
    def extract_offer_template_payload(cls, html: str) -> dict:
        marker = "window.SSR"
        search_from = 0
        while True:
            marker_index = html.find(marker, search_from)
            if marker_index < 0:
                break
            equals_index = html.find("=", marker_index)
            start = html.find("{", equals_index)
            if start < 0:
                break
            end = _json_object_end(html, start)
            if end is None:
                break
            payload = json.loads(html[start:end])
            if payload.get("component") == "OfferTemplate":
                return payload
            search_from = end
        raise ValueError("Kaufland OfferTemplate payload was not found.")

    @classmethod
    def offers_from_payload(cls, payload: dict, limit: int, today: date | None = None) -> list[Offer]:
        today = today or datetime.now(BERLIN_TZ).date()
        cycles = payload.get("props", {}).get("offerData", {}).get("cycles") or []
        current_offers: list[Offer] = []
        future_offers: list[Offer] = []
        seen: set[str] = set()

        for cycle in cycles:
            for category in cycle.get("categories") or []:
                category_name = clean_text(category.get("displayName") or category.get("name"))
                for item in category.get("offers") or []:
                    offer = cls._offer_from_item(item, category_name)
                    if offer is None or offer.id in seen:
                        continue
                    seen.add(offer.id)

                    valid_from = _date_from_iso(offer.valid_from)
                    valid_to = _date_from_iso(offer.valid_to)
                    if valid_from and valid_to and valid_from <= today <= valid_to:
                        if len(current_offers) < limit:
                            current_offers.append(offer)
                    elif valid_from and valid_from > today:
                        if len(future_offers) < limit:
                            future_offers.append(offer)

                    if len(current_offers) >= limit and len(future_offers) >= limit:
                        return current_offers + future_offers
        return current_offers + future_offers

    @classmethod
    def _offer_from_item(cls, item: dict, category_name: str) -> Offer | None:
        source_key = clean_text(item.get("offerId") or item.get("klNr"))
        title = clean_text(item.get("detailTitle") or item.get("title"))
        if not source_key or not title:
            return None

        price = _number_or_none(item.get("price")) or _number_or_none(item.get("formattedPrice"))
        old_price = _number_or_none(item.get("formattedOldPrice"))
        discount_percent = _number_or_none(item.get("discount")) or compute_discount_percent(price, old_price)
        unit_price, unit = _base_price(item)
        description = _description(item, category_name)

        return Offer(
            id=f"kaufland-{source_key}",
            source_offer_id=source_offer_id_from_key(source_key, hash_fallback=True),
            retailer=cls.retailer_name,
            retailer_slug=cls.retailer_slug,
            title=title,
            brand=None,
            category=categorize_offer(title, description=description),
            price=price,
            old_price=old_price,
            unit_price=unit_price,
            unit=unit,
            discount_percent=discount_percent,
            description=description,
            valid_from=date_to_berlin_iso(item.get("dateFrom"), end_of_day=False),
            valid_to=date_to_berlin_iso(item.get("dateTo"), end_of_day=True),
            image_url=_image_url(item),
            source_url=cls.offers_url,
        )


def _json_object_end(text: str, start: int) -> int | None:
    depth = 0
    in_string = False
    escape = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue

        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return index + 1
    return None


def _number_or_none(value: Any) -> float | None:
    if value is None:
        return None
    text = str(value).replace("UVP", "").replace("€", "").strip()
    text = text.replace(".", "").replace(",", ".") if "," in text else text
    match = re.search(r"\d+(?:\.\d+)?", text)
    if not match:
        return None
    try:
        number = float(match.group(0))
    except ValueError:
        return None
    return number if number > 0 else None


def _base_price(item: dict) -> tuple[float | None, str | None]:
    text = clean_text(item.get("formattedBasePrice") or item.get("basePrice"))
    match = re.search(r"\(?\s*([^=()]+?)\s*=\s*([0-9]+(?:[,.][0-9]+)?)", text)
    if not match:
        return None, None
    return _number_or_none(match.group(2)), clean_text(match.group(1))


def _description(item: dict, category_name: str) -> str:
    return join_text(
        clean_text(item.get("detailDescription")),
        clean_text(item.get("detailAction")).replace(";", " · "),
        clean_text(item.get("unit")),
        category_name,
    )


def _date_from_iso(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).date()
    except ValueError:
        return None


def _image_url(item: dict) -> str | None:
    detail_images = item.get("detailImages") or []
    if detail_images:
        return detail_images[0]
    return clean_text(item.get("listImage")) or None
