"""Rent-target validation against a comp set.

Pure deterministic validator — given the analyst's proposed target rents per
cohort plus a comp set (typically from market-study-agent's comp-finder),
returns per-cohort verdicts on whether the proposed rents sit inside,
above, or below the comp distribution's interquartile range.

Stub integration for the plat-agent ↔ market-study-agent rent-validation
surface defined in `contracts/domain/market_study.py`. Today it runs
inside plat-agent — but the function signature accepts and returns
contract types, so a future market-study-agent implementation can replace
this with a network round-trip without touching callers.
"""

from __future__ import annotations

from statistics import median

from .contracts.domain.market_study import (
    CompEntry,
    ProposedRent,
    RentValidationFinding,
    RentValidationRequest,
    RentValidationResponse,
    cohort_key,
)

# Below this comp count for a cohort, we refuse to issue a verdict.
MIN_COMPS_FOR_VERDICT = 3
# Above this, we report high confidence; in between, medium.
COMP_COUNT_HIGH_CONFIDENCE = 6


def validate_target_rents(request: RentValidationRequest) -> RentValidationResponse:
    """Validate each proposed rent against its cohort's comp distribution."""
    findings: list[RentValidationFinding] = []
    for proposed in request.proposed_rents:
        findings.append(_validate_one(proposed, request.comps_by_cohort))
    return RentValidationResponse(
        findings=findings,
        summary=_summary(findings),
    )


def _validate_one(
    proposed: ProposedRent,
    comps_by_cohort: dict[str, list[CompEntry]],
) -> RentValidationFinding:
    key = cohort_key(proposed.bedrooms, proposed.bathrooms, int(proposed.sqft))

    # Exact-key match preferred; fall back to (bedrooms, bathrooms) match
    # across all comp cohorts since exact sqft rarely lines up.
    comps = comps_by_cohort.get(key) or _fallback_comps(
        proposed.bedrooms, proposed.bathrooms, comps_by_cohort
    )
    n = len(comps)

    if n < MIN_COMPS_FOR_VERDICT:
        return RentValidationFinding(
            cohort_key=key,
            proposed_rent=proposed.proposed_target_monthly_rent,
            comp_count=n,
            verdict="insufficient_comps",
            confidence="none",
            message=(
                f"Only {n} comp(s) available for {proposed.bedrooms}BR/"
                f"{proposed.bathrooms}BA cohort — need at least "
                f"{MIN_COMPS_FOR_VERDICT} for a defensible verdict."
            ),
        )

    rents = sorted(c.asking_rent for c in comps)
    p25 = _percentile(rents, 0.25)
    p50 = median(rents)
    p75 = _percentile(rents, 0.75)
    proposed_rent = proposed.proposed_target_monthly_rent

    if proposed_rent < p25:
        verdict = "below_band"
        msg = (
            f"Proposed ${proposed_rent:,.0f}/mo is below the comp p25 "
            f"(${p25:,.0f}). Comps suggest more upside — verify rent "
            "assumptions are not stale."
        )
    elif proposed_rent > p75:
        verdict = "above_band"
        msg = (
            f"Proposed ${proposed_rent:,.0f}/mo is above the comp p75 "
            f"(${p75:,.0f}). Aggressive vs market — verify renovation "
            "scope justifies the premium."
        )
    else:
        verdict = "within_band"
        msg = (
            f"Proposed ${proposed_rent:,.0f}/mo is within the comp IQR "
            f"(${p25:,.0f}-${p75:,.0f}, median ${p50:,.0f})."
        )

    confidence = "high" if n >= COMP_COUNT_HIGH_CONFIDENCE else "medium"

    return RentValidationFinding(
        cohort_key=key,
        proposed_rent=proposed_rent,
        comp_count=n,
        comp_p25=p25,
        comp_median=p50,
        comp_p75=p75,
        verdict=verdict,
        confidence=confidence,
        message=msg,
    )


def _fallback_comps(
    bedrooms: int,
    bathrooms: float,
    comps_by_cohort: dict[str, list[CompEntry]],
) -> list[CompEntry]:
    """Pool comps across all cohort keys with the same (bedrooms, bathrooms).

    Sqft rarely matches exactly between subject and comps, so the exact
    cohort_key match is brittle. Pooling by bedrooms+bathrooms gives us a
    usable comp set in the common case where the analyst's subject sqft
    differs from any comp.
    """
    out: list[CompEntry] = []
    for comps in comps_by_cohort.values():
        for c in comps:
            if c.bedrooms == bedrooms and abs(c.bathrooms - bathrooms) < 0.01:
                out.append(c)
    return out


def _percentile(sorted_values: list[float], q: float) -> float:
    """Linear-interpolated percentile on a pre-sorted list."""
    if not sorted_values:
        raise ValueError("cannot compute percentile of empty list")
    if len(sorted_values) == 1:
        return sorted_values[0]
    pos = q * (len(sorted_values) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(sorted_values) - 1)
    frac = pos - lo
    return sorted_values[lo] + frac * (sorted_values[hi] - sorted_values[lo])


def _summary(findings: list[RentValidationFinding]) -> str:
    if not findings:
        return "No proposed rents to validate."
    counts = {"within_band": 0, "above_band": 0, "below_band": 0, "insufficient_comps": 0}
    for f in findings:
        counts[f.verdict] += 1
    parts = []
    if counts["within_band"]:
        parts.append(f"{counts['within_band']} within band")
    if counts["above_band"]:
        parts.append(f"{counts['above_band']} above p75")
    if counts["below_band"]:
        parts.append(f"{counts['below_band']} below p25")
    if counts["insufficient_comps"]:
        parts.append(f"{counts['insufficient_comps']} no verdict (insufficient comps)")
    return f"{len(findings)} cohort(s) validated: " + ", ".join(parts) + "."
