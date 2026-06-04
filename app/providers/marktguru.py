from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

import requests
from bs4 import BeautifulSoup
from requests import Session
from requests.exceptions import SSLError
from urllib3.exceptions import InsecureRequestWarning
import urllib3

from app.models import Offer
from app.providers.base import OfferProvider
from app.services.categories import categorize_offer


NO_BRAND_NAME = "thisisnobrand123"


@dataclass(slots=True)
class MarktguruConfig:
    api_host: str
    api_key: str
    client_key: str
    used_insecure_tls: bool = False


class MarktguruProvider(OfferProvider):
    base_url = "https://www.marktguru.de"

    def __init__(
        self,
        session: Session | None = None,
        timeout: int = 15,
        verify_tls: str | bool = "auto",
    ) -> None:
        self.session = session or requests.Session()
        self.timeout = timeout
        self.verify_tls = verify_tls
        self._config: MarktguruConfig | None = None

    def fetch_offers(self, retailer_slug: str, zip_code: str, limit: int) -> tuple[list[Offer], list[str]]:
        warnings: list[str] = []
        config = self._get_config(retailer_slug)
        if config.used_insecure_tls:
            warnings.append("TLS 인증서 검증 실패로 Marktguru 요청을 한 번 비검증 모드로 재시도했습니다.")

        endpoint = f"https://{config.api_host}/api/v1/publishers/retailer/{retailer_slug}/offers"
        payload = self._request_json(
            endpoint,
            params={
                "as": "mobile",
                "zipCode": zip_code,
                "limit": limit,
                "offset": 0,
            },
            headers={
                "X-ApiKey": config.api_key,
                "X-ClientKey": config.client_key,
                "Accept": "application/json",
                "Content-type": "application/json",
            },
        )
        offers = [
            self._offer_from_api(item, retailer_slug)
            for item in payload.get("results", [])
            if item.get("product", {}).get("name")
        ]
        return offers, warnings

    def _get_config(self, retailer_slug: str) -> MarktguruConfig:
        if self._config is not None:
            return self._config

        html, used_insecure = self._request_text(f"{self.base_url}/r/{retailer_slug}")
        boot = self.extract_bootstrap_json(html)
        config = boot.get("config", {})
        if not config:
            raise ValueError("Marktguru boot configuration was not found.")

        self._config = MarktguruConfig(
            api_host=config["apiHostAddress"],
            api_key=config["apiKey"],
            client_key=config["clientKey"],
            used_insecure_tls=used_insecure,
        )
        return self._config

    def _request_text(self, url: str) -> tuple[str, bool]:
        response, used_insecure = self._request("GET", url)
        response.raise_for_status()
        return response.text, used_insecure

    def _request_json(self, url: str, params: dict[str, Any], headers: dict[str, str]) -> dict:
        response, _ = self._request("GET", url, params=params, headers=headers)
        response.raise_for_status()
        return response.json()

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

        verify = self.verify_tls
        if verify == "auto":
            verify = True

        try:
            return self.session.request(method, url, verify=verify, **request_kwargs), False
        except SSLError:
            if self.verify_tls != "auto":
                raise
            urllib3.disable_warnings(InsecureRequestWarning)
            return self.session.request(method, url, verify=False, **request_kwargs), True

    @staticmethod
    def extract_bootstrap_json(html: str) -> dict:
        soup = BeautifulSoup(html, "html.parser")
        for script in soup.find_all("script", {"type": "application/json"}):
            text = script.string or script.get_text()
            if not text:
                continue
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                continue
            if "config" in data:
                return data
        match = re.search(r'<script type="application/json">(.*?)</script>', html, re.S)
        if match:
            return json.loads(match.group(1))
        raise ValueError("No Marktguru JSON bootstrap script found.")

    @staticmethod
    def _offer_from_api(item: dict, retailer_slug: str) -> Offer:
        product = item.get("product") or {}
        retailer = item.get("retailer") or {}
        brand_data = item.get("brand") or {}
        unit = item.get("unit") or {}
        source_offer_id = int(item.get("id", 0))
        title = product.get("name", "").strip()
        brand = (brand_data.get("name") or "").strip() or None
        if brand == NO_BRAND_NAME:
            brand = None
        price = _number_or_none(item.get("price"))
        old_price = _number_or_none(item.get("oldPrice"))
        unit_price = _number_or_none(item.get("referencePrice"))
        discount_percent = None
        if old_price and price and old_price > price:
            discount_percent = round((old_price - price) / old_price * 100, 1)
        description = (item.get("description") or "").strip()

        return Offer(
            id=f"marktguru-{source_offer_id}",
            source_offer_id=source_offer_id,
            retailer=retailer.get("name") or retailer_slug,
            retailer_slug=retailer.get("uniqueName") or retailer_slug,
            title=title,
            brand=brand,
            category=categorize_offer(title, brand, description),
            price=price,
            old_price=old_price if old_price else None,
            unit_price=unit_price,
            unit=unit.get("shortName"),
            discount_percent=discount_percent,
            description=description,
            valid_from=item.get("validFrom"),
            valid_to=item.get("validTo"),
            image_url=_image_url(source_offer_id, item),
            source_url=f"https://www.marktguru.de/r/{retailer_slug}",
        )


def _number_or_none(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _image_url(source_offer_id: int, item: dict) -> str | None:
    images = item.get("images") or {}
    if images.get("count", 0) < 1 or not source_offer_id:
        return None
    return f"https://cdn.marktguru.de/api/v1/offers/{source_offer_id}/images/default/0/medium.webp"
