from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime, timedelta
from functools import lru_cache
import json
import logging
from pathlib import Path
import re
import sqlite3
import unicodedata
from zoneinfo import ZoneInfo

from app.models import Offer

logger = logging.getLogger(__name__)

BERLIN_TZ = ZoneInfo("Europe/Berlin")

STOP_WORDS = frozenset(
    {
        "versch",
        "sorten",
        "verschiedene",
        "original",
        "extra",
        "packung",
        "flasche",
        "dose",
        "becher",
        "tafel",
        "glas",
        "beutel",
        "karton",
        "schale",
        "stuck",
        "stueck",
        "aktion",
        "knaller",
        "superpreis",
        "rabatt",
        "angebot",
        "g&g",
        "gut&gunstig",
        "ja!",
        "rewe",
        "edeka",
        "milbona",
        "ja",
    }
)

QUANTITY_REGEX = re.compile(
    r"\b\d+(?:[,.]\d+)?\s*(?:kg|g|ml|l|liter|stk|stuck|stueck|er|teilig|tl|x\s*\d+)?\b",
    re.IGNORECASE,
)
QUANTITY_NORM_REGEX = re.compile(
    r"\b(\d+(?:[.,]\d+)?)\s*(kg|g|ml|l|liter|stk|stueck|stuck)\b",
    re.IGNORECASE,
)
NON_ALNUM_REGEX = re.compile(r"[^a-z0-9\s]")
WHITESPACE_REGEX = re.compile(r"\s+")


def _extract_normalized_quantity(text: str) -> str | None:
    match = QUANTITY_NORM_REGEX.search(text)
    if not match:
        return None
    val_str, unit_str = match.groups()
    try:
        val = float(val_str.replace(",", "."))
    except ValueError:
        return None
    unit = unit_str.lower()
    if unit == "kg":
        return f"{int(round(val * 1000))}g"
    elif unit == "g":
        return f"{int(round(val))}g"
    elif unit in ("l", "liter"):
        return f"{int(round(val * 1000))}ml"
    elif unit == "ml":
        return f"{int(round(val))}ml"
    elif unit in ("stk", "stuck", "stueck"):
        return f"{int(round(val))}stk"
    return None


@lru_cache(maxsize=4096)
def normalize_product_key(title: str, brand: str | None = None) -> str:
    combined = f"{brand or ''} {title}".strip()
    norm_qty = _extract_normalized_quantity(combined)
    text = unicodedata.normalize("NFKD", combined)
    text = "".join(char for char in text if not unicodedata.combining(char))
    text = text.replace("ß", "ss").casefold()

    text = QUANTITY_REGEX.sub(" ", text)
    text = NON_ALNUM_REGEX.sub(" ", text)
    words = [word for word in text.split() if len(word) >= 2 and word not in STOP_WORDS]
    if not words:
        base = WHITESPACE_REGEX.sub("-", text.strip()) or "item"
    else:
        base = "-".join(sorted(set(words[:5])))
    if norm_qty:
        return f"{base}_{norm_qty}"
    return base


VERIFIED_METHODS_BY_SCOPE: dict[str, frozenset[str]] = {
    "store": frozenset(
        {
            "store_direct_api",
            "store_cookie_session",
            "marktguru_store_branch_verified",
        }
    ),
    "region": frozenset(
        {
            "marktguru_regional_flyer_postal_match",
            "marktguru_authoritative_region_mapping",
        }
    ),
    "nationwide": frozenset(
        {
            "marktguru_national_flyer",
            "verified_nationwide_campaign",
        }
    ),
}

TERRITORY_SPLIT_RETAILERS: frozenset[str] = frozenset({"aldi", "aldi-nord", "aldi-sued"})

_VERIFIED_STORE_SQL = (
    "scope_type = 'store' AND region_verified = 1 "
    "AND store_id IS NOT NULL AND TRIM(store_id) != '' "
    "AND zip_code IS NOT NULL AND LENGTH(TRIM(zip_code)) = 5 "
    "AND verification_method IN ('store_direct_api', 'store_cookie_session', 'marktguru_store_branch_verified')"
)
_VERIFIED_REGION_SQL = (
    "scope_type = 'region' AND region_verified = 1 "
    "AND source_region_id IS NOT NULL AND TRIM(source_region_id) != '' "
    "AND LOWER(source_region_id) NOT LIKE '%territory%' "
    "AND zip_code IS NOT NULL AND LENGTH(TRIM(zip_code)) = 5 "
    "AND verification_method IN ('marktguru_regional_flyer_postal_match', 'marktguru_authoritative_region_mapping')"
)
_VERIFIED_NATIONWIDE_SQL = (
    "scope_type = 'nationwide' AND region_verified = 1 "
    "AND source_region_id IS NOT NULL "
    "AND (source_region_id LIKE 'national_flyer:%' OR source_region_id LIKE 'national_campaign:%') "
    "AND source_provider IS NOT NULL AND TRIM(source_provider) != '' "
    "AND LOWER(retailer_slug) NOT IN ('aldi', 'aldi-nord', 'aldi-sued') "
    "AND verification_method IN ('marktguru_national_flyer', 'verified_nationwide_campaign')"
)


def build_store_verification_evidence(
    *,
    store_id: str,
    retailer_slug: str,
    store_address: str,
    requested_plz: str,
    store_name: str | None = None,
    metadata_source: str = "official_store_api",
    verification_method: str = "store_direct_api",
) -> dict[str, Any]:
    """
    Builds an authoritative store verification evidence dictionary tying
    the exact store ID, retailer, verified physical address, and postal code
    to an authoritative metadata source.
    """
    m = re.search(r"\b(\d{5})\b", store_address)
    store_plz = m.group(1) if m else requested_plz
    return {
        "scope": "store",
        "retailer_slug": retailer_slug.strip().lower(),
        "store_id": str(store_id).strip(),
        "store_name": store_name,
        "store_address": store_address.strip(),
        "store_plz": store_plz,
        "matched_plz": requested_plz.strip(),
        "requested_plz": requested_plz.strip(),
        "verification_method": verification_method,
        "metadata_source": metadata_source,
    }


