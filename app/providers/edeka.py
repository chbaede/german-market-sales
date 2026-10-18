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

        last_error: Exception | None = None
        best_offers: list[Offer] = []
        best_warnings: list[str] = list(warnings)

        for market in candidate_markets:
            raw_url = (market.get("url") or "").strip()
            if not raw_url:
                continue
            try:
                offers_url, used_insecure_offers = self._offers_url(raw_url)
                market_warnings = list(warnings)
                if used_insecure_offers:
                    market_warnings.append("EDEKA 요청은 TLS 인증서 검증 실패로 한 번 비검증 모드로 재시도했습니다.")

                html, used_insecure_html = self._request_text(offers_url)
                if used_insecure_html:
                    market_warnings.append("EDEKA 요청은 TLS 인증서 검증 실패로 한 번 비검증 모드로 재시도했습니다.")

                market_name = clean_text(market.get("name"))
                offers = self.offers_from_html(
                    html,
                    limit=limit,
                    source_url=offers_url,
                    market_name=market_name,
                )
                if len(offers) > len(best_offers):
                    best_offers = offers
                    best_warnings = market_warnings
                if len(best_offers) >= min(limit, 30):
                    break
            except Exception as exc:
                last_error = exc
                continue

        if best_offers:
            return best_offers, list(dict.fromkeys(best_warnings))

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
    ) -> list[Offer]:
        soup = BeautifulSoup(html, "html.parser")
        valid_from, valid_to = _page_validity(soup)
        offers: list[Offer] = []

        for dialog in soup.find_all("dialog", id=re.compile(r"^dialog-angebot-")):
            offer = cls._offer_from_dialog(dialog, valid_from, valid_to, source_url, market_name)
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
    ) -> Offer | None:
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
