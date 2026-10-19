from __future__ import annotations

import html
import re
from abc import ABC, abstractmethod
from datetime import date, datetime, time
from typing import Any
from zoneinfo import ZoneInfo

import requests
import urllib3
from requests import Session
from requests.exceptions import SSLError
from urllib3.exceptions import InsecureRequestWarning

from app.models import Offer

BERLIN_TZ = ZoneInfo("Europe/Berlin")
DEFAULT_USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X) AppleWebKit/537.36"


def verify_tls_bool(value: str | bool) -> bool:
    if isinstance(value, bool):
        return value
    if str(value).casefold() in {"false", "0", "no"}:
        return False
    return True


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value)
    # 1. Normalize accidental $# or $entity (e.g. $#2028;, $#x2028;, $amp;) to &# or &entity
    text = re.sub(r"\$#", "&#", text)
    text = re.sub(r"\$([a-zA-Z]+;)", r"&\1", text)
    # 2. Fix missing semicolons on common numeric entities (e.g. &#x2028 or &#2028 followed by non-hex)
    text = re.sub(r"&#x([0-9a-fA-F]+)(?![0-9a-fA-F;])", r"&#x\1;", text)
    text = re.sub(r"&#([0-9]+)(?![0-9;])", r"&#\1;", text)
    # 3. Explicitly replace numeric entities for line/paragraph separators (both hex 2028/2029 and decimal 2028/2029 or 8232/8233)
    text = re.sub(r"&#x?202[89];?", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"&#823[23];?", " ", text)
    # 4. Unescape standard HTML entities (with iterative loop for double-escaping like &amp;#x2028;)
    for _ in range(2):
        if "&" in text:
            new_text = html.unescape(text)
            if new_text == text:
                break
            text = new_text
        else:
            break
    # 5. Replace Unicode line/paragraph separators and control characters with standard whitespace
    text = re.sub(r"[\u2028\u2029\r\n\t]+", " ", text)
    # 6. Strip zero-width characters, soft hyphens, and BOM
    text = re.sub(r"[\u200b-\u200d\ufeff\u00ad]", "", text)
    # 7. Collapse all whitespace into a single space and strip
    return re.sub(r"\s+", " ", text).strip()


def join_text(*parts: str | None, sep: str = " · ") -> str:
    return sep.join(part for part in parts if part)


def parse_positive_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def compute_discount_percent(price: float | None, old_price: float | None) -> float | None:
    if old_price and price and old_price > price:
        return round((old_price - price) / old_price * 100, 1)
    return None


def source_offer_id_from_key(
    source_key: str,
    *,
    allow_hex: bool = False,
    hash_fallback: bool = False,
) -> int:
    if allow_hex:
        hex_digits = re.sub(r"[^0-9a-fA-F]", "", source_key)
        if hex_digits:
            return int(hex_digits[:12], 16)
    digits = re.sub(r"\D", "", source_key)
    if digits:
        return int(digits[:12])
    if hash_fallback:
        return abs(hash(source_key)) % 1_000_000_000
    return 0


def date_to_berlin_iso(value: date | str | None, end_of_day: bool) -> str | None:
    if not value:
        return None
    if isinstance(value, date):
        day = value
    else:
        try:
            day = date.fromisoformat(value)
        except ValueError:
            return None
    clock = time.max if end_of_day else time.min
    return datetime.combine(day, clock, BERLIN_TZ).isoformat(timespec="seconds")


def german_date_to_berlin_iso(value: str | None, end_of_day: bool) -> str | None:
    if not value:
        return None
    try:
        day = date.fromisoformat("-".join(reversed(value.split("."))))
    except ValueError:
        return None
    return date_to_berlin_iso(day, end_of_day=end_of_day)


class OfferProvider(ABC):
    retailer_name: str = ""
    retailer_slug: str = ""
    user_agent: str = DEFAULT_USER_AGENT
    default_headers: dict[str, str] = {}
    browser_impersonation: str | None = None

    def __init__(
        self,
        session: Session | None = None,
        timeout: int = 15,
        verify_tls: str | bool = "auto",
    ) -> None:
        self.session = session or requests.Session()
        self.timeout = timeout
        self.verify_tls = verify_tls
        self.browser_session: Any | None = None

    @abstractmethod
    def fetch_offers(self, retailer_slug: str, zip_code: str, limit: int) -> tuple[list[Offer], list[str]]:
        raise NotImplementedError

    def _validate_retailer_slug(self, retailer_slug: str) -> None:
        if self.retailer_slug and retailer_slug != self.retailer_slug:
            raise ValueError(f"{retailer_slug} is not supported by {self.__class__.__name__}.")

    def _request_text(self, url: str, **kwargs: Any) -> tuple[str, bool]:
        response, used_insecure = self._request("GET", url, **kwargs)
        response.raise_for_status()
        return response.text, used_insecure

    def _request_bytes(self, url: str, **kwargs: Any) -> bytes:
        response, _ = self._request("GET", url, **kwargs)
        response.raise_for_status()
        return response.content

    def _request_json(
        self,
        url: str,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[Any, bool]:
        merged_headers = {"Accept": "application/json", **(headers or {})}
        response, used_insecure = self._request("GET", url, params=params, headers=merged_headers)
        response.raise_for_status()
        return response.json(), used_insecure

    def _request(self, method: str, url: str, **kwargs: Any) -> tuple[requests.Response, bool]:
        headers = {
            "User-Agent": self.user_agent,
            **self.default_headers,
            **kwargs.pop("headers", {}),
        }
        request_kwargs = {
            "timeout": self.timeout,
            "headers": headers,
            **kwargs,
        }

        verify = verify_tls_bool(self.verify_tls)
        try:
            response = self.session.request(method, url, verify=verify, **request_kwargs)
            if response.status_code == 403 and self.browser_impersonation:
                return self._browser_request(method, url, verify=verify, **request_kwargs), False
            return response, False
        except SSLError:
            if self.verify_tls != "auto":
                raise
            urllib3.disable_warnings(InsecureRequestWarning)
            response = self.session.request(method, url, verify=False, **request_kwargs)
            if response.status_code == 403 and self.browser_impersonation:
                return self._browser_request(method, url, verify=False, **request_kwargs), True
            return response, True

    def _browser_request(self, method: str, url: str, verify: bool, **request_kwargs: Any) -> Any:
        impersonate = self.browser_impersonation or "chrome136"
        label = self.retailer_name or self.__class__.__name__
        try:
            from curl_cffi import requests as browser_requests
            from curl_cffi.requests.exceptions import CertificateVerifyError
        except ImportError as exc:
            raise RuntimeError(
                f"{label} 공식 페이지가 브라우저 검증으로 차단되었고 curl_cffi fallback을 사용할 수 없습니다."
            ) from exc

        if self.browser_session is None:
            self.browser_session = browser_requests.Session(impersonate=impersonate)
        try:
            return self.browser_session.request(method, url, verify=verify, **request_kwargs)
        except CertificateVerifyError:
            if self.verify_tls == "auto":
                return self.browser_session.request(method, url, verify=False, **request_kwargs)
            raise
