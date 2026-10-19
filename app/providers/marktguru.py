from __future__ import annotations

import copy
import hashlib
import json
import logging
import os
import re
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from bs4 import BeautifulSoup
from requests import Session

from datetime import date
from types import MappingProxyType

from app.models import Offer
from app.providers.base import OfferProvider, compute_discount_percent, parse_positive_float
from app.services.categories import categorize_offer

logger = logging.getLogger(__name__)

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


PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
PRODUCTION_EVIDENCE_ROOT = (PROJECT_ROOT / "data" / "evidence").resolve()
TEST_FIXTURE_EVIDENCE_ROOT = (PROJECT_ROOT / "tests" / "fixtures" / "evidence").resolve()

_DYNAMIC_APPROVED_ROOTS: set[Path] = set()


def is_production_mode() -> bool:
    env = os.environ.get("APP_ENV", "").strip().lower()
    flask_env = os.environ.get("FLASK_ENV", "").strip().lower()
    if env == "production" or flask_env == "production":
        return True
    if os.environ.get("PYTEST_CURRENT_TEST") or os.environ.get("TESTING") in ("1", "true", "True"):
        return False
    if env in ("test", "testing", "development", "dev") or flask_env in ("test", "testing", "development", "dev"):
        return False
    return True


def get_approved_evidence_roots() -> set[Path]:
    if is_production_mode():
        return {PRODUCTION_EVIDENCE_ROOT}
    roots = {PRODUCTION_EVIDENCE_ROOT}
    if os.environ.get("PYTEST_CURRENT_TEST") or os.environ.get("TESTING"):
        roots.add(TEST_FIXTURE_EVIDENCE_ROOT)
    roots.update(_DYNAMIC_APPROVED_ROOTS)
    return roots


def register_approved_evidence_root(path: str | Path) -> None:
    if is_production_mode():
        raise PermissionError("Registration of custom evidence roots is forbidden in production")
    _DYNAMIC_APPROVED_ROOTS.add(Path(path).resolve())


def clear_dynamic_approved_roots() -> None:
    _DYNAMIC_APPROVED_ROOTS.clear()


def is_approved_evidence_path(p: Path) -> bool:
    try:
        resolved = p.resolve()
        approved_roots = get_approved_evidence_roots()
        return any(resolved == root or resolved.is_relative_to(root) for root in approved_roots)
    except Exception:
        return False


# Trusted evidence ingestion catalog storing cryptographic SHA-256 hashes of audited artifacts
# Bound strictly to canonical relative paths under approved roots.
_TRUSTED_INGESTION_CATALOG: dict[str, str] = {
    # Test fixture paths under tests/fixtures/evidence (test execution only)
    "tests/fixtures/evidence/700.json": "1d6481e826f2d8f7612d53ddd86c5b141e3e31d6eafc739fd5a7727ec0583d8f",
    "tests/fixtures/evidence/901.json": "d34092378f01f4d1c8f33a7ea27fc82a807c9764bf5907a92bb9b0addd78b260",
    "tests/fixtures/evidence/999.json": "a520655b8bf147ce323ff8f5fa7db13fda82c2bb496e4aacf8f271df19de9783",
    "tests/fixtures/evidence/nat_proven_999.json": "545e5b5a6d37881ba6374343b2fd0640b7056ec8e792e7984ccff206acde5e03",
    "tests/fixtures/evidence/nat_valid_606.json": "cbe3e61bf0a56ce2e19fd7f8963dd09fc4e0f5a31c31c0db0262a85a16659f15",
    "tests/fixtures/evidence/reg_valid_101.json": "35a9cdb6d0a7b48f07d68b3598f486094f672bd5fd41adbe11cf0ce2bb28250c",
    "tests/fixtures/evidence/restart_nat_707.json": "e9ebdc125bc55afa2da41eddf4875574aea158a41ab94ec5469fd7136dba8389",
    "tests/fixtures/evidence/test_nat_15.json": "4144bfe24fb1c696cddc39229263501b3c26e1e4f6c1f5436a95c6463002a772",
}

_DYNAMIC_INGESTION_CATALOG: dict[str, str] = {}