def validate_observation_provenance(
    *,
    scope_type: str | None,
    region_verified: bool | int | None,
    verification_method: str | None,
    store_id: str | None = None,
    store_name: str | None = None,
    store_address: str | None = None,
    source_region_id: str | None = None,
    source_provider: str | None = None,
    source_url: str | None = None,
    zip_code: str | None = None,
    retailer_slug: str | None = None,
    verification_evidence: str | dict | None = None,
) -> tuple[bool, str, str]:
    """
    Validates whether an offer or historical price observation has complete,
    non-contradictory, allowlisted provenance for its claimed scope_type.

    Returns (is_verified, normalized_verification_method, reason).
    """
    from app.services.location import get_coordinates_for_plz, haversine_distance_km

    raw_method = (verification_method or "").strip()
    if not bool(region_verified):
        return (False, raw_method or "unverified", "Observation is not marked verified")

    scope = (scope_type or "").strip()
    if scope not in VERIFIED_METHODS_BY_SCOPE:
        return (
            False,
            f"demoted_invalid_scope:{raw_method or 'missing'}",
            f"Scope '{scope}' cannot be verified",
        )

    if not raw_method or raw_method not in VERIFIED_METHODS_BY_SCOPE[scope]:
        return (
            False,
            f"demoted_unrecognized_method:{raw_method or 'missing'}",
            f"Verification method '{raw_method}' is not in the allowlist for scope '{scope}'",
        )

    clean_url = (source_url or "").strip()
    if not clean_url or not (clean_url.startswith("http://") or clean_url.startswith("https://")):
        return (
            False,
            "demoted_missing_source_url",
            "Verified observation requires a valid HTTP(S) source_url",
        )

    clean_provider = (source_provider or "").strip().lower()
    clean_retailer = (retailer_slug or "").strip().lower()

    parsed_evidence: dict | None = None
    if verification_evidence:
        if isinstance(verification_evidence, dict):
            parsed_evidence = verification_evidence
        elif isinstance(verification_evidence, str):
            try:
                parsed_evidence = json.loads(verification_evidence)
            except Exception:
                return (
                    False,
                    "demoted_malformed_evidence",
                    "Stored verification_evidence is malformed JSON",
                )

    if scope == "store":
        clean_store_id = (store_id or "").strip()
        if not clean_store_id:
            return (
                False,
                "demoted_missing_store_id",
                "Store-scoped verification requires a non-empty store_id",
            )

        clean_zip = (zip_code or "").strip()
        user_coords = get_coordinates_for_plz(clean_zip) if len(clean_zip) == 5 and clean_zip.isdigit() else None
        if not user_coords:
            return (
                False,
                "demoted_missing_location_evidence",
                f"Store-scoped verification requires a valid 5-digit German PLZ with coordinates (got '{clean_zip}')",
            )

        from app.services.location import normalize_german_plz
        norm_zip = normalize_german_plz(clean_zip) or clean_zip

        # If neither store_address nor parsed_evidence provides an address, fail with missing location evidence
        ev_address_candidate = (
            (parsed_evidence.get("store_address") if isinstance(parsed_evidence, dict) else None)
            or store_address
            or ""
        )
        if not str(ev_address_candidate).strip():
            return (
                False,
                "demoted_missing_store_location_evidence",
                f"Store verification requires authoritative store location evidence connecting store '{clean_store_id}' to requested PLZ '{clean_zip}'",
            )

        # Inconsistency between store_plz and matched_plz in evidence
        ev_store_plz = str(parsed_evidence.get("store_plz") or "").strip() if isinstance(parsed_evidence, dict) else ""
        ev_matched_plz = str(parsed_evidence.get("matched_plz") or "").strip() if isinstance(parsed_evidence, dict) else ""
        if ev_store_plz and ev_matched_plz and ev_store_plz != ev_matched_plz:
            return (
                False,
                "demoted_store_evidence_inconsistency",
                f"Evidence contains inconsistent store_plz '{ev_store_plz}' and matched_plz '{ev_matched_plz}'",
            )

        # Mismatched store_id or retailer in evidence
        if isinstance(parsed_evidence, dict):
            ev_store_id = str(parsed_evidence.get("store_id") or "").strip()
            if ev_store_id and ev_store_id != clean_store_id:
                return (
                    False,
                    "demoted_store_evidence_mismatch",
                    f"Store evidence ID '{ev_store_id}' contradicts observation store_id '{clean_store_id}'",
                )

            ev_retailer = str(parsed_evidence.get("retailer_slug") or parsed_evidence.get("retailer") or "").strip().lower()
            if ev_retailer and clean_retailer and ev_retailer != clean_retailer:
                return (
                    False,
                    "demoted_retailer_evidence_mismatch",
                    f"Store evidence retailer '{ev_retailer}' contradicts observation retailer '{clean_retailer}'",
                )

        # Require authoritative store metadata evidence linking exact store ID, retailer, address, and PLZ.
        # A caller-supplied store_address or PLZ string alone does not independently establish store verification.
        if parsed_evidence is None or not isinstance(parsed_evidence, dict) or not parsed_evidence:
            return (
                False,
                "demoted_missing_store_evidence",
                f"Store verification requires authoritative store metadata evidence linking store '{clean_store_id}' to requested PLZ '{clean_zip}'; caller-supplied address string alone is rejected",
            )

        # Check store_id in evidence
        if not ev_store_id:
            return (
                False,
                "demoted_missing_store_id_evidence",
                "Store evidence lacks store_id",
            )

        # Require evidence identifying the authoritative source of the store metadata
        metadata_source = str(
            parsed_evidence.get("metadata_source")
            or parsed_evidence.get("source")
            or parsed_evidence.get("evidence_source")
            or parsed_evidence.get("store_metadata_source")
            or ""
        ).strip()
        if not metadata_source:
            return (
                False,
                "demoted_missing_authoritative_metadata_source",
                f"Store evidence for store '{clean_store_id}' lacks identification of authoritative store metadata source",
            )

        if not clean_retailer:
            return (
                False,
                "demoted_missing_retailer_identity",
                "Store-scoped verification requires a non-empty retailer identity",
            )
        if not ev_retailer:
            return (
                False,
                "demoted_missing_retailer_evidence",
                "Store evidence lacks retailer identity",
            )

        # Check store physical address in evidence
        ev_address = str(parsed_evidence.get("store_address") or "").strip()
        if not ev_address:
            return (
                False,
                "demoted_missing_store_location_evidence",
                f"Store evidence lacks physical store address for store '{clean_store_id}'",
            )

        m_plz = re.search(r"\b(\d{5})\b", ev_address)
        if not m_plz:
            return (
                False,
                "demoted_missing_store_location_evidence",
                f"Store evidence address '{ev_address}' lacks a valid 5-digit postal code",
            )
        addr_plz = m_plz.group(1)
        if addr_plz != clean_zip and addr_plz != norm_zip:
            return (
                False,
                "demoted_store_location_mismatch",
                f"Store evidence address PLZ {addr_plz} does not match requested PLZ {clean_zip}",
            )

        # If caller also passed store_address, verify consistency with evidence
        if store_address and str(store_address).strip() != ev_address:
            caller_m = re.search(r"\b(\d{5})\b", str(store_address))
            if caller_m and caller_m.group(1) != addr_plz:
                return (
                    False,
                    "demoted_store_address_evidence_mismatch",
                    f"Observation store address PLZ '{caller_m.group(1)}' contradicts authoritative evidence address PLZ '{addr_plz}'",
                )

        # Check store_plz and matched_plz in evidence
        ev_store_plz = str(parsed_evidence.get("store_plz") or "").strip()
        ev_matched_plz = str(parsed_evidence.get("matched_plz") or "").strip()
        if ev_store_plz and ev_matched_plz and ev_store_plz != ev_matched_plz:
            return (
                False,
                "demoted_store_evidence_inconsistency",
                f"Evidence contains inconsistent store_plz '{ev_store_plz}' and matched_plz '{ev_matched_plz}'",
            )

        for field_name, field_val in (("store_plz", ev_store_plz), ("matched_plz", ev_matched_plz)):
            if field_val and field_val != clean_zip and field_val != norm_zip:
                return (
                    False,
                    "demoted_store_location_mismatch",
                    f"Store evidence {field_name} '{field_val}' does not match requested PLZ '{clean_zip}'",
                )

        if raw_method == "marktguru_store_branch_verified" and clean_provider and clean_provider != "marktguru":
            return (
                False,
                "demoted_inconsistent_provider",
                f"Inconsistent source_provider '{clean_provider}' for marktguru_store_branch_verified",
            )
        if raw_method in ("store_direct_api", "store_cookie_session") and clean_provider == "marktguru":
            return (
                False,
                "demoted_inconsistent_provider",
                f"Inconsistent source_provider '{clean_provider}' for {raw_method}",
            )

        return (True, raw_method, f"Verified store observation for store {clean_store_id} at PLZ {clean_zip}")

    if scope == "region":
        clean_reg_id = (source_region_id or "").strip()
        if not clean_reg_id:
            return (
                False,
                "demoted_missing_region_id",
                "Regional verification requires a non-empty source_region_id",
            )
        if "territory" in clean_reg_id.lower():
            return (
                False,
                "demoted_territory_not_regional_proof",
                f"Retailer territory '{clean_reg_id}' is not offer-level regional proof",
            )

        clean_zip = (zip_code or "").strip()
        user_coords = get_coordinates_for_plz(clean_zip) if len(clean_zip) == 5 and clean_zip.isdigit() else None
        if not user_coords:
            return (
                False,
                "demoted_missing_location_evidence",
                f"Regional verification requires a valid 5-digit German PLZ with coordinates (got '{clean_zip}')",
            )

        if raw_method in ("marktguru_authoritative_region_mapping", "marktguru_regional_flyer_postal_match"):
            from app.providers.marktguru import (
                get_verified_campaign_plzs,
                register_verified_campaign,
                validate_regional_campaign_evidence,
            )

            if parsed_evidence is not None:
                plzs = parsed_evidence.get("covered_plzs") or []
                is_ev_valid, ev_reason, val_ev = validate_regional_campaign_evidence(clean_reg_id, plzs, parsed_evidence)
                if not is_ev_valid:
                    return (
                        False,
                        "demoted_invalid_regional_evidence",
                        f"Regional evidence failed validation: {ev_reason}",
                    )
                register_verified_campaign(clean_reg_id, plzs, val_ev)

            covered = get_verified_campaign_plzs(clean_reg_id)
            if not covered or clean_zip not in covered:
                return (
                    False,
                    "demoted_unsubstantiated_campaign_evidence",
                    f"Region or campaign '{clean_reg_id}' lacks substantiated campaign evidence for PLZ {clean_zip}",
                )

        return (True, raw_method, f"Verified regional observation for region {clean_reg_id} at PLZ {clean_zip}")

    if scope == "nationwide":
        if clean_retailer in TERRITORY_SPLIT_RETAILERS:
            return (
                False,
                "demoted_territory_split_nationwide",
                f"Retailer '{clean_retailer}' is territory-split and cannot be verified as nationwide",
            )

        clean_reg_id = (source_region_id or "").strip()
        has_valid_campaign_id = False
        for prefix in ("national_flyer:", "national_campaign:"):
            if clean_reg_id.startswith(prefix) and len(clean_reg_id[len(prefix) :].strip()) > 0:
                has_valid_campaign_id = True
                break
        if not has_valid_campaign_id:
            return (
                False,
                "demoted_missing_nationwide_campaign_id",
                "Nationwide verification requires source_region_id with 'national_flyer:<id>' or 'national_campaign:<id>'",
            )

        if not clean_provider:
            return (
                False,
                "demoted_missing_source_provider",
                "Nationwide verification requires a non-empty source_provider",
            )

        if raw_method == "marktguru_national_flyer" and clean_provider != "marktguru":
            return (
                False,
                "demoted_inconsistent_provider",
                f"Inconsistent source_provider '{clean_provider}' for marktguru_national_flyer",
            )

        from app.providers.marktguru import (
            is_verified_nationwide_campaign,
            register_verified_nationwide_campaign,
            validate_nationwide_coverage_evidence,
        )

        if parsed_evidence is not None:
            is_ev_valid, ev_reason, val_ev = validate_nationwide_coverage_evidence(clean_reg_id, parsed_evidence)
            if not is_ev_valid:
                return (
                    False,
                    "demoted_invalid_nationwide_evidence",
                    f"Nationwide evidence failed validation: {ev_reason}",
                )
            register_verified_nationwide_campaign(clean_reg_id, val_ev)

        if not is_verified_nationwide_campaign(clean_reg_id):
            return (
                False,
                "demoted_unsubstantiated_nationwide_claim",
                f"Nationwide claim '{clean_reg_id}' lacks scope-specific evidence establishing promotional applicability across Germany",
            )

        return (True, raw_method, f"Verified nationwide observation ({clean_reg_id})")

    return (False, "demoted_unknown_scope", "Unrecognized scope")


