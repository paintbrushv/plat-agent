"""CompsStep — dispatches the comp-finder sibling agent and validates output.

Spec §2.2 (comps.json schema), §4.3 (blocker / hard-error dependency rules),
§4.4.1 (_complete marker). The agent does ALL of the comp-fetching and the
field-origin translation; this step is the orchestrator-side wrapper that
marshals inputs, validates outputs, and decides ok-vs-blocked-vs-error.
"""

from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Final

from pydantic import ValidationError

from plat_agent.contracts.domain.market_study import (
    CompCoverageSignal,
    CompFinderRequest,
    CompFinderResponse,
    CompsArtifact,
    MarketStudyBootstrapPlan,
    MarketStudySkillCommand,
    cohort_key,
)
from plat_agent.contracts.envelope import BridgeRequestV1, BridgeResponseV1
from plat_agent.dispatch.sibling import (
    DispatchError,
    SiblingRepo,
    dispatch_sibling_agent as _dispatch_sibling_agent,
)
from plat_agent.lifecycle.cache import (
    compute_input_hash,
    is_satisfied,
    write_provenance,
)
from plat_agent.lifecycle.complete_marker import write_complete_marker
from plat_agent.lifecycle.defaults import CONTRACT_VERSION
from plat_agent.lifecycle.protocol import StepResult
from plat_agent.lifecycle.state import BlockerItem, LifecycleState, StepName


_STEP_NAME: Final[StepName] = "comps"
_MANIFEST_TITLE_RE: Final[re.Pattern[str]] = re.compile(r"^# Deal Manifest:\s*(.+?)\s*$")
_OM_RENT_COMPS_HEADER_RE: Final[re.Pattern[str]] = re.compile(
    r"rent comparables summary", re.IGNORECASE
)
_RAW_RENT_ROLL_NAME_HINTS: Final[tuple[str, ...]] = (
    "rent roll",
    "rentroll",
    "rediq",
)
_OM_RENT_COMP_ROW_RE: Final[re.Pattern[str]] = re.compile(
    r"^\s*(?P<rank>\d+)\s+"
    r"(?P<name>.+?)\s+"
    r"(?P<distance>\d+(?:\.\d+)?)\s+"
    r"(?P<units>\d+)\s+"
    r"(?P<year_built>\d{4})\s+"
    r"(?P<occupancy>\d+(?:\.\d+)?%)\s+"
    r"(?P<avg_sqft>[\d,]+)\s+"
    r"\$(?P<avg_rent>[\d,]+)\s+"
    r"\$(?P<rent_psf>\d+(?:\.\d+)?)\s*$"
)
_UNIT_TYPE_BEDROOMS_RE: Final[re.Pattern[str]] = re.compile(r"^(?P<beds>\d+)BR$", re.IGNORECASE)


def _slugify(value: str, separator: str = "-") -> str:
    slug = re.sub(r"[^a-z0-9]+", separator, (value or "").strip().lower())
    slug = re.sub(re.escape(separator) + r"{2,}", separator, slug)
    return slug.strip(separator)


def _relative_to(base: Path, path: Path | None) -> str | None:
    if path is None:
        return None
    try:
        return str(path.resolve().relative_to(base.resolve()))
    except ValueError:
        return str(path)


def _extract_subject_name(canonical: dict, run_dir: Path | None) -> str | None:
    if run_dir is not None:
        manifest_path = run_dir / "intake" / "manifest.md"
        if manifest_path.exists():
            for line in manifest_path.read_text(encoding="utf-8").splitlines():
                match = _MANIFEST_TITLE_RE.match(line.strip())
                if match:
                    name = match.group(1).strip()
                    if name:
                        return name

    metadata = canonical.get("metadata") or {}
    deal_name = str(metadata.get("deal_name") or "").strip()
    if deal_name:
        return deal_name
    deal_id = str(metadata.get("deal_id") or "").strip()
    if not deal_id:
        return None
    collapsed = re.sub(r"\s{2,}", " | ", deal_id)
    candidate = collapsed.split("|", 1)[0].strip()
    return candidate or None


def _find_om_path(run_dir: Path | None) -> Path | None:
    if run_dir is None:
        return None
    raw_inputs_dir = run_dir / "raw_inputs"
    if not raw_inputs_dir.is_dir():
        return None
    candidates = sorted(
        (
            path
            for path in raw_inputs_dir.iterdir()
            if path.is_file() and path.suffix.lower() == ".pdf" and "om" in path.name.lower()
        ),
        key=lambda path: path.name.lower(),
    )
    if candidates:
        return candidates[0]
    pdfs = sorted(
        (path for path in raw_inputs_dir.iterdir() if path.is_file() and path.suffix.lower() == ".pdf"),
        key=lambda path: path.stat().st_size,
        reverse=True,
    )
    return pdfs[0] if pdfs else None


def _find_raw_rent_roll_path(run_dir: Path | None) -> Path | None:
    if run_dir is None:
        return None
    raw_inputs_dir = run_dir / "raw_inputs"
    if not raw_inputs_dir.is_dir():
        return None

    candidates = [
        path
        for path in raw_inputs_dir.iterdir()
        if path.is_file() and path.suffix.lower() in {".xlsx", ".xls", ".csv"}
    ]
    if not candidates:
        return None

    def _priority(path: Path) -> tuple[int, int, str]:
        lower = path.name.lower()
        hinted = any(hint in lower for hint in _RAW_RENT_ROLL_NAME_HINTS)
        return (1 if hinted else 0, int(path.stat().st_size), lower)

    return sorted(candidates, key=_priority, reverse=True)[0]


