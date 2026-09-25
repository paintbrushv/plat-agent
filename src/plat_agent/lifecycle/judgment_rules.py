"""Per-field deterministic V1 rules for judgment.

Each rule function consumes intake (+ optional comps) and produces a
ValidatedField for one assumption category. Helpers (delta computation,
directional flag) are shared.

Spec reference: §2.3 (positioning.json schema), §1 V1 Defaults
(yellow_pct=0.15, red_pct=0.25 broker-claim thresholds).
"""

from __future__ import annotations

import os
import sys
from typing import Literal

from plat_agent.lifecycle.defaults import VALIDATION_THRESHOLDS


# Direction semantics:
#   "optimistic"     — higher OM than data is broker-optimistic (rent growth, exit cap inverse)
#   "underestimates" — lower OM than data is broker-underestimating (capex, opex)
DirectionWhenHigherOM = Literal["optimistic", "underestimates"]


def is_directional_optimistic(
    *,
    data_derived: float | None,
    om_claimed: float | None,
    direction_when_higher_om: DirectionWhenHigherOM,
) -> bool:
    """Return True iff the OM claim is directionally unfavorable to the GP."""
    if data_derived is None or om_claimed is None:
        return False
    if direction_when_higher_om == "optimistic":
        return om_claimed > data_derived
    # "underestimates"
    return om_claimed < data_derived


def compute_delta_severity(
    *,
    data_derived: float | None,
    om_claimed: float | None,
) -> str:
    """Compute the threshold-only severity (green/yellow/red).

    Independent of direction — both signs of a 30% delta are "red". This is
    the spec §1 V1 Defaults band that downstream counts/dashboards must
    preserve, even when the editorial `delta_flag` is directional.
    """
    if data_derived is None or om_claimed is None:
        return "green"
    if data_derived == 0.0:
        return "green"
    delta_pct = abs(om_claimed - data_derived) / abs(data_derived)
    yellow = VALIDATION_THRESHOLDS["yellow_pct"]
    red = VALIDATION_THRESHOLDS["red_pct"]
    if delta_pct < yellow:
        return "green"
    if delta_pct >= red:
        return "red"
    return "yellow"


def compute_delta_flag(
    *,
    data_derived: float | None,
    om_claimed: float | None,
    direction_when_higher_om: DirectionWhenHigherOM,
    prefer_directional: bool = False,
) -> str:
    """Compute delta_flag (editorial label) for a ValidatedField.

    Threshold logic (per VALIDATION_THRESHOLDS) is identical to
    :func:`compute_delta_severity`, but with two extra editorial overrides:

    If `prefer_directional=True` AND the OM is directionally unfavorable AND the
    delta would be yellow or red, the editorial directional flag wins:
      - "broker_optimistic"     when higher OM is optimistic (rent_growth, exit_cap inverse)
      - "broker_underestimates" when lower OM is underestimating (capex)

    Severity (green/yellow/red) is preserved separately on
    ``ValidatedField.delta_severity`` regardless of the directional override.
    """
    severity = compute_delta_severity(
        data_derived=data_derived, om_claimed=om_claimed,
    )
    if severity == "green":
        return "green"
    if prefer_directional and is_directional_optimistic(
        data_derived=data_derived,
        om_claimed=om_claimed,
        direction_when_higher_om=direction_when_higher_om,
    ):
        return "broker_optimistic" if direction_when_higher_om == "optimistic" else "broker_underestimates"
    return severity


from plat_agent.lifecycle.judgment import PositioningClass


VALUE_ADD_RENT_GAP_THRESHOLD = 0.10  # 10% gap → renovation thesis viable


def _avg_subject_in_place_rent(intake: dict) -> float | None:
    property_summary = ((intake.get("metadata") or {}).get("property_summary") or {})
    house_box = property_summary.get("house_box_score") or {}
    floorplans = house_box.get("floorplans") or []
    weighted_total = 0.0
    total_units = 0
    for floorplan in floorplans:
        try:
            rent = float(
                floorplan.get("avg_in_place_rent")
                or floorplan.get("avg_rent")
                or 0
            )
            units = int(floorplan.get("units") or 0)
        except (TypeError, ValueError):
            continue
        if rent > 0 and units > 0:
            weighted_total += rent * units
            total_units += units
    if total_units > 0:
        return weighted_total / total_units

    cohorts = intake.get("unit_cohorts", []) or []
    def _cohort_rent(cohort: dict) -> float | None:
        for key in ("in_place_rent", "initial_inplace_rent", "target_monthly_rent", "market_rent"):
            value = cohort.get(key)
            if value:
                return value
        return None

    rents = [_cohort_rent(c) for c in cohorts if _cohort_rent(c)]
    if not rents:
        return None
    # Weight by unit_count when present
    weighted_total = 0.0
    total_units = 0
    for c in cohorts:
        rent = _cohort_rent(c)
        units = c.get("unit_count", 0) or 0
        if rent and units:
            weighted_total += rent * units
            total_units += units
    if total_units > 0:
        return weighted_total / total_units
    return sum(rents) / len(rents)