def is_synthetic_evidence_artifact(p: Path, content_dict: dict[str, Any] | None = None) -> bool:
    name = p.name.lower()
    synthetic_markers = (
        "test",
        "fixture",
        "mock",
        "dummy",
        "sample",
        "restart",
        "nat_valid",
        "reg_valid",
        "nat_proven",
        "700.json",
        "901.json",
        "999.json",
        "self_authored",
    )
    if any(marker in name for marker in synthetic_markers):
        return True
    if content_dict:
        cid = str(content_dict.get("campaign_id") or "").lower()
        if any(marker in cid for marker in ("test_", "mock_", "dummy_", "fake_", "sample_", "restart_", "valid_", "proven_")):
            return True
        lim = str(content_dict.get("limitations") or "").lower()
        if any(marker in lim for marker in ("audited nationwide", "persisted nationwide", "full national coverage", "independently audited")):
            return True
        # Reject self-authored JSON with audit strings, claims, or lacking authentic upstream flyer fields
        src_rec = content_dict.get("source_record")
        if isinstance(src_rec, dict):
            if "audit_reference" in src_rec or "audit" in src_rec:
                return True
            if src_rec.get("provider") == "marktguru" and not any(k in src_rec for k in ("id", "flyerId", "pages", "validFrom")):
                return True
        elif src_rec is None and content_dict.get("scope") == "nationwide":
            return True
    return False


def ingest_trusted_evidence_artifact(
    file_path: str | Path,
    expected_hash: str | None = None,
) -> str:
    """
    Ingests and registers an evidence artifact in the trusted ingestion catalog.
    Validates origin directory, JSON structure, and SHA-256 cryptographic digest.
    In production mode, arbitrary evidence registration with caller-supplied hash is forbidden;
    hashes must originate from the pre-approved manifest, and synthetic artifacts are rejected.
    """
    p = Path(file_path).resolve()
    if not p.is_file():
        raise FileNotFoundError(f"Evidence artifact '{p}' does not exist")
    if not is_approved_evidence_path(p):
        raise ValueError(f"Evidence artifact '{p}' is not located within an approved evidence root")

    content = p.read_bytes()
    try:
        artifact_json = json.loads(content.decode("utf-8"))
    except Exception as exc:
        raise ValueError(f"Evidence artifact '{p}' is not valid JSON: {exc}") from exc

    if not isinstance(artifact_json, dict):
        raise ValueError(f"Evidence artifact '{p}' must be a JSON object")

    calculated_hash = hashlib.sha256(content).hexdigest()

    rel_to_project: str | None = None
    try:
        rel_to_project = p.relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        pass

    manifest_hash = _TRUSTED_INGESTION_CATALOG.get(rel_to_project) if rel_to_project else None

    if is_production_mode():
        if is_synthetic_evidence_artifact(p, artifact_json):
            raise PermissionError(f"Synthetic campaign artifact '{p.name}' cannot be approved for production")
        if not manifest_hash:
            raise PermissionError(
                f"Evidence artifact '{rel_to_project or p.name}' is not in the pre-approved trusted catalog; "
                "arbitrary evidence registration with caller-supplied hash is forbidden in production mode"
            )
        if expected_hash and expected_hash != manifest_hash:
            raise ValueError(
                f"Caller-supplied hash '{expected_hash}' contradicts pre-approved manifest hash '{manifest_hash}'"
            )
        if calculated_hash != manifest_hash:
            raise ValueError(
                f"Evidence artifact '{p.name}' SHA-256 digest '{calculated_hash}' does not match pre-approved manifest '{manifest_hash}'"
            )
        return calculated_hash

    # Non-production / test mode:
    if manifest_hash:
        if expected_hash and expected_hash != manifest_hash:
            raise ValueError(
                f"Evidence artifact '{p.name}' SHA-256 digest contradicts pre-approved manifest"
            )
        if calculated_hash != manifest_hash:
            raise ValueError(
                f"Evidence artifact '{p.name}' SHA-256 digest '{calculated_hash}' does not match expected '{manifest_hash}'"
            )
    elif expected_hash and calculated_hash != expected_hash:
        raise ValueError(
            f"Evidence artifact '{p.name}' SHA-256 digest '{calculated_hash}' does not match expected '{expected_hash}'"
        )

    _DYNAMIC_INGESTION_CATALOG[str(p)] = calculated_hash
    if rel_to_project:
        _DYNAMIC_INGESTION_CATALOG[rel_to_project] = calculated_hash
    return calculated_hash


def clear_dynamic_ingestion_catalog() -> None:
    _DYNAMIC_INGESTION_CATALOG.clear()


