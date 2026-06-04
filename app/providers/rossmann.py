from __future__ import annotations

import re
import struct
from dataclasses import dataclass

import requests
from requests import Session
from requests.exceptions import SSLError
from urllib3.exceptions import InsecureRequestWarning
import urllib3

from app.models import Offer
from app.providers.base import OfferProvider
from app.providers.marktguru import MarktguruProvider


@dataclass(slots=True)
class RossmannCatalog:
    catalog_id: str
    start_date: str | None
    end_date: str | None
    search_terms: set[str]


class RossmannProvider(OfferProvider):
    catalog_url = "https://www.rossmann.de/de/kataloge/angebote/index.html"
    catalog_root = "https://www.rossmann.de/de/kataloge/angebote/catalogs"
    minimum_match_ratio = 0.6

    def __init__(
        self,
        session: Session | None = None,
        marketguru: MarktguruProvider | None = None,
        timeout: int = 15,
        verify_tls: str | bool = "auto",
    ) -> None:
        self.session = session or requests.Session()
        self.marketguru = marketguru or MarktguruProvider(timeout=timeout, verify_tls=verify_tls)
        self.timeout = timeout
        self.verify_tls = verify_tls

    def fetch_offers(self, retailer_slug: str, zip_code: str, limit: int) -> tuple[list[Offer], list[str]]:
        offers, warnings = self.marketguru.fetch_offers(retailer_slug, zip_code, limit)
        try:
            catalog = self.fetch_catalog()
        except Exception as exc:
            warnings.append(f"ROSSMANN 공식 전단지 색인을 확인하지 못해 Marktguru 상세 데이터만 사용했습니다: {exc}")
            return offers, warnings

        match_count = self.official_match_count(offers, catalog.search_terms)
        if offers and match_count / len(offers) >= self.minimum_match_ratio:
            for offer in offers:
                offer.source_url = self.catalog_url
        else:
            warnings.append(
                "ROSSMANN 공식 전단지 색인과 Marktguru 항목의 일치율이 낮아 Marktguru 출처 링크를 유지했습니다."
            )
        return offers, warnings

    def fetch_catalog(self) -> RossmannCatalog:
        html, _ = self._request_text(self.catalog_url)
        catalog_id = self.extract_catalog_id(html)
        start_date = self.extract_parameter(html, "startDate")
        end_date = self.extract_parameter(html, "endDate")
        terms = self.parse_search_index_terms(self._request_bytes(self.search_index_url(catalog_id)))
        if not terms:
            raise ValueError("공식 전단지 검색 색인이 비어 있습니다.")
        return RossmannCatalog(catalog_id=catalog_id, start_date=start_date, end_date=end_date, search_terms=terms)

    @classmethod
    def search_index_url(cls, catalog_id: str) -> str:
        return f"{cls.catalog_root}/{catalog_id}/search/index.bin"

    @classmethod
    def extract_catalog_id(cls, html: str) -> str:
        match = re.search(r'catalog:\s*"([^"]+)::catalog"', html)
        if not match:
            raise ValueError("ROSSMANN 공식 전단지 catalog ID를 찾지 못했습니다.")
        return match.group(1)

    @staticmethod
    def extract_parameter(html: str, name: str) -> str | None:
        match = re.search(rf"{re.escape(name)}:\s*\"([^\"]*)\"", html)
        return match.group(1) if match else None

    @staticmethod
    def parse_search_index_terms(data: bytes) -> set[str]:
        reader = _BinaryReader(data)
        terms: set[str] = set()
        for _ in range(reader.int32()):
            term = reader.string().strip().casefold()
            if term:
                terms.add(term)
            page_count = reader.uint16()
            for _ in range(page_count):
                reader.uint16()
                rect_count = reader.uint16()
                reader.skip(rect_count * 8)
        return terms

    @classmethod
    def official_match_count(cls, offers: list[Offer], official_terms: set[str]) -> int:
        return sum(1 for offer in offers if cls.offer_matches_terms(offer, official_terms))

    @staticmethod
    def offer_matches_terms(offer: Offer, official_terms: set[str]) -> bool:
        text = " ".join(part for part in [offer.brand or "", offer.title, offer.description] if part)
        for token in _search_tokens(text):
            if token in official_terms:
                return True
        return False

    def _request_text(self, url: str) -> tuple[str, bool]:
        response, used_insecure = self._request("GET", url)
        response.raise_for_status()
        if "Client Challenge" in response.text:
            raise ValueError("공식 ROSSMANN 페이지가 Client Challenge를 반환했습니다.")
        return response.text, used_insecure

    def _request_bytes(self, url: str) -> bytes:
        response, _ = self._request("GET", url)
        response.raise_for_status()
        return response.content

    def _request(self, method: str, url: str, **kwargs) -> tuple[requests.Response, bool]:
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


class _BinaryReader:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.cursor = 0

    def int32(self) -> int:
        self._ensure(4)
        value = struct.unpack_from("<i", self.data, self.cursor)[0]
        self.cursor += 4
        return value

    def uint16(self) -> int:
        self._ensure(2)
        value = self.data[self.cursor] | (self.data[self.cursor + 1] << 8)
        self.cursor += 2
        return value

    def string(self) -> str:
        end = self.data.find(b"\0", self.cursor)
        if end == -1:
            raise ValueError("ROSSMANN 검색 색인 문자열이 종료되지 않았습니다.")
        value = self.data[self.cursor : end].decode("utf-8", errors="replace")
        self.cursor = end + 1
        return value

    def skip(self, length: int) -> None:
        self._ensure(length)
        self.cursor += length

    def _ensure(self, length: int) -> None:
        if self.cursor + length > len(self.data):
            raise ValueError("ROSSMANN 검색 색인 형식이 예상보다 짧습니다.")


def _search_tokens(text: str) -> set[str]:
    raw_tokens = re.findall(r"[\wäöüßÄÖÜ+-]+", text.casefold())
    tokens: set[str] = set()
    for token in raw_tokens:
        if len(token) < 3 or token.isdigit():
            continue
        tokens.add(token)
        for part in re.split(r"[-+]", token):
            if len(part) >= 3 and not part.isdigit():
                tokens.add(part)
    return tokens
