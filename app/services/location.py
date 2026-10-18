from __future__ import annotations

from threading import Lock
from typing import Any
import requests

QUICK_CITIES: tuple[dict[str, str], ...] = (
    {"zip_code": "80331", "city": "München", "ko": "뮌헨"},
    {"zip_code": "10115", "city": "Berlin", "ko": "베를린"},
    {"zip_code": "60311", "city": "Frankfurt", "ko": "프랑크푸르트"},
    {"zip_code": "20095", "city": "Hamburg", "ko": "함부르크"},
    {"zip_code": "50667", "city": "Köln", "ko": "쾰른"},
    {"zip_code": "70173", "city": "Stuttgart", "ko": "슈투트가르트"},
    {"zip_code": "40213", "city": "Düsseldorf", "ko": "뒤셀도르프"},
)

# Common IP-geolocation Postfach / non-street PLZs mapped to central street PLZs
POSTFACH_TO_STREET_PLZ: dict[str, str] = {
    "80313": "80331",  # München
    "80001": "80331",
    "10001": "10115",  # Berlin
    "10178": "10115",
    "60001": "60311",  # Frankfurt am Main
    "60284": "60311",
    "20001": "20095",  # Hamburg
    "50001": "50667",  # Köln
    "70001": "70173",  # Stuttgart
    "40001": "40213",  # Düsseldorf
    "04001": "04109",  # Leipzig
    "01001": "01067",  # Dresden
    "90001": "90402",  # Nürnberg
    "30001": "30159",  # Hannover
    "28001": "28195",  # Bremen
}

CITY_DEFAULT_PLZ: dict[str, str] = {
    "münchen": "80331",
    "munich": "80331",
    "berlin": "10115",
    "frankfurt am main": "60311",
    "frankfurt": "60311",
    "hamburg": "20095",
    "köln": "50667",
    "cologne": "50667",
    "stuttgart": "70173",
    "düsseldorf": "40213",
    "duesseldorf": "40213",
    "leipzig": "04109",
    "dresden": "01067",
    "nürnberg": "90402",
    "nuremberg": "90402",
    "hannover": "30159",
    "hanover": "30159",
    "bremen": "28195",
    "dortmund": "44135",
    "essen": "45127",
    "bonn": "53111",
    "mannheim": "68159",
    "karlsruhe": "76133",
    "heidelberg": "69115",
    "augsburg": "86150",
    "wiesbaden": "65183",
    "freiburg im breisgau": "79098",
    "freiburg": "79098",
    "mainz": "55116",
    "aachen": "52062",
    "kiel": "24103",
    "münster": "48143",
    "regensburg": "93047",
    "würzburg": "97070",
    "ulm": "89073",
    "ingolstadt": "85049",
    "darmstadt": "64283",
}

EXACT_PLZ_CITIES: dict[str, str] = {
    "10115": "Berlin",
    "14195": "Berlin",
    "80331": "München",
    "80333": "München",
    "80335": "München",
    "60311": "Frankfurt am Main",
    "60313": "Frankfurt am Main",
    "20095": "Hamburg",
    "50667": "Köln",
    "70173": "Stuttgart",
    "40213": "Düsseldorf",
    "04109": "Leipzig",
    "01067": "Dresden",
    "90402": "Nürnberg",
    "30159": "Hannover",
    "28195": "Bremen",
    "44135": "Dortmund",
    "45127": "Essen",
    "53111": "Bonn",
    "68159": "Mannheim",
    "76133": "Karlsruhe",
    "69115": "Heidelberg",
    "86150": "Augsburg",
}

PLZ_PREFIX2_CITIES: dict[str, str] = {
    "01": "Dresden",
    "04": "Leipzig",
    "10": "Berlin",
    "12": "Berlin",
    "13": "Berlin",
    "14": "Berlin / Potsdam",
    "20": "Hamburg",
    "21": "Hamburg",
    "22": "Hamburg",
    "24": "Kiel",
    "28": "Bremen",
    "30": "Hannover",
    "38": "Braunschweig",
    "40": "Düsseldorf",
    "44": "Dortmund",
    "45": "Essen",
    "47": "Duisburg",
    "48": "Münster",
    "50": "Köln",
    "51": "Köln",
    "52": "Aachen",
    "53": "Bonn",
    "55": "Mainz",
    "60": "Frankfurt am Main",
    "63": "Offenbach / Hanau",
    "64": "Darmstadt",
    "65": "Wiesbaden",
    "68": "Mannheim",
    "69": "Heidelberg",
    "70": "Stuttgart",
    "76": "Karlsruhe",
    "79": "Freiburg",
    "80": "München",
    "81": "München",
    "85": "München Region",
    "86": "Augsburg",
    "89": "Ulm",
    "90": "Nürnberg",
    "91": "Erlangen",
    "93": "Regensburg",
    "97": "Würzburg",
}

