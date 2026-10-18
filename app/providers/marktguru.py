from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from bs4 import BeautifulSoup
from requests import Session

from datetime import date
from types import MappingProxyType

from app.models import Offer
from app.providers.base import OfferProvider, compute_discount_percent, parse_positive_float
from app.services.categories import categorize_offer

NO_BRAND_NAME = "thisisnobrand123"


# ===========================================================================
# Authoritative Marktguru Regional Coverage Registry
# ===========================================================================
# Source:
#   Documented publisher delivery zones and regional advertising contract definitions
#   verified against 5-digit German postal code boundaries (Postleitzahlen).
#
# Limitations:
#   1. Retailers and distributors frequently adjust flyer distribution boundaries per
#      promotional campaign without updating external metadata.
#   2. Broad geographic heuristics—such as two-digit PLZ prefixes, city name substring
#      matches, or distance/radius thresholds (e.g. <= 60km)—are explicitly REJECTED
#      as proof of promotional coverage. Supermarket advertising zones often exclude
#      border towns or split metropolitan areas across different cooperative regions
#      (e.g., EDEKA Minden-Hannover vs. EDEKA Nord). A nearby location does NOT prove
#      an offer applies to a requested PLZ.
#   3. Therefore, this mapping is restricted to explicitly verified distribution zone keys
#      with confirmed 5-digit PLZ sets. Any unlisted region or campaign lacking explicit
#      source-provided postal codes fails closed (region_verified=False).
from pathlib import Path


@dataclass(frozen=True, slots=True)
class RegionalCoverageEvidence:
    region_key: str
    source_url: str
    evidence_type: str
    verification_date: str
    covered_plzs: set[str] | frozenset[str]
    limitations: str
    evidence_artifact_path: str | Path | None = None
    promotional_applicability_confirmed: bool = False


@dataclass(frozen=True, slots=True)
class NationwideCoverageEvidence:
    campaign_id: str
    source_url: str
    evidence_type: str
    verification_date: str
    scope: str
    promotional_applicability_confirmed: bool
    limitations: str
    evidence_artifact_path: str | Path | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "campaign_id": self.campaign_id,
            "source_url": self.source_url,
            "evidence_type": self.evidence_type,
            "verification_date": self.verification_date,
            "scope": self.scope,
            "promotional_applicability_confirmed": self.promotional_applicability_confirmed,
            "limitations": self.limitations,
            "evidence_artifact_path": str(self.evidence_artifact_path) if self.evidence_artifact_path else None,
        }


def validate_regional_coverage_evidence(
    evidence: RegionalCoverageEvidence | None,
) -> tuple[bool, str]:
    """
    Validates that a RegionalCoverageEvidence entry is backed by an accessible,
    verifiable artifact confirming promotional campaign applicability and exact
    5-digit German PLZ coverage. Plausible URLs, non-empty strings, or generic
    geographic boundaries alone are rejected.
    """
    from app.services.location import get_coordinates_for_plz

    if not isinstance(evidence, RegionalCoverageEvidence):
        return False, "Missing RegionalCoverageEvidence instance"
    if not evidence.region_key or not str(evidence.region_key).strip():
        return False, "Missing region_key"
    if not evidence.source_url or not str(evidence.source_url).strip().startswith(("http://", "https://")):
        return False, "Invalid or missing source_url"
    if not evidence.verification_date or not re.match(r"^\d{4}-\d{2}-\d{2}$", str(evidence.verification_date).strip()):
        return False, "Invalid or missing verification_date"
    if not evidence.limitations or not str(evidence.limitations).strip():
        return False, "Missing limitations documentation"
    if evidence.promotional_applicability_confirmed is not True:
        return False, "Promotional campaign applicability is not confirmed (generic geography rejected)"
    if evidence.evidence_type != "verified_campaign_distribution_manifest":
        return False, f"Unsupported evidence_type '{evidence.evidence_type}'"
    if not evidence.covered_plzs:
        return False, "Empty covered_plzs set"

    normalized_plzs: set[str] = set()
    for plz in evidence.covered_plzs:
        s_plz = str(plz).strip()
        if len(s_plz) != 5 or not s_plz.isdigit() or get_coordinates_for_plz(s_plz) is None:
            return False, f"Invalid or unrecognized 5-digit German PLZ '{plz}'"
        normalized_plzs.add(s_plz)

    if not evidence.evidence_artifact_path:
        return False, "Remote source_url cannot establish coverage without an accessible recorded evidence artifact"

    artifact_path = Path(evidence.evidence_artifact_path)
    if not artifact_path.is_file():
        return False, f"Recorded evidence artifact '{artifact_path}' does not exist"

    try:
        artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return False, f"Failed to read recorded evidence artifact: {exc}"

    if not isinstance(artifact, dict):
        return False, "Recorded evidence artifact must be a JSON object"
    if str(artifact.get("region_key") or "").strip().casefold() != evidence.region_key.strip().casefold():
        return False, "Artifact region_key mismatches evidence region_key"
    if str(artifact.get("source_url") or "").strip() != evidence.source_url.strip():
        return False, "Artifact source_url mismatches evidence source_url"
    if artifact.get("scope_meaning") != "promotional_campaign_applicability":
        return False, "Artifact does not confirm promotional_campaign_applicability"
    if not str(artifact.get("campaign_id") or "").strip():
        return False, "Artifact lacks a verifiable campaign_id"

    artifact_plzs_raw = artifact.get("covered_plzs")
    if not isinstance(artifact_plzs_raw, list) or not artifact_plzs_raw:
        return False, "Artifact lacks covered_plzs list"
    artifact_plzs = {str(p).strip() for p in artifact_plzs_raw if p is not None}
    if artifact_plzs != normalized_plzs:
        return False, "Artifact covered_plzs does not match claimed covered_plzs"

    return True, "Verified promotional campaign distribution manifest"