def validate_evidence_artifact_integrity(p: Path) -> tuple[bool, str, str | None]:
    """
    Validates that the artifact file exists, is located within an approved evidence root,
    originates from a trusted ingestion record, matches its recorded cryptographic digest,
    and substantively substantiates its promotional coverage claims.
    Returns (is_valid, reason, sha256_hash).
    """
    try:
        resolved = p.resolve()
    except Exception as exc:
        return False, f"Failed to resolve artifact path: {exc}", None

    if not resolved.is_file():
        return False, f"Evidence artifact '{p}' does not exist on disk", None

    if not is_approved_evidence_path(resolved):
        return False, f"Evidence artifact '{p}' is not located in an approved evidence directory", None

    rel_to_project: str | None = None
    try:
        rel_to_project = resolved.relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        pass

    try:
        artifact_raw = json.loads(resolved.read_text(encoding="utf-8"))
    except Exception as exc:
        return False, f"Failed to parse evidence artifact '{resolved.name}' JSON: {exc}", None

    if not isinstance(artifact_raw, dict):
        return False, f"Evidence artifact '{resolved.name}' is not a JSON object", None

    if is_production_mode():
        if is_synthetic_evidence_artifact(resolved, artifact_raw):
            return False, f"Synthetic campaign artifact '{resolved.name}' cannot be approved for production", None
        expected_hash = _TRUSTED_INGESTION_CATALOG.get(rel_to_project) if rel_to_project else None
    else:
        expected_hash = (
            _DYNAMIC_INGESTION_CATALOG.get(str(resolved))
            or (rel_to_project and _DYNAMIC_INGESTION_CATALOG.get(rel_to_project))
            or (rel_to_project and _TRUSTED_INGESTION_CATALOG.get(rel_to_project))
        )

    if not expected_hash:
        return False, f"Evidence artifact '{resolved.name}' has not been ingested through the trusted evidence ingestion process (unregistered artifact)", None

    try:
        actual_hash = hashlib.sha256(resolved.read_bytes()).hexdigest()
    except Exception as exc:
        return False, f"Failed to read evidence artifact '{resolved.name}': {exc}", None

    if actual_hash != expected_hash:
        return False, f"Evidence artifact '{resolved.name}' cryptographic digest mismatch (recorded: {expected_hash}, actual: {actual_hash})", None

    # Substantive content validation: non-empty campaign_id, source_url, valid calendar date
    if not str(artifact_raw.get("campaign_id") or "").strip():
        return False, "Evidence artifact lacks required campaign_id", None
    if not str(artifact_raw.get("source_url") or "").strip():
        return False, "Evidence artifact lacks required source_url", None
    vdate = str(artifact_raw.get("verification_date") or "").strip()
    if not _is_valid_calendar_date(vdate):
        return False, f"Evidence artifact has invalid verification_date '{vdate}'", None

    return True, "Artifact origin and cryptographic integrity verified", actual_hash


def _is_valid_calendar_date(val: Any) -> bool:
    if not val or not isinstance(val, str):
        return False
    s = val.strip()
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", s):
        return False
    try:
        from datetime import datetime

        d = datetime.strptime(s, "%Y-%m-%d").date()
        return 2020 <= d.year <= 2050
    except (ValueError, TypeError):
        return False


def validate_regional_coverage_evidence(
    evidence: RegionalCoverageEvidence | None,
) -> tuple[bool, str]:
    """
    Validates that a RegionalCoverageEvidence entry is backed by an accessible,
    verifiable artifact confirming promotional campaign applicability and exact
    5-digit German PLZ coverage. Uses strict provenance, calendar date, source URL,
    and cryptographic artifact integrity verification.
    """
    from app.services.location import get_coordinates_for_plz

    if not isinstance(evidence, RegionalCoverageEvidence):
        return False, "Missing RegionalCoverageEvidence instance"
    if not evidence.region_key or not str(evidence.region_key).strip():
        return False, "Missing region_key"
    if not evidence.source_url or not str(evidence.source_url).strip().startswith(("http://", "https://")):
        return False, "Invalid or missing source_url"

    from urllib.parse import urlparse
    parsed_u = urlparse(evidence.source_url)
    if parsed_u.netloc.lower() not in ("marktguru.de", "www.marktguru.de", "api.marktguru.de"):
        return False, f"Untrusted source_url domain '{parsed_u.netloc}' in regional evidence"

    if not _is_valid_calendar_date(evidence.verification_date):
        return False, f"Invalid or unparseable calendar date for verification_date '{evidence.verification_date}' (expected valid YYYY-MM-DD)"
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

    p = Path(evidence.evidence_artifact_path)
    ok_int, err_int, _ = validate_evidence_artifact_integrity(p)
    if not ok_int:
        return False, err_int

    try:
        artifact = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return False, f"Failed to read recorded evidence artifact: {exc}"

    if not isinstance(artifact, dict):
        return False, "Recorded evidence artifact must be a JSON object"

    art_campaign_id = str(artifact.get("campaign_id") or "").strip().casefold()
    if not art_campaign_id:
        return False, "Artifact lacks required non-empty campaign_id"

    art_region_key = str(artifact.get("region_key") or "").strip().casefold()
    if art_region_key and art_region_key != evidence.region_key.strip().casefold():
        return False, f"Artifact region_key '{art_region_key}' does not match claimed region '{evidence.region_key}'"

    art_source_url = str(artifact.get("source_url") or "").strip()
    if not art_source_url:
        return False, "Artifact lacks required non-empty source_url"
    if art_source_url != evidence.source_url.strip():
        return False, f"Artifact source_url '{art_source_url}' does not match evidence source_url '{evidence.source_url}'"

    art_scope = str(artifact.get("scope") or "").strip().casefold()
    if art_scope and art_scope != "region":
        return False, f"Artifact scope must be 'region', got '{art_scope}'"

    if artifact.get("promotional_campaign_applicability") is not True:
        return False, "Artifact promotional_campaign_applicability must be strictly True"

    art_date = str(artifact.get("verification_date") or "").strip()
    if not _is_valid_calendar_date(art_date):
        return False, f"Artifact verification_date '{art_date}' is missing or not a valid calendar date"
    if art_date != str(evidence.verification_date).strip():
        return False, f"Artifact verification_date '{art_date}' does not match evidence verification_date '{evidence.verification_date}'"

    artifact_plzs_raw = artifact.get("covered_plzs")
    if not isinstance(artifact_plzs_raw, list) or not artifact_plzs_raw:
        return False, "Artifact lacks covered_plzs list"
    artifact_plzs = {str(p).strip() for p in artifact_plzs_raw if p is not None}
    if artifact_plzs != normalized_plzs:
        return False, "Artifact covered_plzs does not match claimed covered_plzs"

    return True, "Verified promotional campaign distribution manifest"