def _avg_comp_effective_rent(comps: dict | None) -> float | None:
    if not comps:
        return None
    rents: list[float] = []
    for comp in comps.get("comps", []) or []:
        for ut in comp.get("unit_types", []) or []:
            rent = ut.get("effective_rent")
            if rent is None:
                rent = ut.get("face_rent")
            if rent is None and ut.get("rent_psf") and ut.get("sqft"):
                rent = float(ut["rent_psf"]) * float(ut["sqft"])
            if rent:
                rents.append(rent)
    if not rents:
        return None
    return sum(rents) / len(rents)


def classify_positioning(intake: dict, comps: dict | None) -> PositioningClass:
    """V1 positioning rule: rent gap → value_add vs stabilized.

    Returns a PositioningClass. When comps are unavailable we cannot detect
    a rent gap, so default to "stabilized" with lower confidence (judgment
    cannot tell the difference between a stabilized property and a value-add
    one without comp data).
    """
    subject_rent = _avg_subject_in_place_rent(intake)
    comp_rent = _avg_comp_effective_rent(comps)

    if subject_rent is None or comp_rent is None:
        return PositioningClass(
            value="stabilized",
            confidence=0.45,
            rationale=(
                "No comp rent data available — defaulting to stabilized "
                "classification at reduced confidence. Lifecycle should treat "
                "comps_unavailable=true and recommendation=NEEDS_DATA."
            ),
        )

    rent_gap_pct = (comp_rent - subject_rent) / subject_rent
    if rent_gap_pct >= VALUE_ADD_RENT_GAP_THRESHOLD:
        return PositioningClass(
            value="value_add",
            confidence=0.85,
            rationale=(
                f"Comp avg effective rent ${comp_rent:,.0f} is "
                f"{rent_gap_pct * 100:.1f}% above subject in-place rent "
                f"${subject_rent:,.0f}; renovation thesis appears viable."
            ),
        )
    if rent_gap_pct < 0:
        return PositioningClass(
            value="stabilized",
            confidence=0.75,
            rationale=(
                f"Comp avg effective rent ${comp_rent:,.0f} is "
                f"{abs(rent_gap_pct) * 100:.1f}% below subject in-place rent "
                f"${subject_rent:,.0f}; classifying as stabilized."
            ),
        )
    return PositioningClass(
        value="stabilized",
        confidence=0.75,
        rationale=(
            f"Comp avg effective rent ${comp_rent:,.0f} is only "
            f"{rent_gap_pct * 100:.1f}% above subject in-place rent "
            f"${subject_rent:,.0f}; classifying as stabilized."
        ),
    )


from plat_agent.lifecycle.judgment import ValidatedField


# GP heuristic: $1 of monthly rent lift requires ~$120 of unit-interior capex
# (kitchens/baths/floors). Tunable in V2 by submarket.
RENT_UPLIFT_TO_CAPEX_MULTIPLE = 120.0
STABILIZED_TURN_CAPEX_PER_UNIT = 3500.0