# ===========================================================================
# Audited Authoritative Regional Coverage Registry
#
# Audit Status:
#   Marktguru's public API does not expose an authoritative static distribution-zone
#   manifest mapping region keys (such as 'berlin_north') to verified 5-digit PLZ
#   promotional coverage. Per fail-closed integrity rules, no unsubstantiated
#   mappings are registered here. Any entry added in the future MUST pass
#   `validate_regional_coverage_evidence()` with a recorded manifest artifact.
# ===========================================================================
AUTHORITATIVE_REGION_REGISTRY: dict[str, RegionalCoverageEvidence] = {}

# Derived mapping containing only validated registry entries
AUTHORITATIVE_REGION_COVERAGE_MAP: dict[str, set[str]] = {
    k: set(v.covered_plzs)
    for k, v in AUTHORITATIVE_REGION_REGISTRY.items()
    if validate_regional_coverage_evidence(v)[0]
}


VALID_EVIDENCE_TYPES: frozenset[str] = frozenset(
    {
        "verified_nationwide_distribution_manifest",
        "verified_campaign_distribution_manifest",
        "official_national_campaign_distribution",
    }
)


def validate_nationwide_coverage_evidence(
    campaign_id: str,
    evidence: Any,
) -> tuple[bool, str, dict[str, Any] | None]:
    """
    Validates explicit, scope-specific nationwide coverage evidence for Germany.
    Rejects missing, empty, generic, contradictory, or unsupported evidence.
    """
    if not campaign_id or not str(campaign_id).strip():
        return False, "Missing campaign_id", None

    clean_id = str(campaign_id).strip()
    norm_id = clean_id.casefold()
    clean_core_id = norm_id.removeprefix("national_flyer:").removeprefix("national_campaign:")

    if evidence is None:
        return False, "Missing nationwide coverage evidence; no default evidence permitted", None

    if isinstance(evidence, NationwideCoverageEvidence):
        ev_dict = evidence.to_dict()
    elif isinstance(evidence, dict):
        ev_dict = dict(evidence)
    else:
        return False, "Evidence must be an evidence dictionary or NationwideCoverageEvidence instance", None

    if not ev_dict:
        return False, "Empty evidence dictionary cannot establish nationwide applicability", None

    ev_campaign_id = str(ev_dict.get("campaign_id") or "").strip().casefold()
    ev_core_id = ev_campaign_id.removeprefix("national_flyer:").removeprefix("national_campaign:")
    if ev_core_id and ev_core_id != clean_core_id:
        return False, f"Evidence campaign_id '{ev_campaign_id}' does not match claimed campaign '{clean_id}'", None

    source_url = str(ev_dict.get("source_url") or "").strip()
    if not source_url or not (source_url.startswith("http://") or source_url.startswith("https://")):
        return False, "Missing or invalid source_url in nationwide evidence", None

    evidence_type = str(ev_dict.get("evidence_type") or "").strip()
    if evidence_type not in ("verified_nationwide_distribution_manifest", "official_national_campaign_distribution"):
        return False, f"Unsupported nationwide evidence_type '{evidence_type}'", None

    verification_date = str(ev_dict.get("verification_date") or "").strip()
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", verification_date):
        return False, "Invalid or missing verification_date in nationwide evidence (expected YYYY-MM-DD)", None

    scope = str(ev_dict.get("scope") or "").strip().casefold()
    if scope != "nationwide":
        return False, f"Evidence scope must be 'nationwide', got '{scope}'", None

    if ev_dict.get("promotional_applicability_confirmed") is not True:
        return False, "promotional_applicability_confirmed must be strictly True in nationwide evidence", None

    limitations = str(ev_dict.get("limitations") or "").strip()
    if not limitations:
        return False, "Missing limitations documentation in nationwide evidence", None

    # Contradiction checks: cannot contain regional restrictions or postal code bounds
    if ev_dict.get("region_id") or ev_dict.get("regionId") or ev_dict.get("postalCodes") or ev_dict.get("validZipCodes"):
        return False, "Contradictory regional restriction fields found in nationwide evidence", None

    artifact_path = ev_dict.get("evidence_artifact_path")
    if artifact_path:
        p = Path(artifact_path)
        if not p.is_file():
            return False, f"Recorded nationwide evidence artifact '{artifact_path}' does not exist", None
        try:
            art_data = json.loads(p.read_text(encoding="utf-8"))
            if not isinstance(art_data, dict):
                return False, "Recorded evidence artifact must be a JSON object", None
            if str(art_data.get("scope") or "").strip().casefold() != "nationwide":
                return False, "Artifact does not confirm nationwide scope", None
            if art_data.get("promotional_campaign_applicability") is not True:
                return False, "Artifact does not confirm promotional_campaign_applicability", None
        except Exception as exc:
            return False, f"Failed to read recorded nationwide evidence artifact: {exc}", None

    validated = {
        "campaign_id": clean_id,
        "source_url": source_url,
        "evidence_type": evidence_type,
        "verification_date": verification_date,
        "scope": "nationwide",
        "promotional_applicability_confirmed": True,
        "limitations": limitations,
        "evidence_artifact_path": str(artifact_path) if artifact_path else None,
    }
    return True, "Verified nationwide campaign evidence", validated


