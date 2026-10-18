from __future__ import annotations

import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag

from app.models import Offer
from app.providers.base import (
    OfferProvider,
    clean_text,
    compute_discount_percent,
    german_date_to_berlin_iso,
    join_text,
    source_offer_id_from_key,
)
from app.services.categories import categorize_offer

EDEKA_BASE_URL = "https://www.edeka.de"
EDEKA_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)


class EdekaProvider(OfferProvider):
    retailer_name = "EDEKA"
    retailer_slug = "edeka"
    market_search_url = f"{EDEKA_BASE_URL}/api/marketsearch/markets"
    user_agent = EDEKA_USER_AGENT
    default_headers = {"Accept-Language": "de-DE,de;q=0.9,en;q=0.8"}
    browser_impersonation = "chrome136"

    def fetch_offers(self, retailer_slug: str, zip_code: str, limit: int) -> tuple[list[Offer], list[str]]:
        self._validate_retailer_slug(retailer_slug)

        warnings: list[str] = []
        candidate_markets, used_insecure = self._candidate_markets(zip_code)
        if used_insecure:
            warnings.append("EDEKA 요청은 TLS 인증서 검증 실패로 한 번 비검증 모드로 재시도했습니다.")

        candidate_markets = _sort_candidate_markets(candidate_markets, zip_code)
        if not candidate_markets:
            raise ValueError(f"No geographically valid EDEKA markets found for PLZ {zip_code}.")
        from app.services.location import normalize_german_plz

        clean_zip = (zip_code or "").strip()
        norm_zip = normalize_german_plz(clean_zip) or clean_zip

        # Prioritize candidate markets whose postal code matches the requested PLZ
        def _is_market_matching_plz(m: dict) -> bool:
            m_addr = _market_address(m)
            pc = _extract_market_plz(m, m_addr)
            return bool(pc and (pc == clean_zip or pc == norm_zip))

        # Stable partition: matching PLZ candidates first, followed by other eligible candidates
        matching_candidates = [m for m in candidate_markets if _is_market_matching_plz(m)]
        other_candidates = [m for m in candidate_markets if not _is_market_matching_plz(m)]
        ordered_candidates = matching_candidates + other_candidates

        unverified_fallback: tuple[list[Offer], list[str]] | None = None
        last_error: Exception | None = None
        evaluated_count = 0
        max_candidates_to_try = 5

        for market in ordered_candidates:
            if evaluated_count >= max_candidates_to_try:
                break
            raw_url = (market.get("url") or "").strip()
            if not raw_url:
                continue
            evaluated_count += 1
            try:
                offers_url, used_insecure_offers = self._offers_url(raw_url)
                market_warnings = list(warnings)
                if used_insecure_offers:
                    market_warnings.append("EDEKA 요청은 TLS 인증서 검증 실패로 한 번 비검증 모드로 재시도했습니다.")

                html, used_insecure_html = self._request_text(offers_url)
                if used_insecure_html:
                    market_warnings.append("EDEKA 요청은 TLS 인증서 검증 실패로 한 번 비검증 모드로 재시도했습니다.")

                market_name = clean_text(market.get("name"))
                store_id = str(market.get("id") or market.get("marketId") or "").strip() or None
                store_address = _market_address(market)

                market_pc = _extract_market_plz(market, store_address)
                is_same_plz = bool(market_pc and (market_pc == clean_zip or market_pc == norm_zip))

                offers = self.offers_from_html(
                    html,
                    limit=limit,
                    source_url=offers_url,
                    market_name=market_name,
                    store_id=store_id,
                    store_address=store_address,
                    requested_zip=zip_code,
                    location_verified=is_same_plz,
                )
                if offers:
                    if is_same_plz:
                        # Preferred: exact/normalized PLZ match verified store
                        return offers, list(dict.fromkeys(market_warnings))
                    elif unverified_fallback is None:
                        # Keep first candidate with offers as unverified fallback, but continue searching for a verified store
                        unverified_fallback = (offers, list(dict.fromkeys(market_warnings)))
            except Exception as exc:
                last_error = exc
                continue

        if unverified_fallback is not None:
            return unverified_fallback

        if last_error:
            raise ValueError(f"No EDEKA offers found for PLZ {zip_code}: {last_error}") from last_error
        raise ValueError(f"No EDEKA offers found for PLZ {zip_code}.")

    @classmethod
    def offers_from_html(
        cls,
        html: str,
        limit: int,
        source_url: str,
        market_name: str | None = None,
        store_id: str | None = None,
        store_address: str | None = None,
        requested_zip: str | None = None,
        location_verified: bool = False,
    ) -> list[Offer]:
        soup = BeautifulSoup(html, "html.parser")
        valid_from, valid_to = _page_validity(soup)
        offers: list[Offer] = []

        for dialog in soup.find_all("dialog", id=re.compile(r"^dialog-angebot-")):
            offer = cls._offer_from_dialog(
                dialog,
                valid_from,
                valid_to,
                source_url,
                market_name,
                store_id=store_id,
                store_address=store_address,
                requested_zip=requested_zip,
                location_verified=location_verified,
            )
            if offer is None:
                continue
            offers.append(offer)
            if len(offers) >= limit:
                break
        return offers

    @classmethod
    def _offer_from_dialog(
        cls,
        dialog: Tag,
        fallback_from: str | None,
        fallback_to: str | None,
        source_url: str,
        market_name: str | None,
        store_id: str | None = None,
        store_address: str | None = None,
        requested_zip: str | None = None,
        location_verified: bool = False,
    ) -> Offer | None:
        from app.services.location import get_coordinates_for_plz, normalize_german_plz

        dialog_id = dialog.get("id", "")
        source_key = dialog_id.removeprefix("dialog-angebot-")
        title = _title(dialog)
        if not title:
            return None

        price, old_price, price_note = _prices(dialog)
        discount_percent = _discount_percent(dialog) or compute_discount_percent(price, old_price)
        if old_price is None and price and discount_percent:
            old_price = round(price / (1 - discount_percent / 100), 2)

        unit_price, unit = _unit_price(dialog)
        description = _description(dialog, price_note, market_name)
        valid_from = _valid_from(dialog) or fallback_from

        clean_store_id = str(store_id).strip() if store_id is not None else ""
        has_store_identity = bool(clean_store_id and (market_name or store_address))

        clean_zip = (requested_zip or "").strip()
        norm_zip = normalize_german_plz(clean_zip) or clean_zip
        user_coords = get_coordinates_for_plz(clean_zip) if len(clean_zip) == 5 and clean_zip.isdigit() else None

        addr_plz: str | None = None
        if store_address:
            m_plz = re.search(r"\b(\d{5})\b", store_address)
            if m_plz:
                addr_plz = m_plz.group(1)

        # Defensible location evidence requires the store's authoritative postal code to match
        # the requested PLZ (or its normalized street PLZ). A nearest store in a different PLZ
        # (even within 60km) does NOT prove promotional applicability for the requested PLZ.
        is_location_substantiated = False
        location_mismatch = False

        if has_store_identity and user_coords:
            if addr_plz:
                if addr_plz == clean_zip or addr_plz == norm_zip:
                    is_location_substantiated = True
                else:
                    location_mismatch = True
            elif location_verified:
                is_location_substantiated = True

        if has_store_identity and is_location_substantiated and not location_mismatch:
            scope_type = "store"
            region_verified = True
            verification_method = "store_direct_api"
            verification_reason = f"Official EDEKA store endpoint for market {clean_store_id} verified at PLZ {clean_zip}"
        elif has_store_identity:
            scope_type = "store"
            region_verified = False
            verification_method = "unverified_store_location"
            if location_mismatch:
                verification_reason = f"EDEKA store {clean_store_id} is located at PLZ {addr_plz}, which does not match requested PLZ {clean_zip}; promotional applicability unverified"
            else:
                verification_reason = f"EDEKA store {clean_store_id} lacks verified geographic location evidence for requested PLZ {clean_zip}"
        else:
            scope_type = "unknown"
            region_verified = False
            verification_method = "unverified_missing_store_id"
            verification_reason = "EDEKA store identity could not be established"

        verification_evidence: dict[str, Any] | None = None
        if region_verified:
            verification_evidence = {
                "scope": "store",
                "store_id": clean_store_id,
                "store_name": market_name,
                "store_address": store_address,
                "matched_plz": clean_zip,
                "verification_method": verification_method,
            }

        return Offer(
            id=f"edeka-{source_key}",
            source_offer_id=source_offer_id_from_key(source_key, allow_hex=True),
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
            valid_from=valid_from,
            valid_to=fallback_to,
            image_url=_image_url(dialog),
            source_url=source_url,
            requested_zip_code=requested_zip,
            scope_type=scope_type,
            store_id=clean_store_id or None,
            store_name=market_name,
            store_address=store_address,
            source_provider="edeka-official",
            region_verified=region_verified,
            verification_method=verification_method,
            verification_reason=verification_reason,
            verification_evidence=verification_evidence,
        )

    def _candidate_markets(self, zip_code: str) -> tuple[list[dict], bool]:
        from app.services.location import normalize_german_plz, resolve_plz_city

        search_queries: list[str] = [zip_code]
        normalized = normalize_german_plz(zip_code)
        if normalized and normalized not in search_queries:
            search_queries.append(normalized)
        city = resolve_plz_city(normalized or zip_code)
        if city and city != "Deutschland" and city not in search_queries:
            search_queries.append(city)
        if len(zip_code) >= 3 and zip_code[:3] not in search_queries:
            search_queries.append(zip_code[:3])

        any_insecure = False
        for query in search_queries:
            try:
                payload, used_insecure = self._request_json(
                    self.market_search_url,
                    params={"searchstring": query},
                )
                any_insecure = any_insecure or used_insecure
            except Exception:
                if query == search_queries[0] and len(search_queries) == 1:
                    raise
                continue

            markets = payload.get("markets") or []
            candidates: list[dict] = []
            for market in markets:
                url = (market.get("url") or "").strip()
                if market.get("isEdekaHomepageMarket") and url and (url.startswith("http://") or url.startswith("https://")):
                    candidates.append(market)
            for market in markets:
                url = (market.get("url") or "").strip()
                if url and (url.startswith("http://") or url.startswith("https://")) and market not in candidates:
                    candidates.append(market)
            if candidates:
                return candidates[:8], any_insecure

        raise ValueError(f"No EDEKA market found for PLZ {zip_code}.")

    def _offers_url(self, market_url: str) -> tuple[str, bool]:
        response, used_insecure = self._request("GET", market_url.strip())
        response.raise_for_status()

        soup = BeautifulSoup(response.text, "html.parser")
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            if re.search(r"/maerkte/\d+/angebote", href):
                return urljoin(response.url, href), used_insecure

        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            if "angebote" in href.lower() and not href.startswith("#"):
                return urljoin(response.url, href), used_insecure

        base = re.sub(r"/[^/]+\.(?:jsp|html?)$", "/", response.url.strip())
        if not base.endswith("/"):
            base = f"{base}/"
        return urljoin(base, "angebote/"), used_insecure