def get_geographic_identity(offer: Offer, fallback_zip: str) -> tuple[str, str, str]:
    """
    Returns (scope_type, geo_id, effective_zip) ensuring consistent identity per scope:
    - store: ('store', store_id or effective_zip, effective_zip)
    - region: ('region', region_id or effective_zip, effective_zip)
    - nationwide: ('nationwide', 'DE', 'DE')
    - unknown: ('unknown', effective_zip, effective_zip)
    """
    scope = offer.scope_type or "unknown"
    effective_zip = offer.requested_zip_code or fallback_zip
    if scope == "store":
        geo_id = offer.store_id or offer.store_name or effective_zip
        return ("store", geo_id, effective_zip)
    elif scope == "region":
        geo_id = offer.source_region_id or effective_zip
        return ("region", geo_id, effective_zip)
    elif scope == "nationwide":
        return ("nationwide", "DE", "DE")
    else:
        return ("unknown", effective_zip, effective_zip)


class PriceHistoryService:
    _INITIALIZED_DBS: set[Path] = set()
    _SEEDED_DBS: set[Path] = set()

    def __init__(self, cache_dir: Path, db_name: str = "price_history.db") -> None:
        self.cache_dir = cache_dir
        self.db_path = (cache_dir / db_name).resolve()
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._atl_cache: tuple[int, dict[str, float]] | None = None
        if self.db_path not in self._INITIALIZED_DBS or not self.db_path.exists():
            self._init_db()
            self._INITIALIZED_DBS.add(self.db_path)

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._get_connection() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS price_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    product_key TEXT NOT NULL,
                    title TEXT NOT NULL,
                    brand TEXT,
                    category TEXT,
                    retailer_slug TEXT NOT NULL,
                    retailer_name TEXT NOT NULL,
                    price REAL,
                    old_price REAL,
                    unit_price REAL,
                    unit TEXT,
                    discount_percent REAL,
                    recorded_at TEXT NOT NULL,
                    valid_from TEXT,
                    valid_to TEXT,
                    source_url TEXT,
                    zip_code TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS verified_campaign_registry (
                    campaign_id TEXT PRIMARY KEY,
                    scope_type TEXT NOT NULL,
                    evidence_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_vcr_camp ON verified_campaign_registry(campaign_id)")

            # Safe migration for existing tables:
            cursor = conn.execute("PRAGMA table_info(price_history)")
            existing_cols = {row["name"] for row in cursor.fetchall()}
            new_cols = {
                "scope_type": "TEXT DEFAULT 'unknown'",
                "store_id": "TEXT",
                "store_name": "TEXT",
                "store_address": "TEXT",
                "region_verified": "INTEGER DEFAULT 0",
                "source_provider": "TEXT",
                "source_region_id": "TEXT",
                "verification_method": "TEXT",
                "verification_evidence": "TEXT",
            }
            for col_name, col_type in new_cols.items():
                if col_name not in existing_cols:
                    conn.execute(f"ALTER TABLE price_history ADD COLUMN {col_name} {col_type}")

            conn.execute("CREATE INDEX IF NOT EXISTS idx_ph_key ON price_history(product_key)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_ph_date ON price_history(recorded_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_ph_retailer ON price_history(retailer_slug)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_ph_zip ON price_history(zip_code)")

            self._revalidate_historical_records_conn(conn)
            conn.commit()

    def revalidate_historical_records(self) -> int:
        """
        Idempotently audits all historical rows in price_history against the
        strict provenance allowlist and demotes any unsubstantiated records to
        region_verified = 0 without deleting raw rows. Returns number of demoted rows.
        """
        with self._get_connection() as conn:
            demoted = self._revalidate_historical_records_conn(conn)
            conn.commit()
            if demoted > 0:
                self._atl_cache = None
            return demoted

    @staticmethod
    def _revalidate_historical_records_conn(conn: sqlite3.Connection) -> int:
        # 1. Reload verified campaigns from verified_campaign_registry table into Marktguru registries
        try:
            v_rows = conn.execute("SELECT campaign_id, scope_type, evidence_json FROM verified_campaign_registry").fetchall()
            from app.providers.marktguru import register_verified_campaign, register_verified_nationwide_campaign

            for v_row in v_rows:
                c_id = v_row["campaign_id"]
                s_type = v_row["scope_type"]
                try:
                    ev_data = json.loads(v_row["evidence_json"])
                except Exception as exc:
                    logger.warning("Malformed JSON in verified_campaign_registry for campaign '%s': %s", c_id, exc)
                    continue

                if s_type == "nationwide":
                    success = register_verified_nationwide_campaign(c_id, ev_data)
                    if not success:
                        logger.warning("Failed to restore verified nationwide campaign '%s': invalid evidence", c_id)
                elif s_type == "region":
                    plzs = ev_data.get("covered_plzs") or ev_data.get("validZipCodes") or []
                    success = register_verified_campaign(c_id, plzs, ev_data)
                    if not success:
                        logger.warning("Failed to restore verified regional campaign '%s': invalid evidence", c_id)
        except sqlite3.OperationalError as exc:
            logger.debug("verified_campaign_registry table not available during revalidation: %s", exc)
        except Exception as exc:
            logger.warning("Unexpected error reading verified_campaign_registry: %s", exc)

        cursor = conn.execute("PRAGMA table_info(price_history)")
        col_names = {row["name"] for row in cursor.fetchall()}
        store_addr_select = "store_address" if "store_address" in col_names else "NULL as store_address"
        evidence_select = "verification_evidence" if "verification_evidence" in col_names else "NULL as verification_evidence"

        rows = conn.execute(
            f"""
            SELECT id, scope_type, region_verified, verification_method,
                   store_id, store_name, {store_addr_select}, source_region_id, source_provider,
                   source_url, zip_code, retailer_slug, {evidence_select}
            FROM price_history
            WHERE region_verified = 1
            """
        ).fetchall()

        updates: list[tuple[str, int]] = []
        for row in rows:
            is_valid, norm_method, _ = validate_observation_provenance(
                scope_type=row["scope_type"],
                region_verified=row["region_verified"],
                verification_method=row["verification_method"],
                store_id=row["store_id"],
                store_name=row["store_name"],
                store_address=row["store_address"],
                source_region_id=row["source_region_id"],
                source_provider=row["source_provider"],
                source_url=row["source_url"],
                zip_code=row["zip_code"],
                retailer_slug=row["retailer_slug"],
                verification_evidence=row["verification_evidence"],
            )
            if not is_valid:
                updates.append((norm_method, row["id"]))

        if updates:
            conn.executemany(
                """
                UPDATE price_history
                SET region_verified = 0,
                    verification_method = ?
                WHERE id = ?
                """,
                updates,
            )

        conn.execute(
            """
            UPDATE price_history
            SET verification_method = 'unverified_legacy_unsubstantiated'
            WHERE region_verified = 0
              AND (verification_method IS NULL OR TRIM(verification_method) = '')
            """
        )
        return len(updates)

    def record_offers(
        self,
        offers: list[Offer],
        zip_code: str,
        recorded_at: str | None = None,
    ) -> int:
        if not offers:
            return 0

        timestamp = recorded_at or datetime.now(UTC).isoformat(timespec="seconds")
        date_prefix = timestamp.split("T", 1)[0]

        with self._get_connection() as conn:
            existing_rows = conn.execute(
                """
                SELECT product_key, retailer_slug, COALESCE(scope_type, 'unknown') as scope_type,
                       COALESCE(store_id, '') as store_id, COALESCE(source_region_id, '') as source_region_id,
                       COALESCE(zip_code, '') as zip_code, COALESCE(region_verified, 0) as region_verified, price
                FROM price_history
                WHERE recorded_at LIKE ? AND price IS NOT NULL
                """,
                (f"{date_prefix}%",),
            ).fetchall()
            seen_today: set[tuple[str, str, str, str, str, int, float]] = set()
            for row in existing_rows:
                s_type = row["scope_type"]
                eff_zip = "DE" if s_type == "nationwide" else row["zip_code"]
                if s_type == "store":
                    g_id = row["store_id"] or eff_zip
                elif s_type == "region":
                    g_id = row["source_region_id"] or eff_zip
                elif s_type == "nationwide":
                    g_id = "DE"
                else:
                    g_id = eff_zip
                seen_today.add(
                    (
                        row["product_key"],
                        row["retailer_slug"],
                        s_type,
                        g_id,
                        eff_zip,
                        int(row["region_verified"]),
                        round(float(row["price"]), 4),
                    )
                )

            rows_to_insert: list[tuple] = []
            for offer in offers:
                if offer.price is None:
                    continue

                product_key = offer.product_key or normalize_product_key(offer.title, offer.brand)
                scope_type, geo_id, effective_zip = get_geographic_identity(offer, zip_code)

                # Auto-populate verification evidence from registries if present
                if offer.region_verified and not offer.verification_evidence:
                    if offer.scope_type == "nationwide":
                        from app.providers.marktguru import get_verified_nationwide_evidence

                        offer.verification_evidence = get_verified_nationwide_evidence(offer.source_region_id)
                    elif offer.scope_type == "region":
                        from app.providers.marktguru import get_verified_campaign_evidence

                        offer.verification_evidence = get_verified_campaign_evidence(offer.source_region_id)

                raw_ev = offer.verification_evidence
                ev_json_str = None
                if raw_ev is not None:
                    if isinstance(raw_ev, (dict, list)):
                        ev_json_str = json.dumps(raw_ev)
                    elif isinstance(raw_ev, str):
                        ev_json_str = raw_ev

                is_verified, norm_method, _ = validate_observation_provenance(
                    scope_type=scope_type,
                    region_verified=offer.region_verified,
                    verification_method=offer.verification_method,
                    store_id=offer.store_id,
                    store_name=offer.store_name,
                    store_address=offer.store_address,
                    source_region_id=offer.source_region_id,
                    source_provider=offer.source_provider,
                    source_url=offer.source_url,
                    zip_code=effective_zip,
                    retailer_slug=offer.retailer_slug,
                    verification_evidence=raw_ev,
                )
                verified_int = 1 if is_verified else 0

                # Persist verified campaign provenance to verified_campaign_registry table
                if is_verified and scope_type in ("nationwide", "region") and offer.source_region_id and ev_json_str:
                    try:
                        conn.execute(
                            """
                            INSERT OR REPLACE INTO verified_campaign_registry (campaign_id, scope_type, evidence_json, created_at)
                            VALUES (?, ?, ?, ?)
                            """,
                            (offer.source_region_id, scope_type, ev_json_str, timestamp),
                        )
                    except sqlite3.Error as exc:
                        logger.warning("Failed to persist verified campaign '%s' to registry: %s", offer.source_region_id, exc)

                dedup_key = (
                    product_key,
                    offer.retailer_slug,
                    scope_type,
                    geo_id,
                    effective_zip,
                    verified_int,
                    round(float(offer.price), 4),
                )
                if dedup_key in seen_today:
                    continue
                seen_today.add(dedup_key)

                rows_to_insert.append(
                    (
                        product_key,
                        offer.title,
                        offer.brand,
                        offer.category,
                        offer.retailer_slug,
                        offer.retailer,
                        offer.price,
                        offer.old_price,
                        offer.unit_price,
                        offer.unit,
                        offer.discount_percent,
                        timestamp,
                        offer.valid_from,
                        offer.valid_to,
                        offer.source_url,
                        effective_zip,
                        scope_type,
                        offer.store_id,
                        offer.store_name,
                        offer.store_address,
                        verified_int,
                        offer.source_provider,
                        offer.source_region_id,
                        norm_method if offer.region_verified and not is_verified else (offer.verification_method or norm_method),
                        ev_json_str,
                    )
                )

            if rows_to_insert:
                conn.executemany(
                    """
                    INSERT INTO price_history (
                        product_key, title, brand, category, retailer_slug, retailer_name,
                        price, old_price, unit_price, unit, discount_percent, recorded_at,
                        valid_from, valid_to, source_url, zip_code,
                        scope_type, store_id, store_name, store_address, region_verified, source_provider,
                        source_region_id, verification_method, verification_evidence
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    rows_to_insert,
                )
                conn.commit()
                self._atl_cache = None

        return len(rows_to_insert)

    def get_all_time_low_map(self, zip_code: str | None = None, store_id: str | None = None) -> dict[str, float]:
        with self._get_connection() as conn:
            if zip_code:
                if store_id:
                    store_clause = f"({_VERIFIED_STORE_SQL} AND store_id = ?)"
                    params = (store_id, zip_code)
                else:
                    store_clause = f"({_VERIFIED_STORE_SQL} AND zip_code = ?)"
                    params = (zip_code, zip_code)
                query = f"""
                    SELECT product_key, MIN(price) as min_price
                    FROM price_history
                    WHERE price IS NOT NULL AND price > 0
                      AND (
                          {store_clause}
                          OR ({_VERIFIED_REGION_SQL} AND zip_code = ?)
                          OR ({_VERIFIED_NATIONWIDE_SQL})
                      )
                    GROUP BY product_key
                """
                cursor = conn.execute(query, params)
            else:
                cursor = conn.execute(
                    f"""
                    SELECT product_key, MIN(price) as min_price
                    FROM price_history
                    WHERE price IS NOT NULL AND price > 0
                      AND (
                          ({_VERIFIED_STORE_SQL})
                          OR ({_VERIFIED_REGION_SQL})
                          OR ({_VERIFIED_NATIONWIDE_SQL})
                      )
                    GROUP BY product_key
                    """
                )
            return {row["product_key"]: float(row["min_price"]) for row in cursor.fetchall()}

    def get_product_stats(
        self,
        title: str,
        brand: str | None = None,
        current_price: float | None = None,
        zip_code: str | None = None,
        store_id: str | None = None,
    ) -> dict:
        product_key = normalize_product_key(title, brand)
        with self._get_connection() as conn:
            cursor = conn.execute(
                f"""
                SELECT
                    MIN(price) as min_price,
                    MAX(price) as max_price,
                    AVG(price) as avg_price,
                    COUNT(*) as count
                FROM price_history
                WHERE product_key = ? AND price IS NOT NULL
                  AND (
                      ({_VERIFIED_STORE_SQL})
                      OR ({_VERIFIED_REGION_SQL})
                      OR ({_VERIFIED_NATIONWIDE_SQL})
                  )
                """,
                (product_key,),
            )
            agg = cursor.fetchone()

            min_price = float(agg["min_price"]) if agg and agg["min_price"] is not None else None
            max_price = float(agg["max_price"]) if agg and agg["max_price"] is not None else None
            avg_price = float(round(agg["avg_price"], 2)) if agg and agg["avg_price"] is not None else None
            record_count = int(agg["count"]) if agg else 0

            regional_min = None
            regional_avg = None
            is_regional_atl = False
            if zip_code:
                if store_id:
                    store_clause = f"({_VERIFIED_STORE_SQL} AND store_id = ?)"
                    r_params = (product_key, store_id, zip_code)
                else:
                    store_clause = f"({_VERIFIED_STORE_SQL} AND zip_code = ?)"
                    r_params = (product_key, zip_code, zip_code)
                r_query = f"""
                    SELECT
                        MIN(price) as min_price,
                        AVG(price) as avg_price
                    FROM price_history
                    WHERE product_key = ? AND price IS NOT NULL
                      AND (
                          {store_clause}
                          OR ({_VERIFIED_REGION_SQL} AND zip_code = ?)
                          OR ({_VERIFIED_NATIONWIDE_SQL})
                      )
                """
                r_cursor = conn.execute(r_query, r_params)
                r_agg = r_cursor.fetchone()
                if r_agg and r_agg["min_price"] is not None:
                    regional_min = float(r_agg["min_price"])
                    regional_avg = float(round(r_agg["avg_price"], 2))
                    if current_price is not None:
                        is_regional_atl = current_price <= (regional_min + 0.001)

            # Fetch recent price observation points: strictly isolate by scope / location
            if zip_code:
                if store_id:
                    store_p_clause = f"({_VERIFIED_STORE_SQL} AND store_id = ?)"
                    p_params = (product_key, store_id, zip_code)
                else:
                    store_p_clause = f"({_VERIFIED_STORE_SQL} AND zip_code = ?)"
                    p_params = (product_key, zip_code, zip_code)
                p_query = f"""
                    SELECT
                        recorded_at, price, old_price, retailer_name, retailer_slug,
                        source_url, unit_price, unit, discount_percent, zip_code, scope_type, store_name,
                        store_id, region_verified
                    FROM price_history
                    WHERE product_key = ? AND price IS NOT NULL
                      AND (
                          {store_p_clause}
                          OR ({_VERIFIED_REGION_SQL} AND zip_code = ?)
                          OR ({_VERIFIED_NATIONWIDE_SQL})
                      )
                    ORDER BY recorded_at ASC
                    LIMIT 30
                """
                points_cursor = conn.execute(p_query, p_params)
            else:
                p_query = f"""
                    SELECT
                        recorded_at, price, old_price, retailer_name, retailer_slug,
                        source_url, unit_price, unit, discount_percent, zip_code, scope_type, store_name,
                        store_id, region_verified
                    FROM price_history
                    WHERE product_key = ? AND price IS NOT NULL
                      AND (
                          ({_VERIFIED_STORE_SQL})
                          OR ({_VERIFIED_REGION_SQL})
                          OR ({_VERIFIED_NATIONWIDE_SQL})
                      )
                    ORDER BY recorded_at ASC
                    LIMIT 30
                """
                points_cursor = conn.execute(p_query, (product_key,))

            history = [
                {
                    "date": row["recorded_at"].split("T", 1)[0],
                    "recorded_at": row["recorded_at"],
                    "price": float(row["price"]),
                    "old_price": float(row["old_price"]) if row["old_price"] is not None else None,
                    "retailer": row["retailer_name"],
                    "retailer_slug": row["retailer_slug"],
                    "source_url": row["source_url"],
                    "unit_price": row["unit_price"],
                    "unit": row["unit"],
                    "discount_percent": row["discount_percent"],
                    "zip_code": row["zip_code"],
                    "scope_type": row["scope_type"],
                    "store_name": row["store_name"],
                    "store_id": row["store_id"],
                    "region_verified": bool(row["region_verified"]),
                }
                for row in points_cursor.fetchall()
            ]

            # Unverified and unknown-scope observations exposed separately for UI transparency
            if zip_code:
                unv_query = f"""
                    SELECT
                        recorded_at, price, old_price, retailer_name, retailer_slug,
                        source_url, unit_price, unit, discount_percent, zip_code, scope_type, store_name,
                        store_id, region_verified
                    FROM price_history
                    WHERE product_key = ? AND price IS NOT NULL
                      AND NOT (
                          ({_VERIFIED_STORE_SQL})
                          OR ({_VERIFIED_REGION_SQL})
                          OR ({_VERIFIED_NATIONWIDE_SQL})
                      )
                      AND (zip_code = ? OR zip_code IS NULL OR zip_code = '' OR scope_type = 'nationwide')
                    ORDER BY recorded_at ASC
                    LIMIT 30
                """
                unv_cursor = conn.execute(unv_query, (product_key, zip_code))
            else:
                unv_query = f"""
                    SELECT
                        recorded_at, price, old_price, retailer_name, retailer_slug,
                        source_url, unit_price, unit, discount_percent, zip_code, scope_type, store_name,
                        store_id, region_verified
                    FROM price_history
                    WHERE product_key = ? AND price IS NOT NULL
                      AND NOT (
                          ({_VERIFIED_STORE_SQL})
                          OR ({_VERIFIED_REGION_SQL})
                          OR ({_VERIFIED_NATIONWIDE_SQL})
                      )
                    ORDER BY recorded_at ASC
                    LIMIT 30
                """
                unv_cursor = conn.execute(unv_query, (product_key,))

            unverified_history = [
                {
                    "date": row["recorded_at"].split("T", 1)[0],
                    "recorded_at": row["recorded_at"],
                    "price": float(row["price"]),
                    "old_price": float(row["old_price"]) if row["old_price"] is not None else None,
                    "retailer": row["retailer_name"],
                    "retailer_slug": row["retailer_slug"],
                    "source_url": row["source_url"],
                    "unit_price": row["unit_price"],
                    "unit": row["unit"],
                    "discount_percent": row["discount_percent"],
                    "zip_code": row["zip_code"],
                    "scope_type": row["scope_type"],
                    "store_name": row["store_name"],
                    "store_id": row["store_id"],
                    "region_verified": False,
                }
                for row in unv_cursor.fetchall()
            ]

        is_all_time_low = False
        if current_price is not None and min_price is not None:
            is_all_time_low = current_price <= (min_price + 0.001)

        # When zip_code is specified, do not leak another region's min/avg price into regional display fields
        display_atl = regional_min if zip_code else min_price
        display_is_atl = is_regional_atl if zip_code else is_all_time_low
        display_avg = regional_avg if zip_code else avg_price

        return {
            "product_key": product_key,
            "title": title,
            "product_name": title,
            "brand": brand,
            "current_price": current_price,
            "all_time_low": display_atl,
            "all_time_high": max_price,
            "average_price": display_avg,
            "avg_price": display_avg,
            "regional_all_time_low": regional_min,
            "national_all_time_low": min_price,
            "is_all_time_low": display_is_atl,
            "is_regional_all_time_low": is_regional_atl,
            "is_national_all_time_low": is_all_time_low,
            "record_count": record_count,
            "records_count": record_count,
            "history": history,
            "unverified_history": unverified_history,
        }

    def compare_across_stores(self, query: str, offers: list[Offer]) -> dict:
        query_key = query.casefold().strip()
        tokens = [t for t in WHITESPACE_REGEX.split(query_key) if len(t) >= 2] or [query_key]

        matching: list[Offer] = []
        for offer in offers:
            haystack = f"{offer.title} {offer.brand or ''} {offer.description}".casefold()
            if all(token in haystack for token in tokens):
                matching.append(offer)

        by_retailer: dict[str, list[dict]] = {}
        for offer in matching:
            by_retailer.setdefault(offer.retailer, []).append(
                {
                    "id": offer.id,
                    "title": offer.title,
                    "brand": offer.brand,
                    "retailer": offer.retailer,
                    "retailer_slug": offer.retailer_slug,
                    "price": offer.price,
                    "old_price": offer.old_price,
                    "unit_price": offer.unit_price,
                    "unit": offer.unit,
                    "discount_percent": offer.discount_percent,
                    "image_url": offer.image_url,
                    "source_url": offer.source_url,
                    "valid_from": offer.valid_from,
                    "valid_to": offer.valid_to,
                    "scope_type": offer.scope_type,
                    "store_name": offer.store_name,
                    "region_verified": offer.region_verified,
                }
            )

        priced = [o for o in matching if o.price is not None and o.price > 0]
        cheapest_offer = min(priced, key=lambda o: o.price) if priced else None
        most_expensive = max(priced, key=lambda o: o.price) if priced else None

        min_price = cheapest_offer.price if cheapest_offer else None
        max_price = most_expensive.price if most_expensive else None
        avg_price = round(sum(o.price for o in priced) / len(priced), 2) if priced else None
        max_savings = round(max_price - min_price, 2) if min_price and max_price else 0.0
        savings_pct = (
            round((max_savings / max_price) * 100, 1)
            if (max_price and max_price > 0 and max_savings > 0)
            else 0.0
        )

        stores = [
            min(offers_list, key=lambda o: (o["price"] is None, o["price"] or 999999))
            for offers_list in by_retailer.values()
        ]
        stores.sort(key=lambda s: (s["price"] is None, s["price"] or 999999))

        cheapest_dict = (
            {"retailer": cheapest_offer.retailer, "price": cheapest_offer.price, "title": cheapest_offer.title}
            if cheapest_offer
            else None
        )

        return {
            "query": query,
            "total_matches": len(matching),
            "stores_count": len(by_retailer),
            "cheapest_store": cheapest_dict,
            "cheapest_store_name": cheapest_offer.retailer if cheapest_offer else None,
            "cheapest_price": min_price,
            "highest_price": max_price,
            "average_price": avg_price,
            "potential_savings": max_savings,
            "max_savings": max_savings,
            "savings_percent": savings_pct,
            "by_retailer": by_retailer,
            "stores": stores,
        }

    def match_watchlist_deals(self, items: list[dict], offers: list[Offer]) -> dict:
        matches = []
        for item in items:
            item_id = item.get("id") or ""
            name = (item.get("name") or item.get("keyword") or "").strip()
            if not name:
                continue
            raw_target = item.get("target_price")
            target_price = None
            if raw_target is not None:
                try:
                    target_price = float(raw_target)
                except (ValueError, TypeError):
                    target_price = None

            tokens = [t.casefold() for t in WHITESPACE_REGEX.split(name) if len(t) >= 2] or [name.casefold()]

            item_matches = []
            for offer in offers:
                haystack = f"{offer.title} {offer.brand or ''} {offer.description}".casefold()
                if all(tok in haystack for tok in tokens):
                    is_hit = bool(target_price is not None and offer.price is not None and offer.price <= target_price)
                    item_matches.append(
                        {
                            "id": offer.id,
                            "retailer": offer.retailer,
                            "retailer_slug": offer.retailer_slug,
                            "title": offer.title,
                            "price": offer.price,
                            "old_price": offer.old_price,
                            "discount_percent": offer.discount_percent,
                            "unit_price": offer.unit_price,
                            "unit": offer.unit,
                            "valid_from": offer.valid_from,
                            "valid_to": offer.valid_to,
                            "source_url": offer.source_url,
                            "is_target_hit": is_hit,
                        }
                    )

            item_matches.sort(key=lambda m: (m["price"] is None, m["price"] or 999999))
            matches.append(
                {
                    "id": item_id,
                    "name": name,
                    "keyword": name,
                    "target_price": target_price,
                    "deals_count": len(item_matches),
                    "best_deal": item_matches[0] if item_matches else None,
                    "all_deals": item_matches[:5],
                    "matches": item_matches[:5],
                }
            )

        return {
            "items": matches,
            "results": matches,
            "total_active_alerts": sum(1 for m in matches if m["deals_count"] > 0),
        }

    def seed_initial_history(self) -> int:
        if self.db_path in self._SEEDED_DBS and self.db_path.exists():
            return 0

        with self._get_connection() as conn:
            cursor = conn.execute("SELECT COUNT(*) as count FROM price_history")
            count = cursor.fetchone()["count"]
            if count > 50:
                self._SEEDED_DBS.add(self.db_path)
                return 0

        imported = 0
        now = datetime.now(UTC)
        past_week = (now - timedelta(days=7)).isoformat(timespec="seconds")
        current_iso = now.isoformat(timespec="seconds")

        for filename in ("offers.json", "drugstore_offers.json"):
            file_path = self.cache_dir / filename
            if not file_path.exists():
                continue
            try:
                with file_path.open("r", encoding="utf-8") as handle:
                    data = json.load(handle)
                offers_data = data.get("offers", [])
                zip_code = data.get("zip_code", "14195")
                offers = [Offer.from_dict(item) for item in offers_data]

                # Seed current week observations
                imported += self.record_offers(offers, zip_code=zip_code, recorded_at=current_iso)

                # Seed historical anchor points from old_price to build initial baseline trend
                past_offers: list[Offer] = [
                    Offer(
                        id=f"hist-{offer.id}",
                        source_offer_id=offer.source_offer_id,
                        retailer=offer.retailer,
                        retailer_slug=offer.retailer_slug,
                        title=offer.title,
                        brand=offer.brand,
                        category=offer.category,
                        price=offer.old_price,
                        old_price=None,
                        unit_price=None,
                        unit=offer.unit,
                        discount_percent=None,
                        description=offer.description,
                        valid_from=None,
                        valid_to=None,
                        image_url=offer.image_url,
                        source_url=offer.source_url,
                        product_key=offer.product_key,
                    )
                    for offer in offers
                    if offer.old_price and offer.old_price > 0
                ]
                if past_offers:
                    imported += self.record_offers(past_offers, zip_code=zip_code, recorded_at=past_week)
            except Exception:
                continue

        if imported > 0:
            self._SEEDED_DBS.add(self.db_path)
        return imported


def build_weekly_competitor_map(offers: list[Offer]) -> dict[str, list[dict]]:
    """
    Finds competing offers for the same product at other retailers in the same week.
    Returns a dictionary mapping offer.id -> list of competitor dicts sorted by price.
    """
    by_product_key: dict[str, list[Offer]] = defaultdict(list)
    for o in offers:
        pkey = o.product_key or normalize_product_key(o.title, o.brand)
        o.product_key = pkey
        by_product_key[pkey].append(o)

    competitor_map: dict[str, list[dict]] = defaultdict(list)
    for p_offers in by_product_key.values():
        distinct_retailers = {o.retailer_slug for o in p_offers}
        if len(distinct_retailers) <= 1:
            continue

        for current in p_offers:
            comp_list = []
            for other in p_offers:
                if other.retailer_slug == current.retailer_slug:
                    continue
                diff = None
                if current.price is not None and other.price is not None:
                    diff = round(current.price - other.price, 2)
                comp_list.append(
                    {
                        "id": other.id,
                        "retailer": other.retailer,
                        "retailer_slug": other.retailer_slug,
                        "title": other.title,
                        "price": other.price,
                        "old_price": other.old_price,
                        "unit_price": other.unit_price,
                        "unit": other.unit,
                        "discount_percent": other.discount_percent,
                        "source_url": other.source_url,
                        "price_diff": diff,
                        "scope_type": other.scope_type,
                        "store_name": other.store_name,
                        "region_verified": other.region_verified,
                    }
                )
            comp_list.sort(key=lambda c: (c["price"] is None, c["price"] or 999999))
            competitor_map[current.id] = comp_list

    return competitor_map


def enrich_weekly_competitors(offers: list[Offer]) -> list[Offer]:
    """
    Populates product_key, competitors, cheapest_competitor, and is_cheapest_in_week
    on a list of weekly offers in place and returns the list.
    """
    competitor_map = build_weekly_competitor_map(offers)
    for offer in offers:
        comps = competitor_map.get(offer.id, [])
        offer.competitors = comps
        priced_comps = [c for c in comps if c["price"] is not None]
        offer.cheapest_competitor = priced_comps[0] if priced_comps else None
        is_cheapest = False
        if comps and offer.price is not None:
            is_cheapest = all(c["price"] is None or offer.price <= (c["price"] + 0.001) for c in comps)
        offer.is_cheapest_in_week = is_cheapest
    return offers