def validate_regional_campaign_evidence(
    campaign_id: str,
    covered_plzs: Any,
    evidence: Any,
) -> tuple[bool, str, dict[str, Any] | None]:
    """
    Validates explicit, scope-specific regional coverage evidence.
    Rejects missing, empty, generic, or unsupported evidence.
    """
    from app.services.location import get_coordinates_for_plz

    if not campaign_id or not str(campaign_id).strip():
        return False, "Missing campaign_id", None

    clean_id = str(campaign_id).strip()
    norm_id = clean_id.casefold()
    clean_core_id = norm_id.removeprefix("flyer:").removeprefix("campaign:")

    if not isinstance(covered_plzs, (set, frozenset, list, tuple)) or not covered_plzs:
        return False, "Regional registration requires a non-empty set of covered 5-digit PLZs", None

    normalized_plzs: set[str] = set()
    for p in covered_plzs:
        s_p = str(p).strip()
        if len(s_p) != 5 or not s_p.isdigit() or get_coordinates_for_plz(s_p) is None:
            return False, f"Invalid or unrecognized 5-digit German PLZ '{p}'", None
        normalized_plzs.add(s_p)

    if evidence is None:
        return False, "Missing regional campaign coverage evidence; registration rejected", None

    if isinstance(evidence, RegionalCoverageEvidence):
        ev_dict = {
            "campaign_id": clean_id,
            "region_key": evidence.region_key,
            "source_url": evidence.source_url,
            "evidence_type": evidence.evidence_type,
            "verification_date": evidence.verification_date,
            "scope": "region",
            "promotional_applicability_confirmed": evidence.promotional_applicability_confirmed,
            "covered_plzs": list(evidence.covered_plzs),
            "limitations": evidence.limitations,
            "evidence_artifact_path": evidence.evidence_artifact_path,
        }
    elif isinstance(evidence, dict):
        ev_dict = dict(evidence)
    else:
        return False, "Evidence must be an evidence dictionary or RegionalCoverageEvidence instance", None

    if not ev_dict:
        return False, "Empty evidence dictionary cannot establish regional applicability", None

    ev_campaign_id = str(ev_dict.get("campaign_id") or "").strip().casefold()
    ev_core_id = ev_campaign_id.removeprefix("flyer:").removeprefix("campaign:")
    if ev_core_id and ev_core_id != clean_core_id:
        return False, f"Evidence campaign_id '{ev_campaign_id}' does not match claimed campaign '{clean_id}'", None

    source_url = str(ev_dict.get("source_url") or "").strip()
    if not source_url or not (source_url.startswith("http://") or source_url.startswith("https://")):
        return False, "Missing or invalid source_url in regional evidence", None

    evidence_type = str(ev_dict.get("evidence_type") or "").strip()
    if evidence_type not in ("verified_campaign_distribution_manifest", "verified_nationwide_distribution_manifest"):
        return False, f"Unsupported regional evidence_type '{evidence_type}'", None

    verification_date = str(ev_dict.get("verification_date") or "").strip()
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", verification_date):
        return False, "Invalid or missing verification_date in regional evidence (expected YYYY-MM-DD)", None

    if ev_dict.get("promotional_applicability_confirmed") is not True:
        return False, "promotional_applicability_confirmed must be strictly True in regional evidence", None

    limitations = str(ev_dict.get("limitations") or "").strip()
    if not limitations:
        return False, "Missing limitations documentation in regional evidence", None

    ev_plzs_raw = ev_dict.get("covered_plzs") or ev_dict.get("validZipCodes")
    if ev_plzs_raw is not None:
        ev_plzs = {str(p).strip() for p in ev_plzs_raw}
        if ev_plzs != normalized_plzs:
            return False, "Evidence covered_plzs does not match claimed covered_plzs set", None

    artifact_path = ev_dict.get("evidence_artifact_path")
    if artifact_path:
        p = Path(artifact_path)
        if not p.is_file():
            return False, f"Recorded regional evidence artifact '{artifact_path}' does not exist", None
        try:
            art_data = json.loads(p.read_text(encoding="utf-8"))
            if not isinstance(art_data, dict):
                return False, "Recorded evidence artifact must be a JSON object", None
            if art_data.get("scope_meaning") != "promotional_campaign_applicability":
                return False, "Artifact does not confirm promotional_campaign_applicability", None
            art_plzs = {str(p).strip() for p in art_data.get("covered_plzs", [])}
            if art_plzs != normalized_plzs:
                return False, "Artifact covered_plzs does not match claimed covered_plzs", None
        except Exception as exc:
            return False, f"Failed to read recorded regional evidence artifact: {exc}", None

    validated = {
        "campaign_id": clean_id,
        "source_url": source_url,
        "evidence_type": evidence_type,
        "verification_date": verification_date,
        "scope": "region",
        "promotional_applicability_confirmed": True,
        "covered_plzs": sorted(normalized_plzs),
        "limitations": limitations,
        "evidence_artifact_path": str(artifact_path) if artifact_path else None,
    }
    return True, "Verified regional campaign evidence", validated