import math

PLZ_COORDINATES: dict[str, tuple[float, float]] = {
    "10115": (52.5323, 13.3846),
    "14195": (52.4560, 13.2870),
    "80331": (48.1371, 11.5754),
    "80339": (48.1340, 11.5370),
    "20095": (53.5511, 10.0006),
    "50667": (50.9381, 6.9566),
    "60311": (50.1109, 8.6821),
    "70173": (48.7771, 9.1808),
    "40213": (51.2228, 6.7725),
    "04109": (51.3406, 12.3747),
    "01067": (51.0536, 13.7268),
    "90402": (49.4478, 11.0815),
    "30159": (52.3734, 9.7356),
    "28195": (53.0788, 8.8071),
    "44135": (51.5136, 7.4653),
    "45127": (51.4556, 7.0116),
    "53111": (50.7374, 7.0982),
    "68159": (49.4891, 8.4662),
    "76133": (49.0069, 8.4037),
    "69115": (49.4077, 8.6908),
    "86150": (48.3705, 10.8978),
    "93047": (49.0195, 12.0975),
    "97070": (49.7944, 9.9295),
    "89073": (48.3984, 9.9916),
    "79098": (47.9961, 7.8494),
}

PLZ_PREFIX2_COORDINATES: dict[str, tuple[float, float]] = {
    "01": (51.05, 13.74),
    "02": (51.15, 14.98),
    "03": (51.76, 14.33),
    "04": (51.34, 12.37),
    "06": (51.48, 11.97),
    "07": (50.88, 12.08),
    "08": (50.72, 12.49),
    "09": (50.83, 12.92),
    "10": (52.52, 13.40),
    "12": (52.44, 13.43),
    "13": (52.58, 13.35),
    "14": (52.39, 13.06),
    "15": (52.34, 14.55),
    "16": (52.75, 13.24),
    "17": (53.56, 13.26),
    "18": (54.09, 12.13),
    "19": (53.63, 11.41),
    "20": (53.55, 9.99),
    "21": (53.46, 9.98),
    "22": (53.60, 10.05),
    "23": (53.87, 10.69),
    "24": (54.32, 10.14),
    "25": (54.78, 9.44),
    "26": (53.14, 8.21),
    "27": (53.54, 8.58),
    "28": (53.08, 8.80),
    "29": (52.62, 10.08),
    "30": (52.38, 9.73),
    "31": (52.15, 9.95),
    "32": (52.12, 8.67),
    "33": (51.72, 8.75),
    "34": (51.32, 9.50),
    "35": (50.59, 8.67),
    "36": (50.55, 9.68),
    "37": (51.53, 9.93),
    "38": (52.26, 10.53),
    "39": (52.13, 11.63),
    "40": (51.23, 6.78),
    "41": (51.19, 6.44),
    "42": (51.26, 7.15),
    "44": (51.51, 7.47),
    "45": (51.46, 7.01),
    "46": (51.49, 6.86),
    "47": (51.43, 6.76),
    "48": (51.96, 7.63),
    "49": (52.28, 8.05),
    "50": (50.94, 6.96),
    "51": (50.94, 7.02),
    "52": (50.78, 6.08),
    "53": (50.74, 7.10),
    "54": (49.76, 6.64),
    "55": (49.99, 8.27),
    "56": (50.36, 7.60),
    "57": (50.87, 8.02),
    "58": (51.36, 7.47),
    "59": (51.68, 7.82),
    "60": (50.11, 8.68),
    "61": (50.23, 8.61),
    "63": (50.10, 8.77),
    "64": (49.87, 8.65),
    "65": (50.08, 8.24),
    "66": (49.23, 7.00),
    "67": (49.48, 8.45),
    "68": (49.49, 8.47),
    "69": (49.41, 8.69),
    "70": (48.78, 9.18),
    "71": (48.89, 9.19),
    "72": (48.52, 9.06),
    "73": (48.70, 9.65),
    "74": (49.14, 9.22),
    "75": (48.89, 8.70),
    "76": (49.01, 8.40),
    "77": (48.47, 7.94),
    "78": (47.99, 8.82),
    "79": (48.00, 7.85),
    "80": (48.14, 11.58),
    "81": (48.11, 11.60),
    "82": (48.00, 11.34),
    "83": (47.86, 12.13),
    "84": (48.54, 12.15),
    "85": (48.40, 11.75),
    "86": (48.37, 10.90),
    "87": (47.73, 10.32),
    "88": (47.78, 9.61),
    "89": (48.40, 9.99),
    "90": (49.45, 11.08),
    "91": (49.60, 11.01),
    "92": (49.02, 12.10),
    "93": (49.02, 12.10),
    "94": (48.57, 13.46),
    "95": (50.32, 11.92),
    "96": (49.89, 10.89),
    "97": (49.79, 9.93),
    "98": (50.60, 10.69),
    "99": (50.98, 11.03),
}