def _page_validity(soup: BeautifulSoup) -> tuple[str | None, str | None]:
    text = soup.get_text(" ", strip=True)
    match = re.search(r"Gültig vom\s+(\d{2}\.\d{2}\.\d{4})\s+bis zum\s+(\d{2}\.\d{2}\.\d{4})", text)
    if not match:
        return None, None
    return (
        german_date_to_berlin_iso(match.group(1), end_of_day=False),
        german_date_to_berlin_iso(match.group(2), end_of_day=True),
    )


def _title(dialog: Tag) -> str:
    headline = dialog.find("h3")
    if not headline:
        return ""
    return clean_text(headline.get_text(" ", strip=True).replace("Angebot:", "", 1))


def _prices(dialog: Tag) -> tuple[float | None, float | None, str]:
    price_notes = [
        clean_text(node.get_text(" ", strip=True))
        for node in dialog.find_all(class_="sr-only")
        if "preis von" in node.get_text(" ", strip=True).casefold()
    ]
    parsed: list[tuple[str, float]] = []
    for note in price_notes:
        match = re.search(r"(App-Preis|Rabattierter Preis|Festpreis) von\s+([0-9]+(?:[.,][0-9]+)?)\s*€", note)
        if match:
            parsed.append((match.group(1), _decimal(match.group(2))))

    if not parsed:
        return None, None, ""

    price = parsed[0][1]
    old_price = None
    if len(parsed) > 1:
        candidates = [value for _, value in parsed[1:] if value > price]
        old_price = candidates[0] if candidates else None
    return price, old_price, " · ".join(price_notes)