# Private internal registries
_VERIFIED_CAMPAIGN_REGISTRY: dict[str, dict[str, Any]] = {}
_VERIFIED_NATIONWIDE_REGISTRY: dict[str, dict[str, Any]] = {}

# Read-only public mappings preventing callers from bypassing validation
VERIFIED_CAMPAIGN_REGISTRY = MappingProxyType(_VERIFIED_CAMPAIGN_REGISTRY)
VERIFIED_NATIONWIDE_REGISTRY = MappingProxyType(_VERIFIED_NATIONWIDE_REGISTRY)


def register_verified_campaign(
    campaign_id: str,
    covered_plzs: set[str] | frozenset[str] | list[str],
    evidence: dict[str, Any] | RegionalCoverageEvidence | None = None,
) -> bool:
    """
    Registers substantiated promotional campaign coverage evidence for a campaign/flyer.
    Requires validated, scope-specific promotional campaign evidence. Returns True on success,
    False if evidence is missing or invalid.
    """
    is_valid, reason, validated = validate_regional_campaign_evidence(campaign_id, covered_plzs, evidence)
    if not is_valid or not validated:
        return False

    norm_id = str(campaign_id).strip().casefold()
    clean_id = norm_id.removeprefix("flyer:").removeprefix("campaign:")
    _VERIFIED_CAMPAIGN_REGISTRY[norm_id] = validated
    _VERIFIED_CAMPAIGN_REGISTRY[clean_id] = validated
    _VERIFIED_CAMPAIGN_REGISTRY[f"flyer:{clean_id}"] = validated
    return True


def register_verified_nationwide_campaign(
    campaign_id: str,
    evidence: dict[str, Any] | NationwideCoverageEvidence | None = None,
) -> bool:
    """
    Registers substantiated scope-specific nationwide coverage evidence for a campaign/flyer.
    Rejects registration without explicit, validated nationwide evidence. Returns True on success,
    False if evidence is missing or invalid.
    """
    is_valid, reason, validated = validate_nationwide_coverage_evidence(campaign_id, evidence)
    if not is_valid or not validated:
        return False

    norm_id = str(campaign_id).strip().casefold()
    clean_id = norm_id.removeprefix("national_flyer:").removeprefix("national_campaign:")
    _VERIFIED_NATIONWIDE_REGISTRY[norm_id] = validated
    _VERIFIED_NATIONWIDE_REGISTRY[clean_id] = validated
    _VERIFIED_NATIONWIDE_REGISTRY[f"national_flyer:{clean_id}"] = validated
    return True