def haversine_distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2.0) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2.0) ** 2
    return 2.0 * r * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))

def get_coordinates_for_plz(zip_code: str | None) -> tuple[float, float] | None:
    if not zip_code:
        return None
    cleaned = str(zip_code).strip()
    if cleaned in PLZ_COORDINATES:
        return PLZ_COORDINATES[cleaned]
    prefix2 = cleaned[:2]
    if prefix2 in PLZ_PREFIX2_COORDINATES:
        return PLZ_PREFIX2_COORDINATES[prefix2]
    prefix1 = cleaned[:1]
    for p2, coords in PLZ_PREFIX2_COORDINATES.items():
        if p2.startswith(prefix1):
            return coords
    return None

def is_aldi_sued_territory(zip_code: str | None) -> bool:
    if not zip_code:
        return True
    cleaned = str(zip_code).strip()
    if not cleaned or len(cleaned) < 2:
        return True
    if cleaned.startswith(("0", "1", "2", "3")) or cleaned.startswith("49"):
        return False
    return True

def is_aldi_nord_territory(zip_code: str | None) -> bool:
    if not zip_code:
        return True
    cleaned = str(zip_code).strip()
    if not cleaned or len(cleaned) < 2:
        return True
    return not is_aldi_sued_territory(cleaned)

_PLZ_CITY_CACHE: dict[str, str] = dict(EXACT_PLZ_CITIES)
_PLZ_CACHE_LOCK = Lock()

USER_AGENT = "GermanMarketSales/1.0 (https://gmarket.yocto.co.kr)"


def normalize_german_plz(zip_code: str | None, city_hint: str | None = None) -> str | None:
    if zip_code:
        cleaned = str(zip_code).strip()
        if cleaned.isdigit() and len(cleaned) == 5:
            if cleaned in POSTFACH_TO_STREET_PLZ:
                return POSTFACH_TO_STREET_PLZ[cleaned]
            return cleaned
    if city_hint:
        city_key = city_hint.strip().casefold()
        if city_key in CITY_DEFAULT_PLZ:
            return CITY_DEFAULT_PLZ[city_key]
    return None


def resolve_plz_city(zip_code: str, allow_remote: bool = False) -> str:
    cleaned = (zip_code or "").strip()
    if not cleaned:
        return ""
    with _PLZ_CACHE_LOCK:
        cached = _PLZ_CITY_CACHE.get(cleaned)
    if cached:
        return cached

    if allow_remote and cleaned.isdigit() and len(cleaned) == 5:
        remote_city = _lookup_openplz_city(cleaned)
        if remote_city:
            with _PLZ_CACHE_LOCK:
                _PLZ_CITY_CACHE[cleaned] = remote_city
            return remote_city

    return PLZ_PREFIX2_CITIES.get(cleaned[:2], "Deutschland")


def get_location_info(zip_code: str, allow_remote: bool = False) -> dict[str, str]:
    normalized = normalize_german_plz(zip_code) or zip_code
    city = resolve_plz_city(normalized, allow_remote=allow_remote)
    return {
        "zip_code": normalized,
        "city": city,
    }


