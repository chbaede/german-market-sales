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
KAUFLAND_STOREFINDER_URL = "https://filiale.kaufland.de/.klstorefinder.json"
MAX_STORE_DISTANCE_KM = 60.0


class KauflandProvider(OfferProvider):
    retailer_name = "Kaufland"
    retailer_slug = "kaufland"
    offers_url = KAUFLAND_OFFERS_URL
    storefinder_url = KAUFLAND_STOREFINDER_URL
    default_headers = {"Accept-Language": "de-DE,de;q=0.9,en;q=0.8"}
    _stores_cache: list[dict[str, Any]] | None = None

    def fetch_offers(self, retailer_slug: str, zip_code: str, limit: int) -> tuple[list[Offer], list[str]]:
        self._validate_retailer_slug(retailer_slug)

        headers: dict[str, str] | None = None
        store = self._resolve_store(zip_code)
        store_id = store.get("n") if store else None
        store_name = store.get("cn") if store else None
        store_address = _format_kaufland_address(store) if store else None

        if store_id:
            headers = {"Cookie": f"x-aem-variant={store_id}; kl_store={store_id}"}

        html, used_insecure = (
            self._request_text(self.offers_url, headers=headers)
            if headers
            else self._request_text(self.offers_url)
        )
        warnings: list[str] = []
        if used_insecure:
            warnings.append("Kaufland 공식 페이지 요청은 TLS 인증서 검증 실패로 한 번 비검증 모드로 재시도했습니다.")

        payload = self.extract_offer_template_payload(html)
        offers = self.offers_from_payload(
            payload,
            limit=limit,
            store_id=store_id,
            store_name=store_name,
            store_address=store_address,
            requested_zip=zip_code,
        )
        if not offers:
            raise ValueError("Kaufland official offers were empty.")
        return offers, warnings

    def _resolve_store(self, zip_code: str, stores: list[dict[str, Any]] | None = None) -> dict[str, Any] | None:
        from app.services.location import get_coordinates_for_plz, haversine_distance_km

        cleaned = (zip_code or "").strip()
        if not cleaned or not cleaned.isdigit():
            return None
        if stores is None:
            stores = self._load_stores()
        if not stores:
            return None

        # 1. Exact 5-digit PLZ match
        exact_matches = [s for s in stores if str(s.get("pc") or "").strip() == cleaned and s.get("n")]
        if exact_matches:
            return exact_matches[0]

        user_coords = get_coordinates_for_plz(cleaned)
        if not user_coords:
            # Without verified geographic coordinates, do not guess
            return None

        # 2. True geographic distance (haversine) across all stores within MAX_STORE_DISTANCE_KM
        valid_stores = [s for s in stores if s.get("n")]
        candidates_with_dist = []
        for s in valid_stores:
            dist = _store_distance(s, user_coords)
            if dist <= MAX_STORE_DISTANCE_KM:
                candidates_with_dist.append((dist, s))

        if candidates_with_dist:
            candidates_with_dist.sort(key=lambda x: x[0])
            return candidates_with_dist[0][1]

        return None

    def _resolve_store_id(self, zip_code: str) -> str | None:
        store = self._resolve_store(zip_code)
        return str(store.get("n")).strip() if store and store.get("n") else None

    def _load_stores(self) -> list[dict[str, Any]]:
        if KauflandProvider._stores_cache is not None:
            return KauflandProvider._stores_cache
        try:
            payload, _ = self._request_json(self.storefinder_url)
            if isinstance(payload, list):
                KauflandProvider._stores_cache = payload
                return payload
        except Exception:
            pass
        return []

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
    def offers_from_payload(
        cls,
        payload: dict,
        limit: int,
        today: date | None = None,
        store_id: str | None = None,
        store_name: str | None = None,
        store_address: str | None = None,
        requested_zip: str | None = None,
    ) -> list[Offer]:
        today = today or datetime.now(BERLIN_TZ).date()
        cycles = payload.get("props", {}).get("offerData", {}).get("cycles") or []
        current_offers: list[Offer] = []
        future_offers: list[Offer] = []
        seen: set[str] = set()

        for cycle in cycles:
            for category in cycle.get("categories") or []:
                category_name = clean_text(category.get("displayName") or category.get("name"))
                for item in category.get("offers") or []:
                    offer = cls._offer_from_item(
                        item,
                        category_name,
                        store_id=store_id,
                        store_name=store_name,
                        store_address=store_address,
                        requested_zip=requested_zip,
                    )
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
    def _offer_from_item(
        cls,
        item: dict,
        category_name: str,
        store_id: str | None = None,
        store_name: str | None = None,
        store_address: str | None = None,
        requested_zip: str | None = None,
    ) -> Offer | None:
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
            requested_zip_code=requested_zip,
            scope_type="store" if store_id else "nationwide",
            store_id=store_id,
            store_name=store_name or ("Kaufland (Nationale Angebote)" if not store_id else None),
            store_address=store_address,
            source_provider="kaufland-official",
            region_verified=True if store_id else False,
            verification_method="store_cookie_session" if store_id else "national_catalog_fallback",
            verification_reason=f"Kaufland store session for store {store_id}" if store_id else "No verified Kaufland store within 60km; fallback to national offers",
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


def _format_kaufland_address(store: dict) -> str | None:
    street = clean_text(store.get("sn"))
    pc = clean_text(store.get("pc"))
    city = clean_text(store.get("t"))
    parts = [p for p in (street, f"{pc} {city}".strip()) if p]
    return ", ".join(parts) if parts else None


def _store_distance(store: dict, user_coords: tuple[float, float]) -> float:
    from app.services.location import get_coordinates_for_plz, haversine_distance_km, is_valid_coordinates

    lat = store.get("lat")
    lng = store.get("lng")
    if lat is not None and lng is not None:
        if is_valid_coordinates(lat, lng):
            try:
                return haversine_distance_km(user_coords[0], user_coords[1], float(lat), float(lng))
            except (ValueError, TypeError):
                pass
        return 99999.0

    store_pc = str(store.get("pc") or "").strip()
    if store_pc:
        store_coords = get_coordinates_for_plz(store_pc)
        if store_coords and is_valid_coordinates(store_coords[0], store_coords[1]):
            return haversine_distance_km(user_coords[0], user_coords[1], store_coords[0], store_coords[1])

    return 99999.0