def _om_capex_per_unit(intake: dict) -> float | None:
    capex = intake.get("capex_assumptions", {}) or {}
    val = capex.get("renovation_cost_per_unit")
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def compute_capex_per_unit(
    intake: dict,
    comps: dict | None,
    *,
    positioning_value: str,
) -> ValidatedField | None:
    """V1 capex rule. Returns None when subject rent data is missing."""
    om = _om_capex_per_unit(intake)

    if positioning_value != "value_add":
        # Stabilized — small turn-cost only. No data-derived from rent gap.
        return ValidatedField(
            data_derived=STABILIZED_TURN_CAPEX_PER_UNIT,
            om_claimed=om,
            selected=max(STABILIZED_TURN_CAPEX_PER_UNIT, om or 0.0),
            confidence=0.7,
            delta_flag=compute_delta_flag(
                data_derived=STABILIZED_TURN_CAPEX_PER_UNIT,
                om_claimed=om,
                direction_when_higher_om="underestimates",
                prefer_directional=True,
            ),
            delta_severity=compute_delta_severity(
                data_derived=STABILIZED_TURN_CAPEX_PER_UNIT,
                om_claimed=om,
            ),
            rationale="Stabilized positioning: turn-cost only.",
        )

    subject_rent = _avg_subject_in_place_rent(intake)
    comp_rent = _avg_comp_effective_rent(comps)
    if subject_rent is None or comp_rent is None:
        return None
    rent_gap_monthly = max(0.0, comp_rent - subject_rent)
    data_derived = rent_gap_monthly * RENT_UPLIFT_TO_CAPEX_MULTIPLE

    flag = compute_delta_flag(
        data_derived=data_derived,
        om_claimed=om,
        direction_when_higher_om="underestimates",
        prefer_directional=True,
    )
    severity = compute_delta_severity(data_derived=data_derived, om_claimed=om)

    # Conservative selection: take max of data-derived and OM (under-budgeting kills deals).
    selected = max(data_derived, om or 0.0)

    return ValidatedField(
        data_derived=data_derived,
        om_claimed=om,
        selected=selected,
        confidence=0.7,
        delta_flag=flag,
        delta_severity=severity,
        rationale=(
            f"Rent gap ${rent_gap_monthly:,.0f}/mo × ${RENT_UPLIFT_TO_CAPEX_MULTIPLE:.0f} = "
            f"${data_derived:,.0f}/unit data-derived capex."
        ),
    )


VALUE_ADD_DEFAULT_PACE_UNITS_PER_MONTH = 5.0
DEFAULT_RENT_GROWTH = 0.03  # 3% baseline when no submarket data
DEFAULT_EXIT_CAP = 0.06     # 6% baseline


def compute_renovation_pace(
    intake: dict,
    comps: dict | None,
    *,
    positioning_value: str,
) -> ValidatedField | None:
    """V1 renovation pace rule. Per spec §2.3: value_add deals get 5 units/month default.

    V2 will use comp absorption signals (`units_available` velocity in comps);
    V1 simply uses the GP heuristic default and surfaces it as a ValidatedField
    so V2 can swap in without contract change.
    """
    if positioning_value == "value_add":
        return ValidatedField(
            data_derived=VALUE_ADD_DEFAULT_PACE_UNITS_PER_MONTH,
            om_claimed=None,
            selected=VALUE_ADD_DEFAULT_PACE_UNITS_PER_MONTH,
            confidence=0.65,
            delta_flag="green",
            delta_severity="green",
            rationale="V1 default: 5 units/month renovation pace for value-add deals.",
        )
    return ValidatedField(
        data_derived=0.0,
        om_claimed=None,
        selected=0.0,
        confidence=0.85,
        delta_flag="green",
        delta_severity="green",
        rationale="Stabilized positioning: no renovation pace.",
    )


def _om_rent_growth(intake: dict) -> float | None:
    g = intake.get("growth_assumptions", {}) or {}
    val = g.get("rent_growth")
    return float(val) if val is not None else None


def _om_exit_cap(intake: dict) -> float | None:
    e = intake.get("exit_assumptions", {}) or {}
    val = e.get("exit_cap_rate")
    return float(val) if val is not None else None


def _comp_submarket_rent_growth(comps: dict | None) -> float | None:
    if not comps:
        return None
    agg = comps.get("submarket_aggregates", {}) or {}
    val = agg.get("rent_growth_trailing_12mo")
    return float(val) if val is not None else None


def _avg_comp_cap_rate(comps: dict | None) -> float | None:
    if not comps:
        return None
    rates = [c.get("cap_rate_est") for c in comps.get("comps", []) or [] if c.get("cap_rate_est")]
    if not rates:
        return None
    return sum(rates) / len(rates)