def get_verified_campaign_plzs(campaign_or_region_id: str | None) -> set[str] | None:
    """
    Returns verified covered 5-digit PLZ set if the campaign or region has verified
    underlying campaign evidence establishing promotional applicability.
    """
    if not campaign_or_region_id:
        return None
    key = str(campaign_or_region_id).strip()
    key_lower = key.casefold()

    # 1. Authoritative region registry / coverage map
    if key_lower in AUTHORITATIVE_REGION_COVERAGE_MAP:
        return AUTHORITATIVE_REGION_COVERAGE_MAP[key_lower]
    if key_lower in AUTHORITATIVE_REGION_REGISTRY:
        ev = AUTHORITATIVE_REGION_REGISTRY[key_lower]
        is_valid, _ = validate_regional_coverage_evidence(ev)
        if is_valid:
            return set(ev.covered_plzs)

    # 2. Verified campaign distribution manifest registry
    clean_id = key_lower.removeprefix("flyer:").removeprefix("campaign:")
    for lookup in (key, key_lower, clean_id):
        if lookup in _VERIFIED_CAMPAIGN_REGISTRY:
            ev_data = _VERIFIED_CAMPAIGN_REGISTRY[lookup]
            if isinstance(ev_data, dict) and "covered_plzs" in ev_data:
                return set(ev_data["covered_plzs"])

    return None


def is_verified_nationwide_campaign(campaign_or_flyer_id: str | None) -> bool:
    """
    Returns True only if the nationwide campaign has verifiable scope-specific evidence
    establishing promotional campaign applicability across Germany.
    """
    if not campaign_or_flyer_id:
        return False
    key = str(campaign_or_flyer_id).strip().casefold()
    clean_id = key.removeprefix("national_flyer:").removeprefix("national_campaign:")
    return key in _VERIFIED_NATIONWIDE_REGISTRY or clean_id in _VERIFIED_NATIONWIDE_REGISTRY


def get_verified_nationwide_evidence(campaign_id: str | None) -> dict[str, Any] | None:
    """
    Returns validated nationwide evidence dict if registered.
    """
    if not campaign_id:
        return None
    key = str(campaign_id).strip().casefold()
    clean_id = key.removeprefix("national_flyer:").removeprefix("national_campaign:")
    data = _VERIFIED_NATIONWIDE_REGISTRY.get(key) or _VERIFIED_NATIONWIDE_REGISTRY.get(clean_id)
    return dict(data) if data else None


def get_verified_campaign_evidence(campaign_id: str | None) -> dict[str, Any] | None:
    """
    Returns validated regional campaign evidence dict if registered.
    """
    if not campaign_id:
        return None
    key = str(campaign_id).strip().casefold()
    clean_id = key.removeprefix("flyer:").removeprefix("campaign:")
    data = _VERIFIED_CAMPAIGN_REGISTRY.get(key) or _VERIFIED_CAMPAIGN_REGISTRY.get(clean_id)
    return dict(data) if data else None


def clear_campaign_registries() -> None:
    """
    Clears in-memory dynamic campaign registries for test isolation and restart simulation.
    """
    _VERIFIED_CAMPAIGN_REGISTRY.clear()
    _VERIFIED_NATIONWIDE_REGISTRY.clear()


def _extract_verified_campaign_plzs(flyer_data: dict, flyer_id: str | None) -> tuple[set[str] | None, str | None]:
    """
    Distinguishes verified promotional campaign PLZ applicability from generic
    geographic / postal metadata (`postalCodes`, `zipCodes`, `coveragePostalCodes`,
    `region.postalCodes`).
    Returns (verified_plz_set, rejection_or_match_note).
    """
    from app.services.location import get_coordinates_for_plz

    if not flyer_id:
        return None, "Leaflet lacks campaign/flyer identifier"

    # Check for an explicit campaignDistribution manifest object on the flyer
    campaign_dist = flyer_data.get("campaignDistribution")
    raw_valid_zips = None
    scope_meaning = None

    if isinstance(campaign_dist, dict):
        scope_meaning = campaign_dist.get("scope") or campaign_dist.get("scopeMeaning")
        raw_valid_zips = campaign_dist.get("validZipCodes")
    else:
        scope_meaning = flyer_data.get("coverageScope") or flyer_data.get("postalCodeScope")
        raw_valid_zips = flyer_data.get("validZipCodes")

    if raw_valid_zips is None:
        return None, None

    if scope_meaning != "promotional_campaign":
        return None, f"validZipCodes scope '{scope_meaning or 'unspecified'}' does not confirm promotional campaign applicability"

    if not isinstance(raw_valid_zips, (list, tuple, set)) or not raw_valid_zips:
        return None, "validZipCodes is empty or malformed"

    verified_plzs: set[str] = set()
    for p in raw_valid_zips:
        s_plz = str(p).strip()
        if len(s_plz) != 5 or not s_plz.isdigit() or get_coordinates_for_plz(s_plz) is None:
            return None, f"validZipCodes contains invalid or non-5-digit PLZ '{p}'"
        verified_plzs.add(s_plz)

    return verified_plzs, None


