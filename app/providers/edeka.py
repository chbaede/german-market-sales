from __future__ import annotations

import re
from datetime import date, datetime, time
from typing import Any
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
import urllib3
from bs4 import BeautifulSoup, Tag
from requests import Session
from requests.exceptions import SSLError
from urllib3.exceptions import InsecureRequestWarning

from app.models import Offer
from app.providers.base import OfferProvider
from app.services.categories import categorize_offer


BERLIN_TZ = ZoneInfo("Europe/Berlin")
EDEKA_BASE_URL = "https://www.edeka.de"


class EdekaProvider(OfferProvider):
    retailer_name = "EDEKA"
    retailer_slug = "edeka"
    market_search_url = f"{EDEKA_BASE_URL}/api/marketsearch/markets"

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
            raise ValueError(f"{retailer_slug} is not supported by EdekaProvider.")

        warnings: list[str] = []
        market_data, used_insecure = self._market_data(zip_code)
        if used_insecure:
            warnings.append("EDEKA 요청은 TLS 인증서 검증 실패로 한 번 비검증 모드로 재시도했습니다.")

        offers_url, used_insecure = self._offers_url(market_data["url"])
        if used_insecure:
            warnings.append("EDEKA 요청은 TLS 인증서 검증 실패로 한 번 비검증 모드로 재시도했습니다.")

        html, used_insecure = self._request_text(offers_url)
        if used_insecure:
            warnings.append("EDEKA 요청은 TLS 인증서 검증 실패로 한 번 비검증 모드로 재시도했습니다.")

        market_name = _clean_text(market_data.get("name"))
        offers = self.offers_from_html(
            html,
            limit=limit,
            source_url=offers_url,
            market_name=market_name,
        )
        return offers, list(dict.fromkeys(warnings))

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
        discount_percent = _discount_percent(dialog)
        if discount_percent is None and old_price and price and old_price > price:
            discount_percent = round((old_price - price) / old_price * 100, 1)
        if old_price is None and price and discount_percent:
            old_price = round(price / (1 - discount_percent / 100), 2)

        unit_price, unit = _unit_price(dialog)
        description = _description(dialog, price_note, market_name)
        valid_from = _valid_from(dialog) or fallback_from

        return Offer(
            id=f"edeka-{source_key}",
            source_offer_id=_source_offer_id(source_key),
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

    def _market_data(self, zip_code: str) -> tuple[dict, bool]:
        payload, used_insecure = self._request_json(
            self.market_search_url,
            params={"searchstring": zip_code},
        )
        markets = payload.get("markets") or []
        for market in markets:
            if market.get("isEdekaHomepageMarket") and market.get("url"):
                return market, used_insecure
        for market in markets:
            if market.get("url"):
                return market, used_insecure
        raise ValueError(f"No EDEKA market found for PLZ {zip_code}.")

    def _offers_url(self, market_url: str) -> tuple[str, bool]:
        response, used_insecure = self._request("GET", market_url)
        response.raise_for_status()
        market_page_url = response.url
        if not market_page_url.endswith("/"):
            market_page_url = f"{market_page_url}/"
        return urljoin(market_page_url, "angebote/"), used_insecure

    def _request_text(self, url: str) -> tuple[str, bool]:
        response, used_insecure = self._request("GET", url)
        response.raise_for_status()
        return response.text, used_insecure

    def _request_json(self, url: str, params: dict[str, Any]) -> tuple[dict, bool]:
        response, used_insecure = self._request("GET", url, params=params, headers={"Accept": "application/json"})
        response.raise_for_status()
        return response.json(), used_insecure

    def _request(self, method: str, url: str, **kwargs: Any) -> tuple[requests.Response, bool]:
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X) AppleWebKit/537.36",
            "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
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


def _page_validity(soup: BeautifulSoup) -> tuple[str | None, str | None]:
    text = soup.get_text(" ", strip=True)
    match = re.search(r"Gültig vom\s+(\d{2}\.\d{2}\.\d{4})\s+bis zum\s+(\d{2}\.\d{2}\.\d{4})", text)
    if not match:
        return None, None
    return _german_date_iso(match.group(1), end_of_day=False), _german_date_iso(match.group(2), end_of_day=True)


def _title(dialog: Tag) -> str:
    headline = dialog.find("h3")
    if not headline:
        return ""
    return _clean_text(headline.get_text(" ", strip=True).replace("Angebot:", "", 1))


def _prices(dialog: Tag) -> tuple[float | None, float | None, str]:
    price_notes = [
        _clean_text(node.get_text(" ", strip=True))
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
    parts: list[str] = []
    detail = dialog.find("p", class_=lambda value: value and "line-clmap" in str(value))
    if detail:
        parts.append(_clean_text(detail.get_text(" ", strip=True)))
    if price_note:
        parts.append(price_note)
    if market_name:
        parts.append(market_name)
    return " · ".join(part for part in parts if part)


def _valid_from(dialog: Tag) -> str | None:
    for node in dialog.find_all("strong"):
        text = _clean_text(node.get_text(" ", strip=True))
        match = re.search(r"Gültig ab\s+(\d{2}\.\d{2}\.\d{4})", text)
        if match:
            return _german_date_iso(match.group(1), end_of_day=False)
    return None


def _german_date_iso(value: str, end_of_day: bool) -> str | None:
    try:
        day = date.fromisoformat("-".join(reversed(value.split("."))))
    except ValueError:
        return None
    clock = time.max if end_of_day else time.min
    return datetime.combine(day, clock, BERLIN_TZ).isoformat(timespec="seconds")


def _image_url(dialog: Tag) -> str | None:
    image = dialog.find("img")
    if not image:
        return None
    src = image.get("src")
    return str(src) if src else None


def _source_offer_id(source_key: str) -> int:
    hex_digits = re.sub(r"[^0-9a-fA-F]", "", source_key)
    if hex_digits:
        return int(hex_digits[:12], 16)
    digits = re.sub(r"\D", "", source_key)
    return int(digits[:12] or 0)


def _decimal(value: str) -> float:
    return float(value.replace(",", "."))


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()