def compute_rent_growth(intake: dict, comps: dict | None) -> ValidatedField | None:
    """V1 rent growth rule. Prefers submarket trailing-12mo, falls back to 3%."""
    data_derived = _comp_submarket_rent_growth(comps)
    om = _om_rent_growth(intake)

    if data_derived is None:
        # Default fallback at lower confidence
        selected = om if om is not None else DEFAULT_RENT_GROWTH
        # Conservative: cap OM at default if OM > default
        if om is not None and om > DEFAULT_RENT_GROWTH:
            selected = DEFAULT_RENT_GROWTH
        return ValidatedField(
            data_derived=None,
            om_claimed=om,
            selected=selected,
            confidence=0.45,
            delta_flag="green" if om is None else compute_delta_flag(
                data_derived=DEFAULT_RENT_GROWTH, om_claimed=om,
                direction_when_higher_om="optimistic", prefer_directional=True,
            ),
            delta_severity=compute_delta_severity(
                data_derived=DEFAULT_RENT_GROWTH, om_claimed=om,
            ) if om is not None else "green",
            rationale=(
                "No submarket rent-growth data; using V1 default 3% "
                "(OM claim capped to default if higher)."
            ),
        )

    flag = compute_delta_flag(
        data_derived=data_derived, om_claimed=om,
        direction_when_higher_om="optimistic", prefer_directional=True,
    )
    severity = compute_delta_severity(data_derived=data_derived, om_claimed=om)
    # Conservative: pick the lower of data-derived vs OM
    selected = min(data_derived, om) if om is not None else data_derived
    return ValidatedField(
        data_derived=data_derived,
        om_claimed=om,
        selected=selected,
        confidence=0.75,
        delta_flag=flag,
        delta_severity=severity,
        rationale=f"Submarket trailing-12mo rent growth: {data_derived * 100:.1f}%.",
    )


def compute_exit_cap(intake: dict, comps: dict | None) -> ValidatedField | None:
    """V1 exit cap rule. Prefers avg comp cap_rate_est, falls back to 6%."""
    data_derived = _avg_comp_cap_rate(comps)
    om = _om_exit_cap(intake)

    if data_derived is None:
        selected = om if om is not None else DEFAULT_EXIT_CAP
        # Conservative: floor exit cap at default if OM is lower
        if om is not None and om < DEFAULT_EXIT_CAP:
            selected = DEFAULT_EXIT_CAP
        return ValidatedField(
            data_derived=None,
            om_claimed=om,
            selected=selected,
            confidence=0.45,
            # exit cap: LOWER om = higher implied value = broker_optimistic
            delta_flag="green" if om is None else compute_delta_flag(
                data_derived=DEFAULT_EXIT_CAP, om_claimed=om,
                direction_when_higher_om="underestimates",  # lower OM is what "optimistic" looks like for cap rates
                prefer_directional=False,  # we re-flag below for sign
            ),
            delta_severity=compute_delta_severity(
                data_derived=DEFAULT_EXIT_CAP, om_claimed=om,
            ) if om is not None else "green",
            rationale="No comp cap-rate data; using V1 default 6%.",
        )

    # Sign convention: a LOWER om vs data is broker-optimistic for exit cap
    # (lower exit cap → higher reversion value → optimistic).
    base_flag = compute_delta_flag(
        data_derived=data_derived, om_claimed=om,
        direction_when_higher_om="underestimates",  # treat sign mapping inverted
        prefer_directional=False,
    )
    severity = compute_delta_severity(data_derived=data_derived, om_claimed=om)
    final_flag: str = base_flag
    if om is not None and om < data_derived and base_flag in {"yellow", "red"}:
        final_flag = "broker_optimistic"

    # Conservative: pick the higher of data-derived vs OM (higher cap = lower exit value)
    selected = max(data_derived, om) if om is not None else data_derived
    return ValidatedField(
        data_derived=data_derived,
        om_claimed=om,
        selected=selected,
        confidence=0.7,
        delta_flag=final_flag,  # type: ignore[arg-type]
        delta_severity=severity,
        rationale=f"Avg comp cap rate: {data_derived * 100:.2f}%.",
    )


from typing import Any

from plat_agent.lifecycle.defaults import (
    DEFAULT_LEVERAGE_V1,
    LEVERAGE_SOURCE_AGENCY_DSCR,
    LEVERAGE_SOURCE_V1_HARDCODED,
)


_UNDERWRITING_PATH = os.environ.get("UNDERWRITING_ENGINE_PATH")
if _UNDERWRITING_PATH and _UNDERWRITING_PATH not in sys.path:
    sys.path.insert(0, _UNDERWRITING_PATH)

try:
    from engine.modules.debt import compute_agency_loan_terms as _compute_agency_loan_terms  # type: ignore
    _AGENCY_AVAILABLE = True
except ImportError:
    _compute_agency_loan_terms = None
    _AGENCY_AVAILABLE = False


