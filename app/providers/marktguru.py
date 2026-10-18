from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from bs4 import BeautifulSoup
from requests import Session

from app.models import Offer
from app.providers.base import OfferProvider, compute_discount_percent, parse_positive_float
from app.services.categories import categorize_offer

NO_BRAND_NAME = "thisisnobrand123"


MARKTGURU_REGION_MAPPING: dict[str, list[str]] = {
    "berlin": ["10", "12", "13", "14"],
    "berlin_north": ["10", "13"],
    "berlin_south": ["12", "14"],
    "hamburg": ["20", "21", "22"],
    "koeln": ["50", "51"],
    "cologne": ["50", "51"],
    "muenchen": ["80", "81"],
    "munich": ["80", "81"],
    "munich_south": ["81", "82"],
    "frankfurt": ["60", "63"],
    "stuttgart": ["70", "71"],
    "duesseldorf": ["40"],
    "dortmund": ["44"],
    "essen": ["45"],
    "leipzig": ["04"],
    "dresden": ["01"],
    "nuernberg": ["90", "91"],
    "nuremberg": ["90", "91"],
    "hannover": ["30"],
    "bremen": ["28"],
}


def verify_marktguru_regional_applicability(
    zip_code: str | None,
    flyer_data: dict,
    item: dict,
) -> tuple[bool, str, str, str | None]:
    """
    Verifies whether a Marktguru regional leaflet actually covers the requested PLZ.
    Returns: (region_verified, verification_method, verification_reason, source_region_id)
    """
    from app.services.location import (
        get_coordinates_for_plz,
        haversine_distance_km,
        is_valid_coordinates,
        resolve_plz_city,
    )

    region_data = (
        flyer_data.get("region")
        if isinstance(flyer_data.get("region"), dict)
        else (item.get("region") if isinstance(item.get("region"), dict) else {})
    )
    raw_region_id = (
        flyer_data.get("regionId")
        or flyer_data.get("geographicRegionId")
        or region_data.get("id")
        or region_data.get("regionId")
        or item.get("regionId")
    )
    source_region_id = str(raw_region_id) if raw_region_id is not None else None

    # 1. Authoritative postal code list check
    flyer_pcs = (
        flyer_data.get("postalCodes")
        or flyer_data.get("zipCodes")
        or region_data.get("postalCodes")
        or region_data.get("zipCodes")
        or item.get("postalCodes")
    )
    if isinstance(flyer_pcs, (list, tuple, set)) and flyer_pcs:
        pcs = {str(p).strip() for p in flyer_pcs if p}
        if not zip_code:
            return False, "marktguru_unverified_flyer", "No PLZ provided to verify leaflet postal coverage", source_region_id
        if zip_code in pcs:
            return True, "marktguru_regional_flyer_postal_match", f"Marktguru leaflet covers PLZ {zip_code}", source_region_id
        else:
            return False, "marktguru_unverified_flyer", f"Marktguru leaflet postal codes do not cover requested PLZ {zip_code}", source_region_id

    # 2. Geographic coordinates check
    coords = (
        region_data.get("coordinates")
        if isinstance(region_data.get("coordinates"), dict)
        else (flyer_data.get("coordinates") if isinstance(flyer_data.get("coordinates"), dict) else {})
    )
    lat = coords.get("lat") or region_data.get("latitude") or flyer_data.get("latitude")
    lon = coords.get("lon") or region_data.get("longitude") or flyer_data.get("longitude")
    if is_valid_coordinates(lat, lon) and zip_code:
        u_coords = get_coordinates_for_plz(zip_code)
        if u_coords:
            dist = haversine_distance_km(u_coords[0], u_coords[1], lat, lon)
            if dist <= 60.0:
                return True, "marktguru_regional_flyer_geo_match", f"Marktguru leaflet coordinates within {dist:.1f}km of PLZ {zip_code}", source_region_id
            else:
                return False, "marktguru_unverified_flyer", f"Marktguru leaflet region coordinates {dist:.1f}km away from PLZ {zip_code} (max 60km)", source_region_id

    # 3. Documented regional mapping & geographic relationship
    explicit_prefixes = (
        region_data.get("plzPrefixes")
        or region_data.get("postalCodePrefixes")
        or flyer_data.get("plzPrefixes")
    )
    if isinstance(explicit_prefixes, (list, tuple, set)) and explicit_prefixes:
        norm_prefixes = {str(p).strip() for p in explicit_prefixes if p}
        if zip_code and zip_code[:2] in norm_prefixes:
            return True, "marktguru_regional_flyer_prefix_match", f"Marktguru region prefixes cover PLZ {zip_code}", source_region_id
        else:
            return False, "marktguru_unverified_flyer", f"Marktguru region prefixes do not cover PLZ {zip_code}", source_region_id

    region_name = (
        flyer_data.get("regionName")
        or flyer_data.get("geographicRegionName")
        or region_data.get("name")
        or region_data.get("title")
        or (str(raw_region_id) if isinstance(raw_region_id, str) and not raw_region_id.isdigit() else None)
    )

    if region_name and zip_code:
        city_name = resolve_plz_city(zip_code)
        norm_r = (
            str(region_name)
            .strip()
            .casefold()
            .replace("ü", "ue")
            .replace("ö", "oe")
            .replace("ä", "ae")
            .replace("ß", "ss")
        )
        norm_city = (
            city_name.strip()
            .casefold()
            .replace("ü", "ue")
            .replace("ö", "oe")
            .replace("ä", "ae")
            .replace("ß", "ss")
        )

        if norm_r in MARKTGURU_REGION_MAPPING:
            if zip_code[:2] in MARKTGURU_REGION_MAPPING[norm_r]:
                return True, "marktguru_regional_flyer_region_match", f"Marktguru region '{region_name}' covers PLZ {zip_code}", source_region_id
            else:
                return False, "marktguru_unverified_flyer", f"Marktguru region '{region_name}' does not cover PLZ {zip_code}", source_region_id

        if norm_city and (norm_city in norm_r or norm_r in norm_city):
            return True, "marktguru_regional_flyer_region_match", f"Marktguru region '{region_name}' matches city {city_name}", source_region_id

        # Mismatch check against other known cities
        known_cities = (
            "berlin", "hamburg", "koeln", "cologne", "muenchen", "munich",
            "frankfurt", "stuttgart", "duesseldorf", "dortmund", "essen", "leipzig", "dresden"
        )
        if any(k in norm_r for k in known_cities) and (not norm_city or norm_city not in norm_r):
            return False, "marktguru_unverified_flyer", f"Marktguru region '{region_name}' mismatches requested PLZ {zip_code} ({city_name})", source_region_id

    # 4. If raw_region_id exists but lacks geographic metadata/mapping -> fail unverified
    if raw_region_id is not None:
        return False, "marktguru_unverified_flyer", f"Marktguru regional leaflet '{raw_region_id}' lacks geographic mapping metadata for PLZ {zip_code}", source_region_id

    return False, "marktguru_unverified_flyer", "Marktguru leaflet lacks geographic verification for requested PLZ", None


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
        super().__init__(session=session, timeout=timeout, verify_tls=verify_tls)
        self._config: MarktguruConfig | None = None

    def fetch_offers(self, retailer_slug: str, zip_code: str, limit: int) -> tuple[list[Offer], list[str]]:
        warnings: list[str] = []
        config = self._get_config(retailer_slug)
        if config.used_insecure_tls:
            warnings.append("TLS 인증서 검증 실패로 Marktguru 요청을 한 번 비검증 모드로 재시도했습니다.")

        endpoint = f"https://{config.api_host}/api/v1/publishers/retailer/{retailer_slug}/offers"
        res = self._request_json(
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
        if isinstance(res, tuple) and len(res) == 2:
            payload, used_insecure = res
        else:
            payload = res
            used_insecure = False

        if used_insecure:
            warnings.append("TLS 인증서 검증 실패로 Marktguru 요청을 한 번 비검증 모드로 재시도했습니다.")
        offers = [
            self._offer_from_api(item, retailer_slug, zip_code)
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

    def _request_json(self, url: str, params: dict[str, Any], headers: dict[str, str]) -> dict:
        payload, _ = super()._request_json(url, params=params, headers=headers)
        return payload

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
    def _offer_from_api(item: dict, retailer_slug: str, zip_code: str | None = None) -> Offer:
        product = item.get("product") or {}
        retailer = item.get("retailer") or {}
        brand_data = item.get("brand") or {}
        unit = item.get("unit") or {}
        source_offer_id = int(item.get("id", 0))
        title = product.get("name", "").strip()
        brand = (brand_data.get("name") or "").strip() or None
        if brand == NO_BRAND_NAME:
            brand = None
        price = parse_positive_float(item.get("price"))
        old_price = parse_positive_float(item.get("oldPrice"))
        unit_price = parse_positive_float(item.get("referencePrice"))
        discount_percent = compute_discount_percent(price, old_price)
        description = (item.get("description") or "").strip()
        from app.providers.base import clean_text
        from app.services.location import get_coordinates_for_plz, haversine_distance_km, is_valid_coordinates

        branch_data = item.get("branch") if isinstance(item.get("branch"), dict) else {}
        branch_id = item.get("branchId") or branch_data.get("id")
        store_name = clean_text(branch_data.get("name") or branch_data.get("branchName")) or None
        store_address = clean_text(branch_data.get("address") or branch_data.get("street")) or None

        flyer_data = item.get("flyer") if isinstance(item.get("flyer"), dict) else {}
        flyer_id = item.get("flyerId") or flyer_data.get("id")

        scope_type = "unknown"
        store_id: str | None = None
        source_region_id: str | None = None
        region_verified = False
        verification_method = "unknown_scope"
        verification_reason = "No geographic scope could be established"

        # Check if item or flyer explicitly defines national scope
        is_national = (
            flyer_data.get("isNational") is True
            or flyer_data.get("national") is True
            or item.get("isNational") is True
            or item.get("national") is True
            or flyer_data.get("scope") == "national"
            or item.get("scope") == "national"
        )

        if branch_id:
            store_id = str(branch_id)
            scope_type = "store"

            # Verify branch against requested zip_code
            branch_pc = str(branch_data.get("postalCode") or branch_data.get("zipCode") or "").strip()
            branch_dist = branch_data.get("distance")
            coords = branch_data.get("coordinates") if isinstance(branch_data.get("coordinates"), dict) else {}
            lat = coords.get("lat") or branch_data.get("latitude")
            lon = coords.get("lon") or branch_data.get("longitude")

            matched_geo = False
            if zip_code:
                if branch_pc and branch_pc == zip_code:
                    matched_geo = True
                elif isinstance(branch_dist, (int, float)) and 0 <= float(branch_dist) <= 60.0:
                    matched_geo = True
                elif is_valid_coordinates(lat, lon):
                    user_coords = get_coordinates_for_plz(zip_code)
                    if user_coords and haversine_distance_km(user_coords[0], user_coords[1], lat, lon) <= 60.0:
                        matched_geo = True

            if matched_geo:
                region_verified = True
                verification_method = "marktguru_store_branch_verified"
                verification_reason = f"Marktguru branch {branch_id} geographically verified for PLZ {zip_code}"
            else:
                region_verified = False
                verification_method = "marktguru_unverified_branch"
                verification_reason = f"Marktguru branch {branch_id} lacks geographic verification for PLZ {zip_code}"

        elif is_national:
            scope_type = "nationwide"
            region_verified = True
            verification_method = "marktguru_national_flyer"
            verification_reason = "Marktguru leaflet explicitly marked as national"

        elif flyer_id or flyer_data:
            scope_type = "region"
            region_verified, verification_method, verification_reason, source_region_id = verify_marktguru_regional_applicability(
                zip_code, flyer_data, item
            )

        elif zip_code:
            scope_type = "unknown"
            region_verified = False
            verification_method = "unknown_scope"
            verification_reason = "Marktguru response lacks branch and leaflet geographic metadata"

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
            requested_zip_code=zip_code,
            scope_type=scope_type,
            store_id=store_id,
            store_name=store_name,
            store_address=store_address,
            source_provider="marktguru",
            source_region_id=source_region_id,
            region_verified=region_verified,
            verification_method=verification_method,
            verification_reason=verification_reason,
        )


def _image_url(source_offer_id: int, item: dict) -> str | None:
    images = item.get("images") or {}
    if images.get("count", 0) < 1 or not source_offer_id:
        return None
    return f"https://cdn.marktguru.de/api/v1/offers/{source_offer_id}/images/default/0/medium.webp"