def _extract_om_text(path: Path) -> str:
    result = subprocess.run(
        ["/opt/homebrew/bin/pdftotext", "-layout", str(path), "-"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return ""
    return result.stdout or ""


def _extract_om_comp_seed(run_dir: Path | None, deal_root: str) -> tuple[list[dict], str | None]:
    om_path = _find_om_path(run_dir)
    if om_path is None:
        return [], None

    text = _extract_om_text(om_path)
    if not text:
        return [], None

    lines = text.splitlines()
    start_idx = None
    for idx, line in enumerate(lines):
        if _OM_RENT_COMPS_HEADER_RE.search(line):
            start_idx = idx + 1
            break
    if start_idx is None:
        return [], None

    rows: list[dict] = []
    for line in lines[start_idx:]:
        if "Average of Comps" in line:
            break
        match = _OM_RENT_COMP_ROW_RE.match(line)
        if not match:
            continue
        rows.append(
            {
                "rank": int(match.group("rank")),
                "name": match.group("name").strip(),
                "distance_miles": float(match.group("distance")),
                "units": int(match.group("units")),
                "year_built": int(match.group("year_built")),
                "avg_sqft": float(match.group("avg_sqft").replace(",", "")),
                "avg_rent": float(match.group("avg_rent").replace(",", "")),
                "rent_psf": float(match.group("rent_psf")),
                "source": "broker_om_rent_comparables_summary",
            }
        )

    if not rows:
        return [], None

    try:
        om_relative = str(om_path.resolve().relative_to(Path(deal_root).resolve()))
    except ValueError:
        om_relative = om_path.name
    return rows, om_relative


def _build_comp_coverage_signal(
    *,
    unit_mix_summary: list[dict],
    om_comp_seed: list[dict],
) -> CompCoverageSignal | None:
    """Assess whether broker OM comp coverage is too thin to trust alone.

    Only emit a signal when a broker seed actually exists. No seed means the
    market-study workflow is already building from broader search / config
    sources instead of relying on a curated broker list.
    """
    if not om_comp_seed:
        return None

    dominant_row = max(
        unit_mix_summary,
        key=lambda row: int(row.get("count") or 0),
        default=None,
    )
    dominant_key = None
    dominant_bedrooms = None
    dominant_unit_count = None
    if dominant_row is not None:
        try:
            dominant_bedrooms = int(dominant_row.get("bedrooms") or 0)
            dominant_baths = float(dominant_row.get("bathrooms") or 0.0)
            dominant_sqft = int(float(dominant_row.get("sqft") or 0))
            dominant_unit_count = int(dominant_row.get("count") or 0)
            dominant_key = cohort_key(dominant_bedrooms, dominant_baths, dominant_sqft)
        except (TypeError, ValueError):
            dominant_key = None

    broker_seed_comp_count = len(om_comp_seed)
    reasons: list[str] = []
    minimum_total = 5
    minimum_dominant = 3

    if broker_seed_comp_count < minimum_total:
        reasons.append(
            f"Broker OM seed only contains {broker_seed_comp_count} comp(s); "
            f"expand to at least {minimum_total} credible nearby comps before trusting the rent story."
        )
    if dominant_key is not None:
        reasons.append(
            f"Broker OM seed does not by itself prove direct-site effective-rent coverage for dominant cohort "
            f"{dominant_key}; target at least {minimum_dominant} comps for that cohort."
        )

    return CompCoverageSignal(
        broker_seed_comp_count=broker_seed_comp_count,
        minimum_total_comp_count=minimum_total,
        dominant_cohort_key=dominant_key,
        dominant_bedrooms=dominant_bedrooms,
        dominant_unit_count=dominant_unit_count,
        minimum_dominant_cohort_comp_count=minimum_dominant,
        requires_universe_expansion=broker_seed_comp_count < minimum_total,
        reasons=reasons,
    )


def _unit_type_matches_bedrooms(unit_type: str | None, bedrooms: int) -> bool:
    if not isinstance(unit_type, str):
        return False
    normalized = unit_type.strip()
    if bedrooms == 0:
        return normalized.lower() == "studio"
    match = _UNIT_TYPE_BEDROOMS_RE.match(normalized)
    if match is None:
        return False
    return int(match.group("beds")) == bedrooms


def _find_existing_property_config(
    repo_root: Path,
    *,
    market_slug: str,
    property_slug: str,
    subject_name: str | None,
    property_address: str,
) -> Path | None:
    config_dir = repo_root / "agents" / "configs"
    expected = config_dir / f"{market_slug}_{property_slug.replace('-', '_')}.yaml"
    if expected.exists():
        return expected
    normalized_property_slug = _normalize_name(property_slug)
    normalized_subject_name = _normalize_name(subject_name)
    address_lower = property_address.lower()
    for candidate in sorted(config_dir.glob(f"{market_slug}_*.yaml")):
        stem_suffix = candidate.stem.removeprefix(f"{market_slug}_")
        if _normalize_name(stem_suffix) == normalized_property_slug:
            return candidate
        try:
            text = candidate.read_text(encoding="utf-8").lower()
        except OSError:
            continue
        if normalized_subject_name and normalized_subject_name in _normalize_name(text):
            return candidate
        if address_lower and address_lower in text:
            return candidate
    return None


def _find_existing_property_reports_dir(
    repo_root: Path,
    *,
    metro_dir_slug: str,
    property_slug: str,
    subject_name: str | None,
) -> Path | None:
    metro_reports_dir = repo_root / "reports" / metro_dir_slug
    expected = metro_reports_dir / property_slug
    if expected.exists():
        return expected
    if not metro_reports_dir.is_dir():
        return None
    normalized_property_slug = _normalize_name(property_slug)
    normalized_subject_name = _normalize_name(subject_name)
    for candidate in sorted(path for path in metro_reports_dir.iterdir() if path.is_dir()):
        normalized_candidate = _normalize_name(candidate.name)
        if normalized_candidate == normalized_property_slug:
            return candidate
        if normalized_subject_name and normalized_candidate == normalized_subject_name:
            return candidate
    return None


def _build_market_study_bootstrap_plan(
    *,
    canonical: dict,
    run_dir: Path | None,
    deal_root: str,
    subject_name: str | None,
    property_address: str,
    market: str,
) -> MarketStudyBootstrapPlan | None:
    repo = SiblingRepo.from_env_or_default("market-study-agent", "market-study-agent")
    repo_root = repo.path
    if not repo_root.exists():
        return None

    deal_root_path = Path(deal_root)
    metro_slug = market
    metro_dir_slug = market.replace("_", "-")
    property_slug = _slugify(subject_name or property_address.split(",", 1)[0] or "subject-property")
    property_config_relative = f"agents/configs/{metro_slug}_{property_slug.replace('-', '_')}.yaml"
    property_reports_dir_relative = f"reports/{metro_dir_slug}/{property_slug}"
    metro_config_relative = f"agents/configs/{metro_slug}.yaml"
    metro_reports_dir_relative = f"reports/{metro_dir_slug}"

    metro_config_path = repo_root / metro_config_relative
    metro_reports_dir = repo_root / metro_reports_dir_relative
    property_config_path = _find_existing_property_config(
        repo_root,
        market_slug=market,
        property_slug=property_slug,
        subject_name=subject_name,
        property_address=property_address,
    )
    property_reports_dir = _find_existing_property_reports_dir(
        repo_root,
        metro_dir_slug=metro_dir_slug,
        property_slug=property_slug,
        subject_name=subject_name,
    )
    floorplan_summary_path = (
        property_reports_dir / "rent-roll" / "clean" / "floorplan_summary.csv"
        if property_reports_dir is not None
        else None
    )
    raw_rent_roll_path = _find_raw_rent_roll_path(run_dir)

    metro_exists = metro_config_path.exists() and metro_reports_dir.exists()
    property_exists = property_config_path is not None or property_reports_dir is not None
    property_onboarded = (
        property_config_path is not None
        and property_reports_dir is not None
        and floorplan_summary_path is not None
        and floorplan_summary_path.exists()
    )

    if not metro_exists:
        workflow_stage = "new_metro"
    elif not property_exists:
        workflow_stage = "new_property"
    elif not property_onboarded:
        workflow_stage = "full_onboarding"
    else:
        workflow_stage = "existing_property"

    property_config_command_target = (
        _relative_to(repo_root, property_config_path)
        or property_config_relative
    )
    raw_rent_roll_relative = _relative_to(deal_root_path, raw_rent_roll_path)

    full_onboarding_command = "/full-onboarding"
    if property_config_command_target:
        full_onboarding_command += f" --config {property_config_command_target}"
    if raw_rent_roll_relative:
        full_onboarding_command += f" --raw {raw_rent_roll_relative}"
    full_onboarding_command += " --skip-commit"

    analyze_command = f"/analyze-comps {Path(property_config_command_target).stem} --track"
    pull_command = f"/pull-comps {Path(property_config_command_target).stem} --refresh"

    recommended_skill_commands: list[MarketStudySkillCommand] = []
    if workflow_stage == "new_metro":
        recommended_skill_commands.append(
            MarketStudySkillCommand(
                skill_name="new-metro",
                command=f"/new-metro {metro_slug}",
                reason="Market-study repo is missing the metro config and/or metro reports directory.",
            )
        )
        recommended_skill_commands.append(
            MarketStudySkillCommand(
                skill_name="full-onboarding",
                command=full_onboarding_command,
                reason="After metro scaffolding, bootstrap the subject property and create repo-native outputs end to end.",
            )
        )
        recommended_skill_commands.append(
            MarketStudySkillCommand(
                skill_name="analyze-comps",
                command=analyze_command,
                reason="Refresh the analyst-grade comp report after onboarding so reports/<metro>/<property>/comps stays current.",
            )
        )
    elif workflow_stage == "new_property":
        recommended_skill_commands.append(
            MarketStudySkillCommand(
                skill_name="new-property",
                command=f"/new-property {property_slug} --metro {metro_slug}",
                reason="Metro exists, but the subject property config/report tree does not.",
            )
        )
        recommended_skill_commands.append(
            MarketStudySkillCommand(
                skill_name="full-onboarding",
                command=full_onboarding_command,
                reason="Complete the initial property onboarding, rent-roll ETL, and comp workflow.",
            )
        )
        recommended_skill_commands.append(
            MarketStudySkillCommand(
                skill_name="analyze-comps",
                command=analyze_command,
                reason="Ensure the repo-native competitive positioning report is regenerated after onboarding.",
            )
        )
    elif workflow_stage == "full_onboarding":
        recommended_skill_commands.append(
            MarketStudySkillCommand(
                skill_name="full-onboarding",
                command=full_onboarding_command,
                reason="Property scaffolding exists, but onboarding is incomplete because the clean floorplan summary is missing.",
            )
        )
        recommended_skill_commands.append(
            MarketStudySkillCommand(
                skill_name="analyze-comps",
                command=analyze_command,
                reason="Refresh the competitive analysis report after the onboarding rerun.",
            )
        )
    else:
        recommended_skill_commands.append(
            MarketStudySkillCommand(
                skill_name="pull-comps",
                command=pull_command,
                reason="Existing property is already onboarded; refresh the comp dataset using the property config.",
            )
        )
        recommended_skill_commands.append(
            MarketStudySkillCommand(
                skill_name="analyze-comps",
                command=analyze_command,
                reason="Refresh the repo-native comp analysis markdown alongside the updated comp pull.",
            )
        )

    return MarketStudyBootstrapPlan(
        market_study_repo_relative=_relative_to(deal_root_path, repo_root),
        metro_slug=metro_slug,
        metro_dir_slug=metro_dir_slug,
        property_slug=property_slug,
        raw_rent_roll_relative=raw_rent_roll_relative,
        metro_config_relative=metro_config_relative,
        metro_reports_dir_relative=metro_reports_dir_relative,
        property_config_relative=_relative_to(repo_root, property_config_path) or property_config_relative,
        property_reports_dir_relative=_relative_to(repo_root, property_reports_dir) or property_reports_dir_relative,
        floorplan_summary_relative=_relative_to(repo_root, floorplan_summary_path),
        metro_exists=metro_exists,
        property_exists=property_exists,
        property_onboarded=property_onboarded,
        workflow_stage=workflow_stage,
        recommended_skill_commands=recommended_skill_commands,
    )


def _normalize_name(value: str | None) -> str:
    return "".join(ch.lower() for ch in (value or "") if ch.isalnum())


def _build_salvaged_comps_artifact(
    *,
    request: CompFinderRequest,
    comps_by_cohort: dict,
    run_id: str,
    provenance_payload: dict | None = None,
    deal_root: Path,
) -> dict:
    seed_by_name = {
        _normalize_name(str(row.get("name") or "")): row
        for row in request.om_comp_seed
        if row.get("name")
    }
    comps_index: dict[tuple[str, str], dict] = {}
    unit_keys_by_comp: dict[tuple[str, str], set[tuple]] = {}

    for entries in comps_by_cohort.values():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("property_name") or "Unknown Comp")
            address = str(entry.get("address") or name)
            key = (name, address)
            seed = seed_by_name.get(_normalize_name(name), {})
            comp_row = comps_index.setdefault(
                key,
                {
                    "comp_id": f"comp_{len(comps_index) + 1:03d}",
                    "name": name,
                    "address": address,
                    "units": int(seed.get("units") or 1),
                    "unit_types": [],
                },
            )
            if entry.get("year_built") is not None:
                comp_row["year_built"] = int(entry["year_built"])
            elif seed.get("year_built") is not None:
                comp_row["year_built"] = int(seed["year_built"])
            if entry.get("distance_miles") is not None:
                comp_row["distance_miles"] = float(entry["distance_miles"])

            unit_type = "Studio" if int(entry.get("bedrooms") or 0) == 0 else f"{int(entry.get('bedrooms') or 0)}BR"
            unit_row = {
                "unit_type": unit_type,
                "sqft": float(entry.get("sqft") or 0.0),
                "face_rent": float(entry.get("asking_rent") or 0.0),
                "rent_psf": float(entry.get("rent_per_sqft") or 0.0),
            }
            dedupe_key = (
                unit_row["unit_type"],
                unit_row["sqft"],
                unit_row["face_rent"],
                unit_row["rent_psf"],
            )
            seen = unit_keys_by_comp.setdefault(key, set())
            if dedupe_key not in seen:
                comp_row["unit_types"].append(unit_row)
                seen.add(dedupe_key)

    as_of = None
    if isinstance(provenance_payload, dict):
        extracted = provenance_payload.get("extraction_date")
        if isinstance(extracted, str) and extracted.strip():
            as_of = extracted.strip()
    if not as_of:
        as_of = datetime.now(timezone.utc).date().isoformat()

    artifact = {
        "subject": {
            "address": request.property_address,
            "metro_slug": request.market,
            "metro_display": request.market.replace("_", " ").title(),
        },
        "as_of": as_of,
        "comps": list(comps_index.values()),
    }
    snapshot_path = deal_root / "outputs" / run_id / "comps" / "snapshot.json"
    if snapshot_path.exists():
        artifact["raw_snapshot_relative"] = f"outputs/{run_id}/comps/snapshot.json"
    artifact["submarket_aggregates"] = {"comp_count": len(artifact["comps"])}
    return artifact


def _salvage_comp_finder_response_from_disk(
    *,
    run_dir: Path,
    canonical: dict,
    log_path: Path | None,
) -> BridgeResponseV1 | None:
    market_study_dir = run_dir / "market_study"
    grouped_path = market_study_dir / "comps_cohort_grouped.json"
    if not grouped_path.exists():
        return None
    freshness_floor = datetime.now(timezone.utc).timestamp() - 900
    if grouped_path.stat().st_mtime < freshness_floor:
        return None

    try:
        grouped_payload = json.loads(grouped_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    comps_by_cohort = grouped_payload.get("comps_by_cohort")
    if not isinstance(comps_by_cohort, dict):
        return None
    if not any(isinstance(rows, list) and rows for rows in comps_by_cohort.values()):
        return None

    provenance_path = market_study_dir / "_provenance.json"
    provenance_payload: dict | None = None
    if provenance_path.exists() and provenance_path.stat().st_mtime >= freshness_floor:
        try:
            provenance_payload = json.loads(provenance_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            provenance_payload = None

    deal_root = run_dir.parent.parent
    request = _build_comp_finder_request(
        canonical,
        run_dir=run_dir,
        deal_root=str(deal_root),
    )
    comps_dir = run_dir / "comps"
    comps_dir.mkdir(parents=True, exist_ok=True)
    comps_artifact = _build_salvaged_comps_artifact(
        request=request,
        comps_by_cohort=comps_by_cohort,
        run_id=run_dir.name,
        provenance_payload=provenance_payload,
        deal_root=deal_root,
    )
    (comps_dir / "comps.json").write_text(
        json.dumps(comps_artifact, indent=2),
        encoding="utf-8",
    )

    methodology_notes = list(grouped_payload.get("methodology_notes") or [])
    if isinstance(provenance_payload, dict):
        fallback_reason = provenance_payload.get("fallback_reason")
        if isinstance(fallback_reason, str) and fallback_reason.strip():
            methodology_notes.append(fallback_reason.strip())
    methodology_notes.append(
        "plat-agent salvaged market-study disk artifacts after the sibling returned empty stdout."
    )

    response_dict = {
        "contract_version": "v1",
        "status": "needs_analyst_input",
        "deal_slug": deal_root.name,
        "run_id": run_dir.name,
        "agent_name": "comp-finder",
        "payload": {
            "comps_by_cohort": comps_by_cohort,
            "comps_relative": f"outputs/{run_dir.name}/comps/comps.json",
            "methodology_notes": methodology_notes,
        },
        "artifacts": [
            {
                "relative_path": f"outputs/{run_dir.name}/comps/comps.json",
                "kind": "json",
                "description": "Lifecycle comps artifact salvaged from market-study disk outputs.",
            },
            {
                "relative_path": f"outputs/{run_dir.name}/market_study/comps_cohort_grouped.json",
                "kind": "json",
                "description": "Cohort-grouped comp set written by market-study-agent.",
            },
            {
                "relative_path": f"outputs/{run_dir.name}/market_study/_provenance.json",
                "kind": "json",
                "description": "Comp-finder provenance written by market-study-agent.",
            },
        ],
        "provenance": [
            {
                "source": "market-study-agent disk artifact salvage",
                "locator": f"outputs/{run_dir.name}/market_study/comps_cohort_grouped.json",
                "note": (
                    "Sibling returned empty stdout; plat-agent recovered the latest market-study artifacts from disk."
                    + (f" stderr log: {log_path}" if log_path else "")
                ),
            }
        ],
        "error": None,
        "sanity_flags": ["salvaged_from_disk_after_empty_stdout"],
    }
    envelope_path = market_study_dir / "_response_envelope.json"
    envelope_path.write_text(json.dumps(response_dict, indent=2), encoding="utf-8")
    return BridgeResponseV1.model_validate(response_dict)


def dispatch_sibling_agent(repo, request, **kwargs):
    """Dispatch via the sibling bridge.

    The market-study workflow owns its own bootstrapping and output creation.
    CompsStep should route through the sibling agent contract rather than
    short-circuiting into repo-local ETL helpers.
    """
    return _dispatch_sibling_agent(repo, request, **kwargs)


def _build_comp_finder_request(
    canonical: dict,
    *,
    run_dir: Path | None = None,
    deal_root: str | None = None,
) -> CompFinderRequest:
    """Translate `canonical_deal.json` into a CompFinderRequest payload.

    Extracts subject address, market slug, and a unit_mix_summary aggregating
    `unit_cohorts[]`. Raises ValueError when either required §2.1 metadata
    field (address, market) is missing — the lifecycle treats this as a
    comps hard error (CompsStep.run translates the ValueError per §4.3).
    """
    metadata = canonical.get("metadata") or {}
    address = metadata.get("address")
    if not address:
        raise ValueError(
            "canonical_deal.json missing required metadata.address; "
            "intake should have populated it. See spec §2.1 required fields."
        )
    market = metadata.get("market")
    if not market:
        raise ValueError(
            "canonical_deal.json missing required metadata.market; "
            "comp-finder cannot resolve a metro config without it."
        )
    subject_name = _extract_subject_name(canonical, run_dir)
    subject_units = sum(int(cohort.get("unit_count") or 0) for cohort in canonical.get("unit_cohorts") or [])
    unit_mix_summary: list[dict] = []
    for cohort in canonical.get("unit_cohorts") or []:
        unit_mix_summary.append(
            {
                "bedrooms": cohort.get("bedrooms"),
                "bathrooms": cohort.get("bathrooms"),
                "sqft": cohort.get("sqft"),
                "count": cohort.get("unit_count"),
            }
        )
    om_comp_seed, om_source_relative = _extract_om_comp_seed(run_dir, deal_root or "")
    coverage_signal = _build_comp_coverage_signal(
        unit_mix_summary=unit_mix_summary,
        om_comp_seed=om_comp_seed,
    )
    bootstrap_plan = _build_market_study_bootstrap_plan(
        canonical=canonical,
        run_dir=run_dir,
        deal_root=deal_root or "",
        subject_name=subject_name,
        property_address=address,
        market=market,
    )
    return CompFinderRequest(
        coverage_signal=coverage_signal,
        bootstrap_plan=bootstrap_plan,
        subject_name=subject_name,
        property_address=address,
        subject_units=subject_units or None,
        market=market,
        unit_mix_summary=unit_mix_summary,
        om_comp_seed=om_comp_seed,
        om_source_relative=om_source_relative,
    )


def _build_bridge_request(
    canonical: dict,
    deal_slug: str,
    run_id: str,
    deal_root: str,
    *,
    run_dir: Path | None = None,
) -> BridgeRequestV1:
    cf_request = _build_comp_finder_request(
        canonical,
        run_dir=run_dir,
        deal_root=deal_root,
    )
    return BridgeRequestV1(
        contract_version="v1",
        deal_slug=deal_slug,
        run_id=run_id,
        deal_root=deal_root,
        agent_name="comp-finder",
        payload=cf_request.model_dump(mode="json"),
    )


def _resolve_deal_root(run_dir: Path, state: LifecycleState) -> Path:
    """run_dir is `<deal_root>/outputs/<run_id>/`; deal_root is two levels up."""
    return run_dir.parent.parent


def _resolve_comps_artifact_path(
    response: BridgeResponseV1, deal_root: Path, run_dir: Path
) -> Path:
    """Resolve the on-disk path of the §2.2 comps artifact.

    Per the comp-finder.md sibling contract, the agent emits TWO artifacts:
    a cohort-grouped envelope at `market_study/comps_cohort_grouped.json`
    (referenced by `payload.comps_relative` in CompFinderResponse) AND the
    lifecycle §2.2 `comps.json` at `comps/comps.json`. The lifecycle §2.2
    schema (CompsArtifact) lives at the second location.

    We prefer the comp-finder envelope's `comps_relative` only when it
    points at a `comps.json` file (i.e. matches the §2.2 shape). Otherwise
    we fall back to the canonical run-scoped path `comps/comps.json` —
    that is the path the lifecycle spec §2.2 + downstream judgment.py /
    memo.py / crm.py all read from.
    """
    payload = response.payload or {}
    rel = payload.get("comps_relative") if isinstance(payload, dict) else None
    if isinstance(rel, str) and rel.endswith("comps.json"):
        # The sibling pointed `comps_relative` at the §2.2 file directly.
        # Resolve under deal_root, not run_dir — the contract path is
        # always relative to deal_root.
        candidate = deal_root / rel
        if candidate.exists():
            return candidate
    return run_dir / "comps" / "comps.json"


def _interpret_none_payload(
    response: BridgeResponseV1,
    run_dir: Path,
    canonical_path: Path,
) -> StepResult:
    """V1.4 graceful degradation — comp-finder returned payload=None.

    Mirrors the IntakeStep handler: emit empty comps.json fallback +
    structured blocker so judgment can run OM-only with comps_unavailable.
    """
    comps_dir = run_dir / "comps"
    input_hash = compute_input_hash([canonical_path])
    err = response.error
    err_message = (
        err.message if err is not None
        else f"comp-finder returned status={response.status} with payload=None"
    )
    return _emit_empty_comps_fallback(
        comps_dir=comps_dir,
        input_hash=input_hash,
        error_code="empty_payload_response",
        error_message=err_message,
        log_path=None,
        blocker_id="empty_payload_response",
    )


def _interpret_response(
    response: BridgeResponseV1,
    run_dir: Path,
    canonical_path: Path,
    deal_root: Path,
    request_payload: CompFinderRequest | None = None,
) -> StepResult:
    """Translate sibling response + on-disk artifact into a StepResult.

    Decision tree:
      1. response.status == 'error' → StepResult(error) per §4.3 alt path
         (judgment continues with comps_unavailable=true).
      2. comps.json missing → StepResult(error) — agent lied about success.
      3. comps.json fails CompsArtifact schema validation → StepResult(error).
      4. comps.json valid but degraded (1-2 comps, or any comp lacks
         unit_types[]) → StepResult(blocked) with §4.3 blocker entry.
      5. Otherwise → StepResult(ok). Write _provenance.json + _complete.
    """
    comps_dir = run_dir / "comps"
    comps_dir.mkdir(parents=True, exist_ok=True)
    input_hash = compute_input_hash([canonical_path])

    # 0. V1.4 — None payload short-circuit. Agent ran but produced no
    # usable body; emit graceful fallback before any payload-shape work.
    if response.payload is None and response.status != "error":
        return _interpret_none_payload(response, run_dir, canonical_path)

    # 1. Sibling-side error (e.g. all scrapers 403'd; no config for market).
    if response.status == "error":
        # response.error may be a BridgeError, a dict (from model_copy), or None.
        err = response.error
        if err is None:
            err_code = "unknown"
            err_message = ""
        elif isinstance(err, dict):
            err_code = err.get("code", "unknown")
            err_message = err.get("message", "")
        else:
            err_code = err.code
            err_message = err.message
        write_provenance(
            comps_dir,
            input_hash=input_hash,
            contract_version=CONTRACT_VERSION,
            status="error",
            extra={
                "error_code": err_code,
                "error_message": err_message,
            },
        )
        # No _complete — step must rerun on --resume.
        return StepResult(
            status="error",
            error_message=(
                f"comp-finder reported error: "
                f"{err_message if err_message else 'no detail'}"
            ),
        )

    # 2. comps.json must exist on disk. Resolve via the response's
    # comps_relative when it points at a `comps.json`, else the canonical
    # run-scoped `comps/comps.json` (per spec §2.2).
    payload = response.payload or {}
    has_comps_relative = (
        isinstance(payload, dict)
        and isinstance(payload.get("comps_relative"), str)
    )
    comps_file = _resolve_comps_artifact_path(response, deal_root, run_dir)
    # Mirror the resolved file into run_dir/comps/comps.json so downstream
    # consumers (judgment.py, memo.py, crm.py) read from the stable path.
    canonical_local = comps_dir / "comps.json"
    if comps_file.exists() and comps_file.resolve() != canonical_local.resolve():
        canonical_local.write_bytes(comps_file.read_bytes())
    if not canonical_local.exists():
        # Willow Court regression — when the comp-finder claims status=ok
        # but the response payload is missing comps_relative OR the
        # referenced file doesn't exist, fall back gracefully: write an
        # empty comps.json with the `comps_dispatch_failed` sanity flag
        # and a blocker so judgment can run OM-only with comps_unavailable.
        # _complete is still written so --resume sees the step as
        # committed (the operator can re-run comps in isolation).
        reason = (
            "comp-finder response missing comps_relative payload field"
            if not has_comps_relative
            else f"comp-finder claimed status={response.status} but no comps.json on disk at {comps_file}"
        )
        return _emit_empty_comps_fallback(
            comps_dir=comps_dir,
            input_hash=input_hash,
            error_code="missing_artifact",
            error_message=reason,
            log_path=None,
        )
    comps_file = canonical_local

    # 3. Schema validate per §2.2.
    try:
        artifact = CompsArtifact.model_validate(json.loads(comps_file.read_text()))
    except (json.JSONDecodeError, ValidationError) as exc:
        write_provenance(
            comps_dir,
            input_hash=input_hash,
            contract_version=CONTRACT_VERSION,
            status="error",
            extra={"error_code": "schema_violation"},
        )
        return StepResult(
            status="error",
            error_message=(
                f"comps/comps.json failed §2.2 schema validation: {exc}"
            ),
        )

    # 4. Degraded coverage detection per spec §2.2 status mapping.
    blockers: list[BlockerItem] = []
    if len(artifact.comps) < 3:
        blockers.append(
            BlockerItem(
                step="comps",
                id="degraded_comp_set",
                description=(
                    f"Only {len(artifact.comps)} comp(s) returned; "
                    f"<3 triggers comps blocker per §2.2 + §4.3. "
                    f"Judgment proceeds with reduced confidence."
                ),
            )
        )
    comps_without_unit_types = [
        c.comp_id for c in artifact.comps if not c.unit_types
    ]
    if comps_without_unit_types:
        blockers.append(
            BlockerItem(
                step="comps",
                id="comps_missing_unit_types",
                description=(
                    f"Comps lacking unit_types[]: "
                    f"{', '.join(comps_without_unit_types)}. "
                    f"Per-cohort rent calibration cannot run for these."
                ),
            )
        )

    coverage_signal = request_payload.coverage_signal if request_payload is not None else None
    if coverage_signal is not None and coverage_signal.requires_universe_expansion:
        if len(artifact.comps) < coverage_signal.minimum_total_comp_count:
            blockers.append(
                BlockerItem(
                    step="comps",
                    id="thin_comp_universe",
                    description=(
                        f"Broker OM seed was thin ({coverage_signal.broker_seed_comp_count} comp(s)); "
                        f"expanded comp set still only has {len(artifact.comps)} comp(s). "
                        f"Need at least {coverage_signal.minimum_total_comp_count} credible nearby comps "
                        f"before trusting market-rent support."
                    ),
                )
            )

        if coverage_signal.dominant_bedrooms is not None:
            dominant_label = (
                "Studio"
                if coverage_signal.dominant_bedrooms == 0
                else f"{coverage_signal.dominant_bedrooms}BR"
            )
            dominant_coverage = 0
            for comp in artifact.comps:
                if any(
                    _unit_type_matches_bedrooms(
                        unit_type.unit_type,
                        coverage_signal.dominant_bedrooms,
                    )
                    for unit_type in comp.unit_types
                ):
                    dominant_coverage += 1
            if dominant_coverage < coverage_signal.minimum_dominant_cohort_comp_count:
                blockers.append(
                    BlockerItem(
                        step="comps",
                        id="thin_dominant_cohort_comp_coverage",
                        description=(
                            f"Expanded comp set only has {dominant_coverage} comp(s) with {dominant_label} evidence "
                            f"for dominant subject cohort {coverage_signal.dominant_cohort_key or dominant_label}. "
                            f"Need at least {coverage_signal.minimum_dominant_cohort_comp_count} before trusting a rent premium."
                        ),
                    )
                )

    final_status = "ok" if not blockers else "blocked"

    # 5. Write provenance + _complete on validated path (ok or blocked).
    write_provenance(
        comps_dir,
        input_hash=input_hash,
        contract_version=CONTRACT_VERSION,
        status=final_status,
        extra={
            "comp_count": len(artifact.comps),
            "as_of": artifact.as_of,
            "blocker_count": len(blockers),
            "broker_seed_comp_count": (
                coverage_signal.broker_seed_comp_count if coverage_signal is not None else None
            ),
            "requested_universe_expansion": (
                coverage_signal.requires_universe_expansion if coverage_signal is not None else False
            ),
            "dominant_cohort_key": (
                coverage_signal.dominant_cohort_key if coverage_signal is not None else None
            ),
        },
    )
    write_complete_marker(
        comps_dir,
        step="comps",
        file_manifest=["comps.json", "_provenance.json"],
    )

    return StepResult(status=final_status, blockers=blockers)


def _emit_empty_comps_fallback(
    *,
    comps_dir: Path,
    input_hash: str,
    error_code: str,
    error_message: str,
    log_path: Path | None,
    blocker_id: str = "comps_dispatch_failed",
) -> StepResult:
    """Write `comps/{comps.json,_provenance.json,_complete}` for the
    failed-dispatch / missing-artifact path.

    The empty `comps.json` carries:
      * `comps: []` — downstream readers treat this as comps_unavailable
      * `comps_dispatch_failed: true` — sanity flag for log greppers
      * `subject` and `as_of` populated with placeholder values so the
        artifact remains self-describing

    A `_complete` marker is written so `is_satisfied()` can mark the
    step as committed; the actual missing-comps signal propagates via
    the returned blocker (which the runner merges into punchlist.json).
    """
    comps_dir.mkdir(parents=True, exist_ok=True)
    today = datetime.now(timezone.utc).date().isoformat()
    fallback_artifact = {
        "subject": {
            "metro_slug": "unknown",
            "address": "unknown",
        },
        "as_of": today,
        "comps": [],
        "comps_dispatch_failed": True,
        "fallback_reason": error_message[:500],
    }
    (comps_dir / "comps.json").write_text(json.dumps(fallback_artifact, indent=2))
    extra: dict = {
        "error_code": error_code,
        "error_message": error_message,
        "comps_dispatch_failed": True,
    }
    if log_path is not None:
        extra["stderr_log_path"] = str(log_path)
    write_provenance(
        comps_dir,
        input_hash=input_hash,
        contract_version=CONTRACT_VERSION,
        status="blocked",
        extra=extra,
    )
    write_complete_marker(
        comps_dir,
        step="comps",
        file_manifest=["comps.json", "_provenance.json"],
    )
    if blocker_id == "comps_dispatch_failed":
        description = (
            f"comp-finder dispatch failed: {error_message}. "
            + (f"See stderr log: {log_path}." if log_path else "")
        ).strip()
    else:
        # V1.3 pre-dispatch failures pass error_message as-is (it already
        # carries actionable resolution hints — e.g. CLI override syntax).
        description = error_message
    return StepResult(
        status="blocked",
        blockers=[
            BlockerItem(
                step="comps",
                id=blocker_id,
                description=description,
            )
        ],
    )


class CompsStep:
    """Lifecycle comps step — see module docstring."""

    name: StepName = _STEP_NAME

    def run(self, state: LifecycleState, run_dir: Path) -> StepResult:
        """Build CompFinderRequest, dispatch comp-finder, validate response."""
        canonical_path = run_dir / "intake" / "canonical_deal.json"
        if not canonical_path.exists():
            return StepResult(
                status="error",
                error_message=(
                    "intake/canonical_deal.json missing; CompsStep cannot run "
                    "without intake output. (Should have been caught by §4.3 "
                    "intake hard-error halt.)"
                ),
            )
        canonical = json.loads(canonical_path.read_text())

        try:
            request = _build_bridge_request(
                canonical,
                state.deal_slug,
                state.run_id,
                deal_root=str(_resolve_deal_root(run_dir, state)),
                run_dir=run_dir,
            )
        except ValueError as exc:
            # V1.3 — graceful pre-dispatch fallback. Previously a missing
            # metadata.address or metadata.market raised straight out of
            # CompsStep with no on-disk artifact, so V1.1's empty-fallback
            # path never fired and downstream judgment ran without a
            # consistent comps_unavailable signal. Emit the same empty
            # comps.json + provenance + _complete trio that the dispatch-
            # failure path writes, plus a pointed blocker that hints at
            # the --address / --market CLI overrides.
            comps_dir = run_dir / "comps"
            input_hash = compute_input_hash([canonical_path])
            return _emit_empty_comps_fallback(
                comps_dir=comps_dir,
                input_hash=input_hash,
                error_code="missing_subject_metadata",
                error_message=(
                    f"Cannot build CompFinderRequest: {exc} "
                    "Set canonical_deal.json metadata.address and "
                    "metadata.market, or use --address / --market CLI "
                    "overrides on `plat lifecycle`."
                ),
                log_path=None,
                blocker_id="missing_subject_metadata",
            )

        repo = SiblingRepo.from_env_or_default(
            "market-study-agent", "market-study-agent"
        )

        try:
            response = dispatch_sibling_agent(
                repo,
                request,
                payload_model=CompFinderResponse,
            )
        except DispatchError as exc:
            # Willow Court regression — when comp-finder dispatch fails
            # (subprocess error, timeout, unparseable response), the step
            # used to silently propagate StepResult(error) with no
            # provenance and no on-disk artifact. Downstream judgment
            # then ran OM-only with `comps_unavailable` but operators had
            # no log trail to debug the dispatch failure.
            #
            # New behavior: write a `comps/` dir with:
            #   - `comps.json` containing an empty `comps[]` and a
            #     `comps_dispatch_failed` sanity flag (so downstream
            #     consumers see a consistent empty-comp-set artifact)
            #   - `_provenance.json` carrying error_message + stderr log path
            #   - `_complete` marker so is_satisfied() can decide whether
            #     to retry (the missing-comps blocker propagates via the
            #     punchlist; analyst can rerun comps step in isolation)
            return self._handle_dispatch_failure(
                run_dir=run_dir,
                canonical_path=canonical_path,
                exc=exc,
            )

        # Map BridgeResponseV1.status to StepResult.status (Task B5+).
        return _interpret_response(
            response,
            run_dir,
            canonical_path,
            _resolve_deal_root(run_dir, state),
            request_payload=CompFinderRequest.model_validate(request.payload),
        )

    def _handle_dispatch_failure(
        self,
        *,
        run_dir: Path,
        canonical_path: Path,
        exc: DispatchError,
    ) -> StepResult:
        """Translate a DispatchError into a graceful empty-comp-set fallback.

        Willow Court regression — see the `dispatch_sibling_agent` call site
        for the full rationale. We never want a missing `comps/` dir on
        disk after the step "ran"; downstream judgment can run OM-only
        and the operator gets a clear log trail via _provenance.json +
        the captured stderr log.
        """
        canonical = json.loads(canonical_path.read_text(encoding="utf-8"))
        salvaged = _salvage_comp_finder_response_from_disk(
            run_dir=run_dir,
            canonical=canonical,
            log_path=exc.log_path,
        )
        if salvaged is not None:
            return _interpret_response(
                salvaged,
                run_dir,
                canonical_path,
                _resolve_deal_root(run_dir, LifecycleState(deal_slug=run_dir.parent.parent.name, run_id=run_dir.name)),
                request_payload=_build_comp_finder_request(
                    canonical,
                    run_dir=run_dir,
                    deal_root=str(_resolve_deal_root(run_dir, LifecycleState(deal_slug=run_dir.parent.parent.name, run_id=run_dir.name))),
                ),
            )

        comps_dir = run_dir / "comps"
        input_hash = compute_input_hash([canonical_path])
        log_path = exc.log_path  # set by sibling.py on timeout / non-zero exit
        return _emit_empty_comps_fallback(
            comps_dir=comps_dir,
            input_hash=input_hash,
            error_code="dispatch_error",
            error_message=str(exc),
            log_path=log_path,
        )

    def is_satisfied(self, state: LifecycleState, run_dir: Path) -> bool:
        """Default cache check via lifecycle.cache.is_satisfied."""
        canonical = run_dir / "intake" / "canonical_deal.json"
        if not canonical.exists():
            return False
        current_input_hash = compute_input_hash([canonical])
        return is_satisfied(
            state, run_dir, step=self.name, current_input_hash=current_input_hash
        )