def _ensure_agency_sizer() -> bool:
    """Best-effort late bind so tests/resumed processes can set the env var after import."""
    global _AGENCY_AVAILABLE, _compute_agency_loan_terms
    if _AGENCY_AVAILABLE and _compute_agency_loan_terms is not None:
        return True
    underwriting_path = os.environ.get("UNDERWRITING_ENGINE_PATH")
    if underwriting_path and underwriting_path not in sys.path:
        sys.path.insert(0, underwriting_path)
    try:
        from engine.modules.debt import compute_agency_loan_terms as imported_sizer  # type: ignore
    except ImportError:
        return False
    _compute_agency_loan_terms = imported_sizer
    _AGENCY_AVAILABLE = True
    return True


def _t12_noi(intake: dict) -> float | None:
    """Best-effort extraction of T12 NOI from canonical intake.

    Looks at the standard slots produced by mfu's T12 parser
    (engine.ingest.t12_parser): opex_table.t12_noi, broker_claims.year_1_noi,
    or projected_noi from the OM. Returns None when none present.
    """
    candidates = [
        ("opex_table", "t12_noi"),
        ("broker_claims", "t12_noi"),
        ("broker_claims", "year_1_noi"),
        ("metadata", "t12_noi"),
    ]
    for path in candidates:
        cur: Any = intake
        ok = True
        for k in path:
            if not isinstance(cur, dict) or k not in cur:
                ok = False
                break
            cur = cur[k]
        if ok and cur is not None:
            try:
                return float(cur)
            except (TypeError, ValueError):
                continue
    return None


def _projected_noi(intake: dict) -> float | None:
    """Year-1 projected NOI from broker claims (OM-asserted) or T12 fallback."""
    candidates = [
        ("broker_claims", "year_1_noi"),
        ("broker_claims", "projected_noi"),
        ("metadata", "projected_noi"),
    ]
    for path in candidates:
        cur: Any = intake
        ok = True
        for k in path:
            if not isinstance(cur, dict) or k not in cur:
                ok = False
                break
            cur = cur[k]
        if ok and cur is not None:
            try:
                return float(cur)
            except (TypeError, ValueError):
                continue
    return _t12_noi(intake)


def compute_leverage(intake: dict) -> dict[str, Any]:
    """V1.5 leverage rule.

    Path A (preferred) — agency DSCR-constrained sizing via mfu:
        When ``metadata.year_built`` (vintage) is present, call
        ``engine.modules.debt.compute_agency_loan_terms(vintage, t12_noi,
        projected_noi, purchase_price=...)`` and stamp ``source =
        agency_dscr_constrained`` on the returned leverage dict.

    Path B (fallback) — V1 hardcoded:
        When vintage is missing OR the mfu call raises, fall back to the
        original hardcoded ``DEFAULT_LEVERAGE_V1`` (source remains
        ``v1_hardcoded``). Confidence is capped at 0.6 downstream.

    The V2 plan adds a third tier (data-room debt matrix) at the top.
    """
    vintage = (intake.get("metadata") or {}).get("year_built")
    purchase_price = (intake.get("purchase_assumptions") or {}).get("purchase_price")

    # Path B: vintage missing → V1 hardcoded fallback
    if vintage is None:
        return dict(DEFAULT_LEVERAGE_V1)

    # Path A: try mfu agency sizing
    if not _ensure_agency_sizer():
        # mfu not importable — fall back
        return dict(DEFAULT_LEVERAGE_V1)

    try:
        t12 = _t12_noi(intake) or 0.0
        proj = _projected_noi(intake) or 0.0
        terms = _compute_agency_loan_terms(
            vintage=int(vintage),
            t12_noi=t12,
            projected_noi=proj,
            purchase_price=purchase_price,
        )
    except Exception:
        # Any sizing failure → fall back to V1 hardcoded (defensive: never
        # let mfu errors crash judgment).
        return dict(DEFAULT_LEVERAGE_V1)

    # Reshape to plat-agent's leverage block contract
    return {
        "ltv": float(terms.get("ltv", 0.0)),
        "rate": float(terms.get("rate", 0.0)),
        "amort_years": int(terms.get("amort_years", 30)),
        "io_months": int(terms.get("io_months", 24)),
        "term_years": int(terms.get("term_years", 7)),
        "loan_amount": float(terms.get("loan_amount", 0.0)),
        "sizing_method": terms.get("sizing_method", "dscr_constrained"),
        "rate_breakdown": terms.get("rate_breakdown", {}),
        "noi_used": float(terms.get("noi_used", 0.0)),
        "required_dscr": float(terms.get("required_dscr", 1.25)),
        "source": LEVERAGE_SOURCE_AGENCY_DSCR,
    }