def _discount_percent(dialog: Tag) -> float | None:
    text = dialog.get_text(" ", strip=True)
    match = re.search(r"Insgesamt\s+-([0-9]+(?:[.,][0-9]+)?)\s*%\s+Rabatt", text)
    if not match:
        match = re.search(r"-([0-9]+(?:[.,][0-9]+)?)\s*%", text)
    return _decimal(match.group(1)) if match else None


def _unit_price(dialog: Tag) -> tuple[float | None, str | None]:
    text = dialog.get_text(" ", strip=True)
    match = re.search(
        r"Grundpreis(?:\s+(?:mit|ohne)\s+App)?\s*:\s*([0-9A-Za-z]+)\s*=\s*€?\s*([0-9]+(?:[,.][0-9]+)?)",
        text,
    )
    if not match:
        return None, None
    return _decimal(match.group(2)), match.group(1)


def _description(dialog: Tag, price_note: str, market_name: str | None) -> str:
    detail = dialog.find("p", class_=lambda value: value and "line-clmap" in str(value))
    detail_text = clean_text(detail.get_text(" ", strip=True)) if detail else ""
    return join_text(detail_text, price_note, market_name)


def _valid_from(dialog: Tag) -> str | None:
    for node in dialog.find_all("strong"):
        text = clean_text(node.get_text(" ", strip=True))
        match = re.search(r"Gültig ab\s+(\d{2}\.\d{2}\.\d{4})", text)
        if match:
            return german_date_to_berlin_iso(match.group(1), end_of_day=False)
    return None