def _get_json_with_tls_fallback(
    url: str,
    params: dict[str, str] | None = None,
    timeout: float = 2.5,
) -> Any | None:
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    try:
        response = requests.get(url, params=params, headers=headers, timeout=timeout)
    except requests.exceptions.SSLError:
        import urllib3
        from urllib3.exceptions import InsecureRequestWarning

        urllib3.disable_warnings(InsecureRequestWarning)
        response = requests.get(url, params=params, headers=headers, timeout=timeout, verify=False)
    if not response.ok:
        return None
    return response.json()


def _lookup_openplz_city(zip_code: str, timeout: float = 2.0) -> str | None:
    try:
        items = _get_json_with_tls_fallback(
            "https://openplzapi.org/de/Localities",
            params={"postalCode": f"^{zip_code}$"},
            timeout=timeout,
        )
        if isinstance(items, list) and items:
            first = items[0]
            if isinstance(first, dict):
                name = str(first.get("name") or "").strip()
                if name:
                    return name.split(",")[0].strip()
    except Exception:
        return None
    return None


def locate_from_coordinates(lat: float, lon: float, timeout: float = 3.0) -> dict[str, Any] | None:
    try:
        payload = _get_json_with_tls_fallback(
            "https://nominatim.openstreetmap.org/reverse",
            params={
                "format": "jsonv2",
                "lat": f"{lat:.6f}",
                "lon": f"{lon:.6f}",
                "accept-language": "de",
                "addressdetails": "1",
            },
            timeout=timeout,
        )
        address = payload.get("address") if isinstance(payload, dict) else None
        if not isinstance(address, dict):
            return None
        if str(address.get("country_code") or "").lower() != "de":
            return None

        city = (
            address.get("city")
            or address.get("town")
            or address.get("village")
            or address.get("municipality")
            or ""
        )
        city = str(city).split(",")[0].strip()
        raw_plz = str(address.get("postcode") or "").strip()
        normalized_plz = normalize_german_plz(raw_plz, city_hint=city)
        if not normalized_plz:
            return None

        if city:
            with _PLZ_CACHE_LOCK:
                _PLZ_CITY_CACHE[normalized_plz] = city
        else:
            city = resolve_plz_city(normalized_plz, allow_remote=True)

        return {
            "zip_code": normalized_plz,
            "city": city,
            "detected": True,
            "source": "gps",
        }
    except Exception:
        return None


def _is_public_ip(ip_address: str | None) -> bool:
    if not ip_address:
        return False
    ip = ip_address.strip()
    if not ip or ip in {"127.0.0.1", "::1", "localhost"}:
        return False
    if ip.startswith(("10.", "192.168.", "169.254.", "fc", "fd", "fe80:")):
        return False
    if ip.startswith("172."):
        parts = ip.split(".")
        if len(parts) >= 2 and parts[1].isdigit() and 16 <= int(parts[1]) <= 31:
            return False
    return True


def locate_from_ip(ip_address: str | None = None, timeout: float = 2.5) -> dict[str, Any] | None:
    target = ip_address.strip() if ip_address and _is_public_ip(ip_address) else ""
    url = f"https://ipwho.is/{target}" if target else "https://ipwho.is/"
    try:
        payload = _get_json_with_tls_fallback(url, timeout=timeout)
        if not isinstance(payload, dict) or not payload.get("success", True):
            return None
        if str(payload.get("country_code") or "").upper() != "DE":
            return None

        city = str(payload.get("city") or "").strip()
        raw_postal = str(payload.get("postal") or "").strip()
        normalized_plz = normalize_german_plz(raw_postal, city_hint=city)

        # If IP returned a Postfach or unverified PLZ and we have coordinates, refine it
        lat = payload.get("latitude")
        lon = payload.get("longitude")
        if (not normalized_plz or raw_postal in POSTFACH_TO_STREET_PLZ) and isinstance(lat, (int, float)) and isinstance(lon, (int, float)):
            if not normalized_plz:
                coord_match = locate_from_coordinates(float(lat), float(lon), timeout=timeout)
                if coord_match:
                    coord_match["source"] = "ip"
                    return coord_match

        if not normalized_plz:
            return None

        resolved_city = resolve_plz_city(normalized_plz, allow_remote=True) or city or "Deutschland"
        return {
            "zip_code": normalized_plz,
            "city": resolved_city,
            "detected": True,
            "source": "ip",
        }
    except Exception:
        return None
