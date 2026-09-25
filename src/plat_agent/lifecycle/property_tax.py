"""Property-tax evidence resolution and analyst gate for lifecycle valuation."""

from __future__ import annotations

import math
import shlex
from copy import deepcopy
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping

from plat_agent.lifecycle.state import BlockerItem

PROPERTY_TAX_MILLAGE_QUESTION = (
    "What combined property-tax millage should be used? Enter mills per $1,000 "
    "of assessed value (for example, `25.31`)."
)

_POLICY_FIELD = "metadata.property_summary.property_tax_policy.millage_rate_mills"
_RATIO_FIELD = "metadata.property_summary.property_tax_policy.assessment_ratio"
_POLICY_KEYS = {
    "millage_rate_mills",
    "assessment_ratio",
    "source",
    "source_locator",
    "analyst_override",
}
_TAX_BLOCKER_IDS = {
    "missing_property_tax_millage",
    "invalid_property_tax_millage",
    "unsupported_property_tax_assessment_override",
}


@dataclass(frozen=True)
class PropertyTaxEvidenceCandidate:
    millage_rate_mills: Decimal
    source: str
    source_locator: str


@dataclass(frozen=True)
class PropertyTaxGateResult:
    canonical: dict[str, Any]
    policy: dict[str, Any] | None
    blocker: BlockerItem | None
    changed: bool