def verify_marktguru_regional_applicability(
    zip_code: str | None,
    flyer_data: dict,
    item: dict,
    registry: dict[str, RegionalCoverageEvidence] | None = None,
) -> tuple[bool, str, str, str | None]:
    """
    Verifies whether a Marktguru regional leaflet actually covers the requested PLZ
    for a promotional campaign.
    Evidence rules:
      1. Explicit campaign distribution manifest (`validZipCodes` with confirmed
         `promotional_campaign` scope and valid flyer/campaign ID).
      2. Registered mapping in `AUTHORITATIVE_REGION_REGISTRY` that passes
         `validate_regional_coverage_evidence()` with a recorded manifest artifact.
      3. Generic postal code lists (`postalCodes`, `zipCodes`, `coveragePostalCodes`,
         `region.postalCodes`, or unconfirmed `validZipCodes`) are treated strictly
         as diagnostic geographic metadata (`region_verified=False`).
      4. Heuristics (city names, 2-digit PLZ prefixes, distance thresholds) fail closed.
    Returns: (region_verified, verification_method, verification_reason, source_region_id)
    """
    from app.services.location import get_coordinates_for_plz, resolve_plz_city

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
    source_region_id = str(raw_region_id).strip() if raw_region_id is not None and str(raw_region_id).strip() else None
    raw_flyer_id = flyer_data.get("id") or flyer_data.get("campaignId") or item.get("flyerId")
    flyer_id = str(raw_flyer_id).strip() if raw_flyer_id is not None and str(raw_flyer_id).strip() else None

    cleaned_zip = (zip_code or "").strip()
    has_valid_zip = bool(len(cleaned_zip) == 5 and cleaned_zip.isdigit() and get_coordinates_for_plz(cleaned_zip) is not None)

    # 1. Verified promotional campaign PLZ manifest on the leaflet
    verified_campaign_plzs, campaign_note = _extract_verified_campaign_plzs(flyer_data, flyer_id)
    if verified_campaign_plzs is not None:
        eff_region_id = source_region_id or f"flyer:{flyer_id}"
        if not has_valid_zip:
            return False, "marktguru_unverified_flyer", "No valid 5-digit PLZ provided to verify leaflet campaign coverage", eff_region_id
        if cleaned_zip in verified_campaign_plzs:
            campaign_evidence = {
                "campaign_id": str(flyer_id or eff_region_id),
                "source_url": flyer_data.get("url") or f"https://www.marktguru.de/leaflets/{flyer_id}",
                "evidence_type": "verified_campaign_distribution_manifest",
                "verification_date": date.today().isoformat(),
                "scope": "region",
                "promotional_applicability_confirmed": True,
                "covered_plzs": list(verified_campaign_plzs),
                "limitations": "Upstream leaflet campaignDistribution manifest confirmed promotional campaign applicability",
            }
            register_verified_campaign(eff_region_id, verified_campaign_plzs, campaign_evidence)
            if flyer_id:
                register_verified_campaign(str(flyer_id), verified_campaign_plzs, campaign_evidence)
            return (
                True,
                "marktguru_regional_flyer_postal_match",
                f"Marktguru promotional campaign leaflet {flyer_id} verified to cover PLZ {cleaned_zip}",
                eff_region_id,
            )
        return (
            False,
            "marktguru_unverified_flyer",
            f"Marktguru promotional campaign leaflet {flyer_id} postal codes do not cover requested PLZ {cleaned_zip}",
            eff_region_id,
        )

    # 2. Audited authoritative region registry (requires validated evidence artifact)
    region_name = (
        flyer_data.get("regionName")
        or flyer_data.get("geographicRegionName")
        or region_data.get("name")
        or region_data.get("title")
    )
    region_key = (
        str(raw_region_id).strip().casefold()
        if raw_region_id is not None
        else (str(region_name).strip().casefold() if region_name else None)
    )
    active_registry = registry if registry is not None else AUTHORITATIVE_REGION_REGISTRY

    if region_key and region_key in active_registry:
        evidence = active_registry[region_key]
        is_valid_ev, ev_reason = validate_regional_coverage_evidence(evidence)
        eff_region_id = source_region_id or region_key
        if not is_valid_ev:
            return (
                False,
                "marktguru_unverified_flyer",
                f"Region '{region_key}' mapping rejected because evidence is invalid: {ev_reason}",
                eff_region_id,
            )
        if not has_valid_zip:
            return False, "marktguru_unverified_flyer", "No valid 5-digit PLZ provided to verify regional mapping", eff_region_id
        if cleaned_zip in evidence.covered_plzs:
            return (
                True,
                "marktguru_authoritative_region_mapping",
                f"Source region '{region_key}' authoritatively mapped via verified manifest to cover PLZ {cleaned_zip}",
                eff_region_id,
            )
        return (
            False,
            "marktguru_unverified_flyer",
            f"Source region '{region_key}' authoritative coverage does not include PLZ {cleaned_zip}",
            eff_region_id,
        )

    # 3. Generic / diagnostic postal-code lists (postalCodes, zipCodes, coveragePostalCodes, region.postalCodes, unconfirmed validZipCodes)
    generic_pcs = (
        flyer_data.get("postalCodes")
        or flyer_data.get("zipCodes")
        or flyer_data.get("coveragePostalCodes")
        or flyer_data.get("validZipCodes")
        or region_data.get("postalCodes")
        or region_data.get("zipCodes")
        or item.get("postalCodes")
    )
    if isinstance(generic_pcs, (list, tuple, set)) and generic_pcs:
        pcs = {str(p).strip() for p in generic_pcs if p}
        if not has_valid_zip:
            return False, "marktguru_unverified_flyer", "No valid PLZ provided to check leaflet postal metadata", source_region_id
        if cleaned_zip in pcs:
            detail = f" ({campaign_note})" if campaign_note else ""
            return (
                False,
                "marktguru_diagnostic_region_match",
                f"Diagnostic PLZ match for {cleaned_zip} in generic postal metadata; promotional campaign applicability is not verified{detail}",
                source_region_id,
            )
        return (
            False,
            "marktguru_unverified_flyer",
            f"Marktguru leaflet postal codes do not cover requested PLZ {cleaned_zip}",
            source_region_id,
        )

    # 4. Detect mismatched region against requested PLZ for diagnostic clarity
    if cleaned_zip and (region_name or (raw_region_id and not str(raw_region_id).isdigit())):
        city_name = resolve_plz_city(cleaned_zip)
        norm_city = city_name.strip().casefold().replace("ü", "ue").replace("ö", "oe").replace("ä", "ae").replace("ß", "ss")
        norm_r = str(region_name or raw_region_id).strip().casefold().replace("ü", "ue").replace("ö", "oe").replace("ä", "ae").replace("ß", "ss")
        known_cities = (
            "berlin", "hamburg", "koeln", "cologne", "muenchen", "munich",
            "frankfurt", "stuttgart", "duesseldorf", "dortmund", "essen", "leipzig", "dresden"
        )
        if any(k in norm_r for k in known_cities) and (not norm_city or norm_city not in norm_r):
            return False, "marktguru_unverified_flyer", f"Marktguru region '{region_name or raw_region_id}' mismatches requested PLZ {cleaned_zip} ({city_name})", source_region_id

    # 5. Fail-closed: Heuristics rejected & missing authoritative mapping
    if raw_region_id is not None:
        return False, "marktguru_unverified_flyer", f"Marktguru regional leaflet '{raw_region_id}' lacks authoritative geographic coverage mapping for PLZ {zip_code}", source_region_id

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

        effective_retailer_slug = (retailer.get("uniqueName") or retailer_slug or "").strip().lower()
        clean_zip = (zip_code or "").strip()
        user_coords = get_coordinates_for_plz(clean_zip) if len(clean_zip) == 5 and clean_zip.isdigit() else None

        # Check if item or flyer explicitly defines national scope
        is_national = (
            flyer_data.get("isNational") is True
            or flyer_data.get("national") is True
            or item.get("isNational") is True
            or item.get("national") is True
            or flyer_data.get("scope") == "national"
            or item.get("scope") == "national"
        )
        has_regional_restriction = any(
            bool(d.get(k))
            for d in (flyer_data, item)
            if isinstance(d, dict)
            for k in (
                "regionId",
                "region",
                "regionName",
                "postalCodes",
                "zipCodes",
                "validZipCodes",
                "coveragePostalCodes",
                "plzList",
                "campaignDistribution",
            )
        )

        if branch_id is not None and str(branch_id).strip() != "":
            store_id = str(branch_id).strip()
            scope_type = "store"

            # Verify branch against requested zip_code
            branch_pc = str(branch_data.get("postalCode") or branch_data.get("zipCode") or "").strip()
            branch_dist = branch_data.get("distance")
            coords = branch_data.get("coordinates") if isinstance(branch_data.get("coordinates"), dict) else {}
            lat = coords.get("lat") or branch_data.get("latitude")
            lon = coords.get("lon") or branch_data.get("longitude")

            matched_geo = False
            pc_conflict = False
            if user_coords and branch_pc:
                if branch_pc == clean_zip:
                    matched_geo = True
                elif len(branch_pc) == 5 and branch_pc.isdigit():
                    b_coords = get_coordinates_for_plz(branch_pc)
                    if b_coords and haversine_distance_km(user_coords[0], user_coords[1], b_coords[0], b_coords[1]) <= 60.0:
                        matched_geo = True
                    else:
                        pc_conflict = True
                else:
                    pc_conflict = True

            if user_coords and not matched_geo and not pc_conflict:
                if isinstance(branch_dist, (int, float)) and not isinstance(branch_dist, bool) and 0 <= float(branch_dist) <= 60.0:
                    matched_geo = True
                elif is_valid_coordinates(lat, lon):
                    if haversine_distance_km(user_coords[0], user_coords[1], lat, lon) <= 60.0:
                        matched_geo = True

            if matched_geo:
                region_verified = True
                verification_method = "marktguru_store_branch_verified"
                verification_reason = f"Marktguru branch {store_id} geographically verified for PLZ {clean_zip}"
            else:
                region_verified = False
                verification_method = "marktguru_unverified_branch"
                verification_reason = f"Marktguru branch {store_id} lacks geographic verification for PLZ {zip_code}"

        elif is_national:
            clean_flyer_id = str(flyer_id).strip() if flyer_id is not None else ""
            is_territory_split_retailer = effective_retailer_slug in {"aldi", "aldi-nord", "aldi-sued"}
            scope_type = "nationwide"
            source_region_id = f"national_flyer:{clean_flyer_id}" if clean_flyer_id else None

            # Scope-specific evidence required for nationwide verification
            has_nationwide_evidence = bool(
                clean_flyer_id
                and not has_regional_restriction
                and not is_territory_split_retailer
                and is_verified_nationwide_campaign(source_region_id or clean_flyer_id)
            )

            if has_nationwide_evidence:
                region_verified = True
                verification_method = "marktguru_national_flyer"
                verification_reason = f"Marktguru leaflet {clean_flyer_id} verified with substantiated nationwide campaign evidence"
            else:
                region_verified = False
                verification_method = "marktguru_unverified_national"
                if not clean_flyer_id:
                    verification_reason = "Marktguru national claim lacks a traceable flyerId"
                elif has_regional_restriction:
                    verification_reason = f"Marktguru flyer {clean_flyer_id} claims national scope but contains contradictory regional restrictions"
                elif is_territory_split_retailer:
                    verification_reason = f"Retailer '{effective_retailer_slug}' is territory-split and cannot be verified as nationwide without regional proof"
                else:
                    verification_reason = f"Marktguru flyer {clean_flyer_id} claims national scope but lacks substantiated scope-specific nationwide evidence"

        elif flyer_id or flyer_data or has_regional_restriction:
            scope_type = "region"
            region_verified, verification_method, verification_reason, source_region_id = verify_marktguru_regional_applicability(
                zip_code, flyer_data, item
            )

        elif zip_code:
            scope_type = "unknown"
            region_verified = False
            verification_method = "unknown_scope"
            verification_reason = "Marktguru response lacks branch and leaflet geographic metadata"

        verification_evidence: dict[str, Any] | None = None
        if scope_type == "nationwide" and region_verified:
            verification_evidence = get_verified_nationwide_evidence(source_region_id or clean_flyer_id)
        elif scope_type == "region" and region_verified:
            verification_evidence = get_verified_campaign_evidence(source_region_id)

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
            verification_evidence=verification_evidence,
        )


def _image_url(source_offer_id: int, item: dict) -> str | None:
    images = item.get("images") or {}
    if images.get("count", 0) < 1 or not source_offer_id:
        return None
    return f"https://cdn.marktguru.de/api/v1/offers/{source_offer_id}/images/default/0/medium.webp"
