from __future__ import annotations

import re
from datetime import date, datetime, time
from typing import Any
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
REWE_OFFERS_URL = "https://www.rewe.de/angebote/nationale-angebote/"
REWE_FRONTEND_INCLUDES_URL = "https://www.rewe.de/api/frontend-includes"
REWE_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)
REWE_BROWSER_IMPERSONATION = "chrome136"


class ReweProvider(OfferProvider):
    retailer_name = "REWE"
    retailer_slug = "rewe"
    offers_url = REWE_OFFERS_URL
    include_url = REWE_FRONTEND_INCLUDES_URL

    def __init__(
        self,
        session: Session | None = None,
        timeout: int = 15,
        verify_tls: str | bool = "auto",
    ) -> None:
        self.session = session or requests.Session()
        self.browser_session: Any | None = None
        self.timeout = timeout
        self.verify_tls = verify_tls

    def fetch_offers(self, retailer_slug: str, zip_code: str, limit: int) -> tuple[list[Offer], list[str]]:
        if retailer_slug != self.retailer_slug:
            raise ValueError(f"{retailer_slug} is not supported by ReweProvider.")

        warnings: list[str] = []
        html, used_insecure = self._request_text(self.offers_url)
        if used_insecure:
            warnings.append("REWE 공식 페이지 요청은 TLS 인증서 검증 실패로 한 번 비검증 모드로 재시도했습니다.")

        offers: list[Offer] = []
        for week, label in (("current", "Diese Woche"), ("next", "Nächste Woche")):
            valid_from, valid_to = _week_validity(html, label)
            offer_requests = self.offer_requests_from_html(html, limit=limit, week=week)
            if not offer_requests:
                continue

            contents: list[str] = []
            for chunk in _chunks(offer_requests, 60):
                chunk_contents, used_insecure = self._frontend_include_contents(chunk)
                contents.extend(chunk_contents)
                if used_insecure:
                    warnings.append("REWE 상품 타일 요청은 TLS 인증서 검증 실패로 한 번 비검증 모드로 재시도했습니다.")

            offers.extend(
                self.offers_from_include_contents(
                    contents,
                    valid_from=valid_from,
                    valid_to=valid_to,
                    source_url=self.offers_url,
                    limit=limit,
                )
            )
        if not offers:
            raise ValueError("REWE offer tiles were empty.")
        return offers, list(dict.fromkeys(warnings))

    @classmethod
    def offer_requests_from_html(cls, html: str, limit: int, week: str = "current") -> list[dict[str, str]]:
        soup = BeautifulSoup(html, "html.parser")
        week_key = "next" if week == "next" else "current"
        nodes = soup.select(f"#sos-categories-{week_key} .sos-offer[data-offer-nan]")
        if not nodes:
            nodes = soup.select(f'[data-testid="sos-week-tabs-content-week-{week_key}"] [data-offer-nan]')

        offers: list[dict[str, str]] = []
        seen: set[str] = set()
        for node in nodes:
            nan = _clean_text(node.get("data-offer-nan"))
            if not nan or nan in seen:
                continue
            seen.add(nan)
            offer_id = _clean_text(node.get("data-offer-id")) or nan
            offers.append(
                {
                    "id": offer_id,
                    "nan": nan,
                    "ww_ident": _clean_text(node.get("data-offer-wwident")),
                }
            )
            if len(offers) >= limit:
                break
        return offers

    @classmethod
    def offers_from_include_contents(
        cls,
        contents: list[str],
        valid_from: str | None,
        valid_to: str | None,
        source_url: str,
        limit: int,
    ) -> list[Offer]:
        offers: list[Offer] = []
        seen: set[str] = set()
        for content in contents:
            soup = BeautifulSoup(content, "html.parser")
            article = soup.find("article")
            if not isinstance(article, Tag):
                continue
            offer = cls._offer_from_article(article, valid_from, valid_to, source_url)
            if offer is None or offer.id in seen:
                continue
            offers.append(offer)
            seen.add(offer.id)
            if len(offers) >= limit:
                break
        return offers

    @classmethod
    def _offer_from_article(
        cls,
        article: Tag,
        valid_from: str | None,
        valid_to: str | None,
        source_url: str,
    ) -> Offer | None:
        title_link = article.select_one("[data-offer-title]")
        title = _clean_text(title_link.get("data-offer-title") if title_link else "")
        if not title:
            headline = article.select_one(".cor-offer-information__title")
            title = _clean_text(headline.get_text(" ", strip=True) if headline else "")
        if not title:
            return None

        source_key = _clean_text(title_link.get("data-offer-id") if title_link else "") or _clean_text(
            title_link.get("data-offer-nan") if title_link else ""
        )
        if not source_key:
            source_key = title

        description = _description(article)
        price = _price(article, title_link)
        unit_price, unit = _unit_price(description or article.get_text(" ", strip=True))

        return Offer(
            id=f"rewe-{source_key}",
            source_offer_id=_source_offer_id(source_key),
            retailer=cls.retailer_name,
            retailer_slug=cls.retailer_slug,
            title=title,
            brand=None,
            category=categorize_offer(title, description=description),
            price=price,
            old_price=None,
            unit_price=unit_price,
            unit=unit,
            discount_percent=None,
            description=description,
            valid_from=valid_from,
            valid_to=valid_to,
            image_url=_image_url(article),
            source_url=source_url,
        )

    def _frontend_include_contents(self, offer_requests: list[dict[str, str]]) -> tuple[list[str], bool]:
        payload = [
            {
                "id": item["id"],
                "name": "offer-tile-by-nan",
                "namespace": "cor",
                "params": {
                    "nan": item["nan"],
                    "wwIdent": item["ww_ident"],
                    "heroStyles": "false",
                    "showDuration": "auto",
                    "enableDetailDeeplink": "true",
                },
            }
            for item in offer_requests
        ]
        response, used_insecure = self._request(
            "POST",
            self.include_url,
            json=payload,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Origin": "https://www.rewe.de",
                "Referer": self.offers_url,
                "Rd-Client-Href": self.offers_url,
            },
        )
        response.raise_for_status()
        data = response.json()
        return [item.get("content", "") for item in data if item.get("content")], used_insecure

    def _request_text(self, url: str) -> tuple[str, bool]:
        response, used_insecure = self._request("GET", url)
        response.raise_for_status()
        return response.text, used_insecure

    def _request(self, method: str, url: str, **kwargs: Any) -> tuple[requests.Response, bool]:
        headers = {
            "User-Agent": REWE_USER_AGENT,
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
            response = self.session.request(method, url, verify=verify, **request_kwargs)
            if response.status_code == 403:
                return self._browser_request(method, url, verify=verify, **request_kwargs), False
            return response, False
        except SSLError:
            if self.verify_tls != "auto":
                raise
            urllib3.disable_warnings(InsecureRequestWarning)
            response = self.session.request(method, url, verify=False, **request_kwargs)
            if response.status_code == 403:
                return self._browser_request(method, url, verify=False, **request_kwargs), True
            return response, True

    def _browser_request(self, method: str, url: str, verify: bool, **request_kwargs: Any) -> Any:
        try:
            from curl_cffi import requests as browser_requests
        except ImportError as exc:
            raise RuntimeError(
                "REWE 공식 페이지가 브라우저 검증으로 차단되었고 curl_cffi fallback을 사용할 수 없습니다."
            ) from exc

        if self.browser_session is None:
            self.browser_session = browser_requests.Session(impersonate=REWE_BROWSER_IMPERSONATION)
        return self.browser_session.request(method, url, verify=verify, **request_kwargs)


def _week_validity(html: str, label: str) -> tuple[str | None, str | None]:
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(" ", strip=True)
    match = re.search(
        rf"{re.escape(label)}\s+(\d{{1,2}})\.(\d{{1,2}})\.\s+bis\s+(\d{{1,2}})\.(\d{{1,2}})\.",
        text,
    )
    if not match:
        return None, None

    today = datetime.now(BERLIN_TZ).date()
    start_day, start_month, end_day, end_month = (int(part) for part in match.groups())
    ranges: list[tuple[date, date]] = []
    for year in (today.year - 1, today.year, today.year + 1):
        start = date(year, start_month, start_day)
        end_year = year + (1 if (end_month, end_day) < (start_month, start_day) else 0)
        end = date(end_year, end_month, end_day)
        ranges.append((start, end))

    for start, end in ranges:
        if start <= today <= end:
            return _date_iso(start, end_of_day=False), _date_iso(end, end_of_day=True)

    start, end = min(ranges, key=lambda item: abs((item[0] - today).days))
    return _date_iso(start, end_of_day=False), _date_iso(end, end_of_day=True)


def _date_iso(value: date, end_of_day: bool) -> str:
    clock = time.max if end_of_day else time.min
    return datetime.combine(value, clock, BERLIN_TZ).isoformat(timespec="seconds")


def _description(article: Tag) -> str:
    parts = [
        _clean_text(node.get_text(" ", strip=True))
        for node in article.select(".cor-offer-information__additional")
    ]
    text = " ".join(part for part in parts if part)
    return _normalize_punctuation(text)


def _price(article: Tag, title_link: Tag | None) -> float | None:
    price_node = article.select_one(".cor-offer-price__tag-price")
    if price_node:
        price = _euro_price(price_node.get_text(" ", strip=True))
        if price is not None:
            return price

    if title_link and title_link.get("aria-label"):
        match = re.search(r"(?:Aktionspreis|Knallerpreis)\s+([0-9]+(?:[,.][0-9]+)?)\s*€", title_link["aria-label"])
        if match:
            return _decimal(match.group(1))

    footer = article.select_one(".cor-offer-price")
    if footer:
        return _euro_price(footer.get_text(" ", strip=True))
    return None


def _unit_price(text: str) -> tuple[float | None, str | None]:
    match = re.search(
        r"\(?\s*((?:1|100)\s*(?:kg|g|l|L|Liter|ml|Stk\.?))\s*=\s*([0-9]+(?:[,.][0-9]+)?)\s*€",
        text,
    )
    if not match:
        return None, None
    return _decimal(match.group(2)), _clean_text(match.group(1))


def _image_url(article: Tag) -> str | None:
    image = article.select_one('img[data-testid="offer-image"]') or article.find("img")
    if not image:
        return None
    return str(image.get("data-src") or image.get("src") or "") or None


def _source_offer_id(source_key: str) -> int:
    hex_digits = re.sub(r"[^0-9a-fA-F]", "", source_key)
    if hex_digits:
        return int(hex_digits[:12], 16)
    digits = re.sub(r"\D", "", source_key)
    return int(digits[:12] or 0)


def _euro_price(text: str) -> float | None:
    matches = re.findall(r"([0-9]+(?:[,.][0-9]+)?)\s*€?", text)
    if not matches:
        return None
    return _decimal(matches[-1])


def _decimal(value: str) -> float:
    return float(value.replace(",", "."))


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def _normalize_punctuation(value: str) -> str:
    value = re.sub(r"\s+([,.)])", r"\1", value)
    value = re.sub(r"([(])\s+", r"\1", value)
    return _clean_text(value)


def _verify_value(value: str | bool) -> bool:
    if isinstance(value, bool):
        return value
    if value.casefold() in {"false", "0", "no"}:
        return False
    return True


def _chunks(items: list[dict[str, str]], size: int) -> list[list[dict[str, str]]]:
    return [items[index : index + size] for index in range(0, len(items), size)]