def _positive_decimal(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None
    if not parsed.is_finite() or parsed <= 0:
        return None
    serialized = float(parsed)
    if (
        not math.isfinite(serialized)
        or serialized <= 0
        or Decimal(str(serialized)) != parsed
    ):
        return None
    return parsed


def _validated_policy(value: object) -> dict[str, Any] | None:
    if not isinstance(value, Mapping) or set(value) != _POLICY_KEYS:
        return None
    mills = _positive_decimal(value.get("millage_rate_mills"))
    ratio = _positive_decimal(value.get("assessment_ratio"))
    source = value.get("source")
    locator = value.get("source_locator")
    analyst_override = value.get("analyst_override")
    if (
        mills is None
        or ratio is None
        or not isinstance(source, str)
        or not source.strip()
        or not isinstance(locator, str)
        or not locator.strip()
        or not isinstance(analyst_override, bool)
        or analyst_override is not (ratio != Decimal("1.00"))
    ):
        return None
    return {
        "millage_rate_mills": float(mills),
        "assessment_ratio": float(ratio),
        "source": source.strip(),
        "source_locator": locator.strip(),
        "analyst_override": analyst_override,
    }


def _policy_validation_error(value: object) -> str | None:
    if not isinstance(value, Mapping) or set(value) != _POLICY_KEYS:
        return "invalid_property_tax_millage"
    if _positive_decimal(value.get("millage_rate_mills")) is None:
        return "invalid_property_tax_millage"
    ratio = _positive_decimal(value.get("assessment_ratio"))
    analyst_override = value.get("analyst_override")
    if (
        ratio is None
        or not isinstance(analyst_override, bool)
        or analyst_override is not (ratio != Decimal("1.00"))
    ):
        return "unsupported_property_tax_assessment_override"
    source = value.get("source")
    locator = value.get("source_locator")
    if (
        not isinstance(source, str)
        or not source.strip()
        or not isinstance(locator, str)
        or not locator.strip()
    ):
        return "invalid_property_tax_millage"
    return None


def _assessment_ratio_source_locator(policy: Mapping[str, Any]) -> str | None:
    marker = "assessment_ratio="
    components = [
        component.strip()
        for component in str(policy["source_locator"]).split(";")
        if component.strip()
    ]
    for component in components:
        if not component.startswith(marker):
            continue
        source_locator = component.removeprefix(marker).strip()
        source, separator, locator = source_locator.partition(":")
        if separator and source.strip() and locator.strip():
            return f"{source.strip()}:{locator.strip()}"

    millage_sources = {
        "analyst",
        "county_tax_notice",
        "debt_guidance",
        "offering_memorandum",
    }
    ratio_locators_by_source: dict[str, set[str]] = {}
    for component in components:
        source, separator, locator = component.partition(":")
        source = source.strip()
        locator = locator.strip()
        if separator and source and locator and source not in millage_sources:
            ratio_locators_by_source.setdefault(source, set()).add(locator)
    if len(ratio_locators_by_source) != 1:
        return None
    source, locators = next(iter(ratio_locators_by_source.items()))
    return f"{source}:{' | '.join(sorted(locators))}"


def _property_summary(canonical: Mapping[str, Any]) -> Mapping[str, Any]:
    metadata = canonical.get("metadata") or {}
    if not isinstance(metadata, Mapping):
        return {}
    summary = metadata.get("property_summary") or {}
    return summary if isinstance(summary, Mapping) else {}


def _candidate_rows(canonical: Mapping[str, Any]) -> list[PropertyTaxEvidenceCandidate]:
    rows = _property_summary(canonical).get("property_tax_evidence_candidates") or []
    candidates: list[PropertyTaxEvidenceCandidate] = []
    if not isinstance(rows, list):
        return candidates
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        mills = _positive_decimal(row.get("millage_rate_mills"))
        source = row.get("source")
        locator = row.get("source_locator")
        if (
            mills is None
            or not isinstance(source, str)
            or not source.strip()
            or not isinstance(locator, str)
            or not locator.strip()
        ):
            continue
        candidates.append(
            PropertyTaxEvidenceCandidate(
                millage_rate_mills=mills,
                source=source.strip(),
                source_locator=locator.strip(),
            )
        )
    return candidates


def _unresolved_evidence_locations(
    canonical: Mapping[str, Any],
    candidates: list[PropertyTaxEvidenceCandidate],
) -> set[str]:
    summary = _property_summary(canonical)
    rows = summary.get("property_tax_evidence_candidates") or []
    unresolved: set[str] = set()
    if not isinstance(rows, list):
        unresolved.add("metadata.property_summary.property_tax_evidence_candidates")
    else:
        for index, row in enumerate(rows):
            if not isinstance(row, Mapping):
                unresolved.add(
                    "metadata.property_summary."
                    f"property_tax_evidence_candidates[{index}]"
                )
                continue
            mills = _positive_decimal(row.get("millage_rate_mills"))
            source = row.get("source")
            locator = row.get("source_locator")
            if (
                mills is not None
                and isinstance(source, str)
                and source.strip()
                and isinstance(locator, str)
                and locator.strip()
            ):
                continue
            unresolved.add(
                locator.strip()
                if isinstance(locator, str) and locator.strip()
                else (
                    "metadata.property_summary."
                    f"property_tax_evidence_candidates[{index}]"
                )
            )

    candidate_locations = {
        candidate.source_locator for candidate in candidates
    }
    extra = summary.get("property_tax_evidence_locations") or []
    if isinstance(extra, list):
        unresolved.update(
            item.strip()
            for item in extra
            if isinstance(item, str)
            and item.strip()
            and item.strip() not in candidate_locations
        )
    elif extra:
        unresolved.add("metadata.property_summary.property_tax_evidence_locations")
    return unresolved


def _evidence_locations(canonical: Mapping[str, Any]) -> list[str]:
    summary = _property_summary(canonical)
    candidates = _candidate_rows(canonical)
    locations = {
        candidate.source_locator for candidate in candidates
    }
    locations.update(_unresolved_evidence_locations(canonical, candidates))
    extra = summary.get("property_tax_evidence_locations") or []
    if isinstance(extra, list):
        locations.update(
            item.strip() for item in extra
            if isinstance(item, str) and item.strip()
        )
    return sorted(locations)


def render_property_tax_resume_command(
    data_room: Path,
    deal_slug: str,
    run_id: str,
) -> str:
    return (
        f"plat lifecycle {shlex.quote(str(data_room.resolve()))} "
        f"--resume {shlex.quote(f'{deal_slug}/{run_id}')} --millage-rate <mills>"
    )


def _blocker(
    *,
    blocker_id: str,
    canonical: Mapping[str, Any],
    data_room: Path,
    deal_slug: str,
    run_id: str,
    submitted_value: str | None = None,
) -> BlockerItem:
    if blocker_id == "invalid_property_tax_millage":
        description = (
            "The submitted or canonical property-tax millage is not a positive "
            "finite number; valuation has not run and the prior canonical policy "
            "is unchanged."
        )
        field = _POLICY_FIELD
        unit = "mills per $1,000 of assessed value"
        resolution_hint = render_property_tax_resume_command(
            data_room, deal_slug, run_id
        )
    elif blocker_id == "unsupported_property_tax_assessment_override":
        description = (
            "The canonical property-tax assessment ratio is invalid, unapproved, "
            "or lacks stable ratio provenance; valuation has not run and the "
            "canonical policy is unchanged."
        )
        field = _RATIO_FIELD
        unit = "decimal share of purchase price"
        resolution_hint = render_property_tax_resume_command(
            data_room, deal_slug, run_id
        )
    else:
        description = (
            "A current combined property-tax millage is required because labelled "
            "evidence is missing or conflicting; valuation has not run."
        )
        field = _POLICY_FIELD
        unit = "mills per $1,000 of assessed value"
        resolution_hint = render_property_tax_resume_command(
            data_room, deal_slug, run_id
        )
    return BlockerItem(
        step="intake",
        id=blocker_id,
        field=field,
        unit=unit,
        evidence_locations=_evidence_locations(canonical),
        submitted_value=submitted_value,
        description=description,
        resolution_hint=resolution_hint,
    )


def _materialize_policy(
    canonical: dict[str, Any],
    policy: dict[str, Any],
) -> PropertyTaxGateResult:
    existing = _property_summary(canonical).get("property_tax_policy")
    if existing == policy:
        return PropertyTaxGateResult(
            canonical=canonical,
            policy=policy,
            blocker=None,
            changed=False,
        )
    patched = deepcopy(canonical)
    summary = patched.setdefault("metadata", {}).setdefault(
        "property_summary", {}
    )
    summary["property_tax_policy"] = policy
    return PropertyTaxGateResult(
        canonical=patched,
        policy=policy,
        blocker=None,
        changed=True,
    )


def resolve_property_tax_gate(
    canonical: dict[str, Any],
    *,
    millage_rate: str | None,
    source_locator: str | None,
    data_room: Path,
    deal_slug: str,
    run_id: str,
) -> PropertyTaxGateResult:
    """Resolve analyst input/canonical/evidence by strict precedence.

    A supplied CLI value is authoritative only when valid. Invalid current input
    never falls through to a prior policy because doing so would silently ignore
    the analyst's attempted correction.
    """
    summary = _property_summary(canonical)
    has_existing_policy = "property_tax_policy" in summary
    existing_value = summary.get("property_tax_policy")
    existing = _validated_policy(existing_value) if has_existing_policy else None

    if millage_rate is not None:
        mills = _positive_decimal(millage_rate)
        if mills is None:
            return PropertyTaxGateResult(
                canonical=canonical,
                policy=None,
                blocker=_blocker(
                    blocker_id="invalid_property_tax_millage",
                    canonical=canonical,
                    data_room=data_room,
                    deal_slug=deal_slug,
                    run_id=run_id,
                    submitted_value=millage_rate,
                ),
                changed=False,
            )
        locator = source_locator or "plat lifecycle:--millage-rate"
        if existing is not None and existing["assessment_ratio"] != 1.0:
            ratio_source_locator = _assessment_ratio_source_locator(existing)
            if ratio_source_locator is None:
                return PropertyTaxGateResult(
                    canonical=canonical,
                    policy=None,
                    blocker=_blocker(
                        blocker_id=(
                            "unsupported_property_tax_assessment_override"
                        ),
                        canonical=canonical,
                        data_room=data_room,
                        deal_slug=deal_slug,
                        run_id=run_id,
                    ),
                    changed=False,
                )
            policy = {
                "millage_rate_mills": float(mills),
                "assessment_ratio": existing["assessment_ratio"],
                "source": "composite_evidence",
                "source_locator": (
                    f"millage={locator};"
                    f"assessment_ratio={ratio_source_locator}"
                ),
                "analyst_override": True,
            }
        else:
            policy = {
                "millage_rate_mills": float(mills),
                "assessment_ratio": 1.0,
                "source": "analyst",
                "source_locator": locator,
                "analyst_override": False,
            }
        return _materialize_policy(canonical, policy)
    if has_existing_policy and existing is None:
        validation_error = (
            _policy_validation_error(existing_value)
            or "invalid_property_tax_millage"
        )
        submitted_field = (
            "assessment_ratio"
            if validation_error
            == "unsupported_property_tax_assessment_override"
            else "millage_rate_mills"
        )
        return PropertyTaxGateResult(
            canonical=canonical,
            policy=None,
            blocker=_blocker(
                blocker_id=validation_error,
                canonical=canonical,
                data_room=data_room,
                deal_slug=deal_slug,
                run_id=run_id,
                submitted_value=(
                    str(existing_value.get(submitted_field))
                    if isinstance(existing_value, Mapping)
                    and submitted_field in existing_value
                    else None
                ),
            ),
            changed=False,
        )


    if existing is not None:
        return PropertyTaxGateResult(
            canonical=canonical,
            policy=existing,
            blocker=None,
            changed=False,
        )

    candidates = _candidate_rows(canonical)
    unresolved = _unresolved_evidence_locations(canonical, candidates)
    distinct = {candidate.millage_rate_mills for candidate in candidates}
    if candidates and not unresolved and len(distinct) == 1:
        mills = next(iter(distinct))
        source_locators = sorted(
            {
                f"{candidate.source}:{candidate.source_locator}"
                for candidate in candidates
            }
        )
        if len(candidates) == 1:
            source = candidates[0].source
            locator = candidates[0].source_locator
        else:
            source = "composite_evidence"
            locator = "; ".join(source_locators)
        policy = {
            "millage_rate_mills": float(mills),
            "assessment_ratio": 1.0,
            "source": source,
            "source_locator": locator,
            "analyst_override": False,
        }
        return _materialize_policy(canonical, policy)

    return PropertyTaxGateResult(
        canonical=canonical,
        policy=None,
        blocker=_blocker(
            blocker_id="missing_property_tax_millage",
            canonical=canonical,
            data_room=data_room,
            deal_slug=deal_slug,
            run_id=run_id,
        ),
        changed=False,
    )


def is_property_tax_blocker(blocker: BlockerItem) -> bool:
    return blocker.id in _TAX_BLOCKER_IDS