def _image_url(dialog: Tag) -> str | None:
    image = dialog.find("img")
    if not image:
        return None
    src = image.get("src")
    return str(src) if src else None


def _decimal(value: str) -> float:
    return float(value.replace(",", "."))


MAX_STORE_DISTANCE_KM = 60.0


def _sort_candidate_markets(markets: list[dict], zip_code: str) -> list[dict]:
    from app.services.location import get_coordinates_for_plz, haversine_distance_km, is_valid_coordinates, normalize_german_plz

    user_coords = get_coordinates_for_plz(zip_code)
    clean_zip = (zip_code or "").strip()
    norm_zip = normalize_german_plz(clean_zip) or clean_zip

    def evaluate_market(m: dict) -> tuple[int, float] | None:
        contact = m.get("contact") if isinstance(m.get("contact"), dict) else {}
        address = contact.get("address") if isinstance(contact.get("address"), dict) else (m.get("address") if isinstance(m.get("address"), dict) else {})
        market_pc = _extract_market_plz(m, _market_address(m)) or str(address.get("postalCode") or m.get("postalCode") or "").strip()

        # Must have a store identifier
        store_id = str(m.get("id") or m.get("marketId") or "").strip()
        if not store_id:
            return None

        # 1. Exact or normalized 5-digit PLZ match
        if market_pc and (market_pc == clean_zip or market_pc == norm_zip):
            return (0, 0.0)

        # 2. Raw distance if provided by official API
        raw_dist = m.get("distance")
        if isinstance(raw_dist, (int, float)):
            f_dist = float(raw_dist)
            if 0 <= f_dist <= MAX_STORE_DISTANCE_KM:
                return (1, f_dist)
            return None

        # 3. Coordinate haversine calculation
        coords = m.get("coordinates") or {}
        if isinstance(coords, dict) and user_coords:
            lat = coords.get("lat")
            lon = coords.get("lon")
            if is_valid_coordinates(lat, lon):
                dist = haversine_distance_km(user_coords[0], user_coords[1], lat, lon)
                if dist <= MAX_STORE_DISTANCE_KM:
                    return (1, dist)

        # Fail closed: reject any candidate without defensible geographic match
        return None

    valid_candidates: list[tuple[tuple[int, float], dict]] = []
    for m in markets:
        ev = evaluate_market(m)
        if ev is not None:
            valid_candidates.append((ev, m))

    valid_candidates.sort(key=lambda item: item[0])
    return [item[1] for item in valid_candidates]


def _market_address(market: dict) -> str | None:
    contact = market.get("contact") if isinstance(market.get("contact"), dict) else {}
    address = contact.get("address") if isinstance(contact.get("address"), dict) else (market.get("address") if isinstance(market.get("address"), dict) else {})
    if not isinstance(address, dict) or not address:
        return None
    street = clean_text(address.get("street") or address.get("streetAddress"))
    postal_code = clean_text(address.get("postalCode") or address.get("zipCode"))
    city = clean_text(address.get("city") or address.get("location"))
    parts = [p for p in (street, f"{postal_code} {city}".strip()) if p]
    return ", ".join(parts) if parts else None


def _extract_market_plz(market: dict, store_address: str | None = None) -> str | None:
    contact = market.get("contact") if isinstance(market.get("contact"), dict) else {}
    address = contact.get("address") if isinstance(contact.get("address"), dict) else (market.get("address") if isinstance(market.get("address"), dict) else {})
    pc = str(address.get("postalCode") or market.get("postalCode") or "").strip()
    if len(pc) == 5 and pc.isdigit():
        return pc
    if store_address:
        m = re.search(r"\b(\d{5})\b", store_address)
        if m:
            return m.group(1)
    return None