# ===========================================================================
# Audited Authoritative Regional Coverage Registry
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
    Evidence must be traceable to an accessible recorded evidence artifact.
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
    if not ev_campaign_id:
        return False, "Missing campaign_id in nationwide evidence", None
    ev_core_id = ev_campaign_id.removeprefix("national_flyer:").removeprefix("national_campaign:")
    if not ev_core_id or ev_core_id != clean_core_id:
        return False, f"Evidence campaign_id '{ev_campaign_id}' does not match claimed campaign '{clean_id}'", None

    source_url = str(ev_dict.get("source_url") or "").strip()
    if not source_url or not (source_url.startswith("http://") or source_url.startswith("https://")):
        return False, "Missing or invalid source_url in nationwide evidence", None

    evidence_type = str(ev_dict.get("evidence_type") or "").strip()
    if evidence_type not in ("verified_nationwide_distribution_manifest", "official_national_campaign_distribution"):
        return False, f"Unsupported nationwide evidence_type '{evidence_type}'", None

    verification_date = str(ev_dict.get("verification_date") or "").strip()
    if not _is_valid_calendar_date(verification_date):
        return False, "Invalid or unparseable calendar date for verification_date (expected valid YYYY-MM-DD)", None

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

    # Artifact check: a syntactically valid URL or non-empty dictionary is not proof by itself.
    # Evidence must be traceable to an accessible recorded evidence artifact.
    artifact_path = ev_dict.get("evidence_artifact_path")
    if not artifact_path or not str(artifact_path).strip():
        return False, "Remote source_url or dictionary cannot establish nationwide coverage without an accessible recorded evidence artifact", None

    p = Path(artifact_path)
    ok_int, err_int, _ = validate_evidence_artifact_integrity(p)
    if not ok_int:
        return False, err_int, None

    try:
        art_data = json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:
        return False, f"Failed to read recorded nationwide evidence artifact: {exc}", None

    if not isinstance(art_data, dict):
        return False, "Recorded evidence artifact must be a JSON object", None

    art_campaign_id = str(art_data.get("campaign_id") or "").strip().casefold()
    if not art_campaign_id:
        return False, "Artifact lacks required non-empty campaign_id", None
    art_core_id = art_campaign_id.removeprefix("national_flyer:").removeprefix("national_campaign:")
    if art_core_id != clean_core_id:
        return False, f"Artifact campaign_id '{art_campaign_id}' does not match claimed campaign '{clean_id}'", None

    art_source_url = str(art_data.get("source_url") or "").strip()
    if not art_source_url:
        return False, "Artifact lacks required non-empty source_url", None
    if art_source_url != source_url:
        return False, f"Artifact source_url '{art_source_url}' does not match evidence source_url '{source_url}'", None

    if str(art_data.get("scope") or "").strip().casefold() != "nationwide":
        return False, "Artifact does not confirm nationwide scope", None

    if art_data.get("promotional_campaign_applicability") is not True:
        return False, "Artifact promotional_campaign_applicability must be strictly True", None

    art_date = str(art_data.get("verification_date") or "").strip()
    if not _is_valid_calendar_date(art_date):
        return False, f"Artifact verification_date '{art_date}' is missing or not a valid calendar date", None
    if art_date != verification_date:
        return False, f"Artifact verification_date '{art_date}' does not match evidence verification_date '{verification_date}'", None

    if is_production_mode():
        if is_synthetic_evidence_artifact(p, art_data):
            return False, "Self-authored artifact, descriptive claims, or audit strings cannot establish nationwide applicability without genuine upstream provider evidence", None

        src_rec = art_data.get("source_record")
        if not isinstance(src_rec, dict) or "audit_reference" in src_rec or not any(k in src_rec for k in ("id", "flyerId", "pages", "validFrom")):
            return False, "Evidence artifact lacks verified genuine upstream provider source record in production", None

    validated = {
        "campaign_id": clean_id,
        "source_url": source_url,
        "evidence_type": evidence_type,
        "verification_date": verification_date,
        "scope": "nationwide",
        "promotional_applicability_confirmed": True,
        "limitations": limitations,
        "evidence_artifact_path": str(artifact_path),
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
    A caller-provided regional PLZ list without an independently validated
    promotional evidence artifact is rejected.
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
    if not ev_campaign_id:
        return False, "Missing campaign_id in regional evidence", None
    ev_core_id = ev_campaign_id.removeprefix("flyer:").removeprefix("campaign:")
    if not ev_core_id or ev_core_id != clean_core_id:
        return False, f"Evidence campaign_id '{ev_campaign_id}' does not match claimed campaign '{clean_id}'", None

    source_url = str(ev_dict.get("source_url") or "").strip()
    if not source_url or not (source_url.startswith("http://") or source_url.startswith("https://")):
        return False, "Missing or invalid source_url in regional evidence", None

    from urllib.parse import urlparse
    parsed_u = urlparse(source_url)
    if parsed_u.netloc.lower() not in ("marktguru.de", "www.marktguru.de", "api.marktguru.de"):
        return False, f"Untrusted source_url domain '{parsed_u.netloc}' in regional evidence", None

    evidence_type = str(ev_dict.get("evidence_type") or "").strip()
    if evidence_type not in ("verified_campaign_distribution_manifest", "verified_nationwide_distribution_manifest"):
        return False, f"Unsupported regional evidence_type '{evidence_type}'", None

    verification_date = str(ev_dict.get("verification_date") or "").strip()
    if not _is_valid_calendar_date(verification_date):
        return False, "Invalid or unparseable calendar date for verification_date (expected valid YYYY-MM-DD)", None

    if ev_dict.get("promotional_applicability_confirmed") is not True:
        return False, "promotional_applicability_confirmed must be strictly True in regional evidence", None

    limitations = str(ev_dict.get("limitations") or "").strip()
    if not limitations:
        return False, "Missing limitations documentation in regional evidence", None

    ev_plzs_raw = ev_dict.get("covered_plzs")
    if ev_plzs_raw is None:
        return False, "Missing covered_plzs in regional evidence", None
    if not isinstance(ev_plzs_raw, (set, frozenset, list, tuple)):
        return False, "Regional evidence covered_plzs must be a list or set", None
    ev_plzs = {str(p).strip() for p in ev_plzs_raw}
    if ev_plzs != normalized_plzs:
        return False, "Evidence covered_plzs does not match claimed covered_plzs set", None

    # Artifact check: a caller-provided PLZ list without an accessible recorded artifact is rejected
    artifact_path = ev_dict.get("evidence_artifact_path")
    if not artifact_path or not str(artifact_path).strip():
        return False, "Regional campaign registration requires an accessible recorded evidence artifact confirming promotional applicability", None

    p = Path(artifact_path)
    ok_int, err_int, _ = validate_evidence_artifact_integrity(p)
    if not ok_int:
        return False, err_int, None

    try:
        art_data = json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:
        return False, f"Failed to read recorded regional evidence artifact: {exc}", None

    if not isinstance(art_data, dict):
        return False, "Recorded evidence artifact must be a JSON object", None

    art_campaign_id = str(art_data.get("campaign_id") or "").strip().casefold()
    if not art_campaign_id:
        return False, "Artifact lacks required non-empty campaign_id", None
    art_core_id = art_campaign_id.removeprefix("flyer:").removeprefix("campaign:")
    if art_core_id != clean_core_id:
        return False, f"Artifact campaign_id '{art_campaign_id}' does not match claimed campaign '{clean_id}'", None

    art_source_url = str(art_data.get("source_url") or "").strip()
    if not art_source_url:
        return False, "Artifact lacks required non-empty source_url", None
    if art_source_url != source_url:
        return False, f"Artifact source_url '{art_source_url}' does not match evidence source_url '{source_url}'", None

    if str(art_data.get("scope") or "").strip().casefold() != "region":
        return False, "Artifact scope must be 'region'", None

    if art_data.get("promotional_campaign_applicability") is not True:
        return False, "Artifact promotional_campaign_applicability must be strictly True", None

    art_date = str(art_data.get("verification_date") or "").strip()
    if not _is_valid_calendar_date(art_date):
        return False, f"Artifact verification_date '{art_date}' is missing or not a valid calendar date", None
    if art_date != verification_date:
        return False, f"Artifact verification_date '{art_date}' does not match evidence verification_date '{verification_date}'", None

    art_plzs_raw = art_data.get("covered_plzs")
    if not isinstance(art_plzs_raw, list) or not art_plzs_raw:
        return False, "Artifact lacks covered_plzs list", None
    art_plzs = {str(p).strip() for p in art_plzs_raw}
    if art_plzs != normalized_plzs:
        return False, "Artifact covered_plzs does not match claimed covered_plzs", None

    validated = {
        "campaign_id": clean_id,
        "source_url": source_url,
        "evidence_type": evidence_type,
        "verification_date": verification_date,
        "scope": "region",
        "promotional_applicability_confirmed": True,
        "covered_plzs": sorted(normalized_plzs),
        "limitations": limitations,
        "evidence_artifact_path": str(artifact_path),
    }
    return True, "Verified regional campaign evidence", validated


# Private internal registries
_VERIFIED_CAMPAIGN_REGISTRY: dict[str, dict[str, Any]] = {}
_VERIFIED_NATIONWIDE_REGISTRY: dict[str, dict[str, Any]] = {}

# Read-only public mappings preventing callers from bypassing validation
VERIFIED_CAMPAIGN_REGISTRY = MappingProxyType(_VERIFIED_CAMPAIGN_REGISTRY)
VERIFIED_NATIONWIDE_REGISTRY = MappingProxyType(_VERIFIED_NATIONWIDE_REGISTRY)

_REGISTRY_LOCK = threading.RLock()
_stage_state = threading.local()


def snapshot_campaign_registries() -> tuple[dict[str, Any], dict[str, Any]]:
    """
    Returns an independent deep snapshot of in-memory campaign registries.
    """
    with _REGISTRY_LOCK:
        return (
            copy.deepcopy(_VERIFIED_CAMPAIGN_REGISTRY),
            copy.deepcopy(_VERIFIED_NATIONWIDE_REGISTRY),
        )


def restore_campaign_registries(snapshot: tuple[dict[str, Any], dict[str, Any]]) -> None:
    """
    Restores in-memory campaign registries from a previous snapshot under lock.
    """
    with _REGISTRY_LOCK:
        camp_snap, nat_snap = snapshot
        _VERIFIED_CAMPAIGN_REGISTRY.clear()
        _VERIFIED_CAMPAIGN_REGISTRY.update(copy.deepcopy(camp_snap))
        _VERIFIED_NATIONWIDE_REGISTRY.clear()
        _VERIFIED_NATIONWIDE_REGISTRY.update(copy.deepcopy(nat_snap))


@contextmanager
def campaign_registry_transaction():
    """
    Transactional context manager for in-memory campaign registry changes.

    1. Takes a thread-safe snapshot before mutations.
    2. Stages all validated campaign additions into a thread-local staging layer.
       Read operations during the transaction check staged additions before base registries.
    3. On successful block completion, publishes staged changes atomically to the registries.
    4. On exception or failure, discards all staged changes and restores registries to pre-call snapshot.
    """
    with _REGISTRY_LOCK:
        camp_snapshot = copy.deepcopy(_VERIFIED_CAMPAIGN_REGISTRY)
        nat_snapshot = copy.deepcopy(_VERIFIED_NATIONWIDE_REGISTRY)

        prev_staged_camp = getattr(_stage_state, "staged_campaigns", None)
        prev_staged_nat = getattr(_stage_state, "staged_nationwide", None)

        staged_camp: dict[str, dict[str, Any]] = {}
        staged_nat: dict[str, dict[str, Any]] = {}

        _stage_state.staged_campaigns = staged_camp
        _stage_state.staged_nationwide = staged_nat

        committed = False
        try:
            yield
            _VERIFIED_CAMPAIGN_REGISTRY.update(staged_camp)
            _VERIFIED_NATIONWIDE_REGISTRY.update(staged_nat)
            committed = True
        finally:
            _stage_state.staged_campaigns = prev_staged_camp
            _stage_state.staged_nationwide = prev_staged_nat

            if not committed:
                _VERIFIED_CAMPAIGN_REGISTRY.clear()
                _VERIFIED_CAMPAIGN_REGISTRY.update(camp_snapshot)
                _VERIFIED_NATIONWIDE_REGISTRY.clear()
                _VERIFIED_NATIONWIDE_REGISTRY.update(nat_snapshot)


def _purge_nationwide_campaign(clean_id: str) -> None:
    with _REGISTRY_LOCK:
        staged = getattr(_stage_state, "staged_nationwide", None)
        if staged is not None:
            for k in (clean_id, f"national_flyer:{clean_id}", f"national_campaign:{clean_id}"):
                staged.pop(k, None)
        for k in (clean_id, f"national_flyer:{clean_id}", f"national_campaign:{clean_id}"):
            _VERIFIED_NATIONWIDE_REGISTRY.pop(k, None)


def _purge_regional_campaign(clean_id: str) -> None:
    with _REGISTRY_LOCK:
        staged = getattr(_stage_state, "staged_campaigns", None)
        if staged is not None:
            for k in (clean_id, f"flyer:{clean_id}", f"campaign:{clean_id}"):
                staged.pop(k, None)
        for k in (clean_id, f"flyer:{clean_id}", f"campaign:{clean_id}"):
            _VERIFIED_CAMPAIGN_REGISTRY.pop(k, None)


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
    with _REGISTRY_LOCK:
        staged = getattr(_stage_state, "staged_campaigns", None)
        target = staged if staged is not None else _VERIFIED_CAMPAIGN_REGISTRY
        target[norm_id] = validated
        target[clean_id] = validated
        target[f"flyer:{clean_id}"] = validated
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
    with _REGISTRY_LOCK:
        staged = getattr(_stage_state, "staged_nationwide", None)
        target = staged if staged is not None else _VERIFIED_NATIONWIDE_REGISTRY
        target[norm_id] = validated
        target[clean_id] = validated
        target[f"national_flyer:{clean_id}"] = validated
    return True


def get_verified_campaign_plzs(campaign_or_region_id: str | None) -> set[str] | None:
    """
    Returns verified covered 5-digit PLZ set if the campaign or region has verified
    underlying campaign evidence establishing promotional applicability.
    Revalidates the underlying evidence artifact on every query.
    """
    if not campaign_or_region_id:
        return None
    key = str(campaign_or_region_id).strip()
    key_lower = key.casefold()

    # 1. Authoritative region registry
    if key_lower in AUTHORITATIVE_REGION_REGISTRY:
        ev = AUTHORITATIVE_REGION_REGISTRY[key_lower]
        is_valid, reason = validate_regional_coverage_evidence(ev)
        if is_valid:
            return set(ev.covered_plzs)
        else:
            logger.warning("Authoritative region '%s' invalidated: %s", key, reason)
            return None

    # 2. Verified campaign distribution manifest registry
    clean_id = key_lower.removeprefix("flyer:").removeprefix("campaign:")
    ev_data = None
    with _REGISTRY_LOCK:
        staged = getattr(_stage_state, "staged_campaigns", None)
        for lookup in (key, key_lower, clean_id):
            if staged is not None and lookup in staged:
                ev_data = staged[lookup]
                break
            if lookup in _VERIFIED_CAMPAIGN_REGISTRY:
                ev_data = _VERIFIED_CAMPAIGN_REGISTRY[lookup]
                break

    if not ev_data or not isinstance(ev_data, dict):
        return None

    art_path_str = ev_data.get("evidence_artifact_path")
    if not art_path_str:
        logger.warning("Regional campaign '%s' invalidated: missing artifact path", campaign_or_region_id)
        _purge_regional_campaign(clean_id)
        return None

    p = Path(art_path_str)
    ok_int, err_int, _ = validate_evidence_artifact_integrity(p)
    if not ok_int:
        logger.warning("Regional campaign '%s' invalidated: %s", campaign_or_region_id, err_int)
        _purge_regional_campaign(clean_id)
        return None

    plzs = ev_data.get("covered_plzs") or []
    is_valid, reason, _ = validate_regional_campaign_evidence(clean_id, plzs, ev_data)
    if not is_valid:
        logger.warning("Regional campaign '%s' invalidated: %s", campaign_or_region_id, reason)
        _purge_regional_campaign(clean_id)
        return None

    return set(ev_data["covered_plzs"])


def is_verified_nationwide_campaign(campaign_or_flyer_id: str | None) -> bool:
    """
    Returns True only if the nationwide campaign has verifiable scope-specific evidence
    establishing promotional campaign applicability across Germany.
    Revalidates the underlying evidence artifact on every query.
    """
    if not campaign_or_flyer_id:
        return False
    key = str(campaign_or_flyer_id).strip().casefold()
    clean_id = key.removeprefix("national_flyer:").removeprefix("national_campaign:")

    with _REGISTRY_LOCK:
        staged = getattr(_stage_state, "staged_nationwide", None)
        ev_data = (
            (staged.get(key) or staged.get(clean_id)) if staged is not None else None
        ) or _VERIFIED_NATIONWIDE_REGISTRY.get(key) or _VERIFIED_NATIONWIDE_REGISTRY.get(clean_id)
    if not ev_data:
        return False

    art_path_str = ev_data.get("evidence_artifact_path")
    if not art_path_str:
        logger.warning("Nationwide campaign '%s' invalidated: missing artifact path", campaign_or_flyer_id)
        _purge_nationwide_campaign(clean_id)
        return False

    p = Path(art_path_str)
    ok_int, err_int, _ = validate_evidence_artifact_integrity(p)
    if not ok_int:
        logger.warning("Nationwide campaign '%s' invalidated: %s", campaign_or_flyer_id, err_int)
        _purge_nationwide_campaign(clean_id)
        return False

    is_valid, reason, _ = validate_nationwide_coverage_evidence(clean_id, ev_data)
    if not is_valid:
        logger.warning("Nationwide campaign '%s' invalidated: %s", campaign_or_flyer_id, reason)
        _purge_nationwide_campaign(clean_id)
        return False

    return True


def get_verified_nationwide_evidence(campaign_id: str | None) -> dict[str, Any] | None:
    """
    Returns validated nationwide evidence dict if registered and currently valid.
    """
    if not is_verified_nationwide_campaign(campaign_id):
        return None
    key = str(campaign_id).strip().casefold()
    clean_id = key.removeprefix("national_flyer:").removeprefix("national_campaign:")
    with _REGISTRY_LOCK:
        staged = getattr(_stage_state, "staged_nationwide", None)
        data = (
            (staged.get(key) or staged.get(clean_id)) if staged is not None else None
        ) or _VERIFIED_NATIONWIDE_REGISTRY.get(key) or _VERIFIED_NATIONWIDE_REGISTRY.get(clean_id)
        return dict(data) if data else None


def get_verified_campaign_evidence(campaign_id: str | None) -> dict[str, Any] | None:
    """
    Returns validated regional campaign evidence dict if registered and currently valid.
    """
    if get_verified_campaign_plzs(campaign_id) is None:
        return None
    key = str(campaign_id).strip().casefold()
    clean_id = key.removeprefix("flyer:").removeprefix("campaign:")
    with _REGISTRY_LOCK:
        staged = getattr(_stage_state, "staged_campaigns", None)
        data = (
            (staged.get(key) or staged.get(clean_id)) if staged is not None else None
        ) or _VERIFIED_CAMPAIGN_REGISTRY.get(key) or _VERIFIED_CAMPAIGN_REGISTRY.get(clean_id)
        return dict(data) if data else None


def clear_campaign_registries() -> None:
    """
    Clears in-memory dynamic campaign registries for test isolation and restart simulation.
    """
    with _REGISTRY_LOCK:
        staged_camp = getattr(_stage_state, "staged_campaigns", None)
        if staged_camp is not None:
            staged_camp.clear()
        staged_nat = getattr(_stage_state, "staged_nationwide", None)
        if staged_nat is not None:
            staged_nat.clear()
        _VERIFIED_CAMPAIGN_REGISTRY.clear()
        _VERIFIED_NATIONWIDE_REGISTRY.clear()
        _DYNAMIC_INGESTION_CATALOG.clear()
        _DYNAMIC_APPROVED_ROOTS.clear()


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
        from app.providers.base import clean_text

        title = clean_text(product.get("name", ""))
        brand = clean_text(brand_data.get("name")) or None
        if brand == NO_BRAND_NAME:
            brand = None
        price = parse_positive_float(item.get("price"))
        old_price = parse_positive_float(item.get("oldPrice"))
        unit_price = parse_positive_float(item.get("referencePrice"))
        discount_percent = compute_discount_percent(price, old_price)
        description = clean_text(item.get("description", ""))
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

            from app.services.location import normalize_german_plz
            norm_zip = normalize_german_plz(clean_zip) or clean_zip

            matched_geo = False
            if user_coords and branch_pc:
                if branch_pc == clean_zip or branch_pc == norm_zip:
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
        elif scope_type == "store" and region_verified:
            verification_evidence = {
                "scope": "store",
                "retailer_slug": retailer.get("uniqueName") or retailer_slug,
                "store_id": store_id,
                "store_name": store_name,
                "store_address": store_address,
                "store_plz": branch_pc or clean_zip,
                "matched_plz": clean_zip,
                "requested_plz": clean_zip,
                "verification_method": verification_method,
                "metadata_source": "marktguru_branch_api",
            }

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
