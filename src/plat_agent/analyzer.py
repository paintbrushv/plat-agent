"""Deal analysis orchestration — the core Plat workflow.

analyze_deal() is the single entry point. It orchestrates calls to
plat-costmodel MCP tools and optionally runs the underwriting engine.

Workflow:
  1. Call estimate_property_from_model → property estimate + exterior CapEx
  2. For each unit type, call prepare_renovation_program_tool → ROI gate + bridge dict
  3. If base_deal_inputs provided and ROI passes:
     a. Map renovation_programs to canonical schema format
     b. Merge into base_deal_inputs
     c. Run underwriting engine → metrics
  4. Aggregate results → DealAnalysis

Rent assumptions are always provided by the analyst in DealInputs.
Plat never derives rent internally.
"""

from .contracts.domain.market_study import (
    ProposedRent, RentValidationFinding, RentValidationRequest,
)
from .costmodel_client import CostModelClient
from .decision import build_decision_summary
from .models import DealAnalysis, DealInputs, UnitTypeResult
from .rent_validation import validate_target_rents
from .sanity import compute_sanity_flags
from .schema_mapper import build_renovation_programs, merge_renovation_programs


def analyze_deal(
    inputs: DealInputs,
    client: CostModelClient | None = None,
    underwriting_client=None,
    comps_by_cohort: dict | None = None,
) -> DealAnalysis:
    """Run the full Plat deal analysis workflow.

    Args:
        inputs: Structured deal inputs from the analyst.
        client: CostModelClient instance. If None, uses default (spawns subprocess).
        underwriting_client: UnderwritingClient instance. If None and
            base_deal_inputs is provided, uses default.
        comps_by_cohort: Optional comp set keyed by canonical cohort_key
            (typically the comps_by_cohort field of a CompFinderResponse).
            When provided, each unit type's target_monthly_rent is validated
            against the comp distribution and any above_band findings are
            folded into decision_summary.binding_constraints.

    Returns:
        DealAnalysis with per-unit-type results, property totals,
        renovation_programs, and optionally underwriting metrics.
    """
    if client is None:
        client = CostModelClient()

    # Step 1: Property-level estimate (cost + exterior CapEx + risk flags)
    property_data = {
        "property_id": inputs.property_id,
        "total_units": inputs.total_units,
        "year_built": inputs.year_built,
        "property_class": inputs.property_class,
        "market": inputs.market,
        "building_type": inputs.building_type,
        "exterior_items": inputs.exterior_items,
        "unit_mix": [
            {
                "sqft": u.sqft,
                "bedrooms": u.bedrooms,
                "bathrooms": u.bathrooms,
                "count": u.count,
                "scope_level": u.scope_level,
                "finish_tier": u.finish_tier,
            }
            for u in inputs.unit_mix
        ],
    }
    prop_estimate = client.call_tool("estimate_property_from_model", {"property_data": property_data})

    # Step 2: Per-unit-type ROI gate + renovation program
    unit_type_results: list[UnitTypeResult] = []
    all_pass = True

    for unit_type in inputs.unit_mix:
        bridge_result = client.call_tool(
            "prepare_renovation_program_tool",
            {
                "unit_sqft": unit_type.sqft,
                "bedrooms": unit_type.bedrooms,
                "bathrooms": unit_type.bathrooms,
                "current_monthly_rent": unit_type.current_monthly_rent,
                "target_monthly_rent": unit_type.target_monthly_rent,
                "start_month": inputs.start_month,
                "monthly_pace": inputs.monthly_pace,
                "scope_level": unit_type.scope_level,
                "finish_tier": unit_type.finish_tier,
                "year_built": inputs.year_built,
                "property_class": inputs.property_class,
                "market": inputs.market,
                "downtime_days": inputs.downtime_days,
                "threshold_pct": inputs.roi_threshold_pct,
            },
        )

        passes = bridge_result.get("ready_to_underwrite", False)
        roi_result = bridge_result.get("roi_result", {})
        reno_program = bridge_result.get("renovation_program")

        if not passes:
            all_pass = False

        # Pull cost estimate for this unit type from the property estimate
        unit_estimates = prop_estimate.get("unit_estimates", [])
        idx = inputs.unit_mix.index(unit_type)
        unit_est_slice = _slice_for_unit_type(unit_estimates, inputs.unit_mix, idx)
        cost_low = sum(e.get("total_low", 0) for e in unit_est_slice)
        cost_high = sum(e.get("total_high", 0) for e in unit_est_slice)

        diagnostics = _roi_diagnostics(roi_result, inputs.roi_threshold_pct, passes)

        unit_type_results.append(UnitTypeResult(
            sqft=unit_type.sqft,
            bedrooms=unit_type.bedrooms,
            bathrooms=unit_type.bathrooms,
            count=unit_type.count,
            cost_estimate_low=cost_low,
            cost_estimate_high=cost_high,
            roi_pct=roi_result.get("roi_pct", 0.0),
            roi_passes=passes,
            renovation_program=reno_program if passes else None,
            roi_result=roi_result,
            **diagnostics,
        ))

    # Step 3: Build canonical renovation_programs
    canonical_programs = build_renovation_programs(
        inputs, unit_type_results, strategy=inputs.renovation_strategy,
        base_deal_inputs=inputs.base_deal_inputs,
    )

    # Step 4: Optionally run underwriting engine
    underwriting_metrics = None
    underwriting_feasible = None
    underwriting_full = None

    if inputs.base_deal_inputs is not None and all_pass:
        underwriting_metrics, underwriting_feasible, underwriting_full = _run_underwriting(
            inputs, canonical_programs, underwriting_client,
        )

    sanity_flags = compute_sanity_flags(underwriting_metrics)

    rent_findings = _validate_rents_if_comps(inputs, comps_by_cohort)

    decision_summary = build_decision_summary(
        ready_to_underwrite=all_pass,
        underwriting_feasible=underwriting_feasible,
        unit_type_results=unit_type_results,
        risk_flags=prop_estimate.get("risk_flags", []),
        sanity_flags=sanity_flags,
    )
    if rent_findings:
        _fold_rent_findings_into_decision(decision_summary, rent_findings)

    summary = _build_summary(
        inputs, unit_type_results, prop_estimate, all_pass,
        underwriting_metrics, sanity_flags, decision_summary,
    )

    return DealAnalysis(
        property_id=inputs.property_id,
        total_units=inputs.total_units,
        ready_to_underwrite=all_pass,
        unit_type_results=unit_type_results,
        total_renovation_cost_low=prop_estimate.get("total_renovation_low", 0),
        total_renovation_cost_high=prop_estimate.get("total_renovation_high", 0),
        exterior_capex_low=prop_estimate.get("exterior_capex_low", 0),
        exterior_capex_high=prop_estimate.get("exterior_capex_high", 0),
        risk_flags=prop_estimate.get("risk_flags", []),
        renovation_programs=canonical_programs,
        underwriting_metrics=underwriting_metrics,
        underwriting_feasible=underwriting_feasible,
        underwriting_full=underwriting_full,
        sanity_flags=sanity_flags,
        decision_summary=decision_summary,
        summary=summary,
    )


def _run_underwriting(
    inputs: DealInputs,
    canonical_programs: list[dict],
    uw_client,
) -> tuple[dict, bool, dict | None]:
    """Merge renovation_programs into base deal and run the underwriting engine.

    Returns (normalized_metrics, feasible, full_result).
    full_result is the raw engine response when inputs.full_cashflow is True,
    else None. normalized_metrics is always in the nested (MCP-summary) shape
    so downstream consumers don't branch on mode.
    """
    if uw_client is None:
        from .underwriting_client import UnderwritingClient
        uw_client = UnderwritingClient()

    # Merge renovation programs into a copy of the base deal inputs
    import copy
    deal_inputs = copy.deepcopy(inputs.base_deal_inputs)
    merge_renovation_programs(deal_inputs, canonical_programs)

    if inputs.full_cashflow:
        full = uw_client.run_full(deal_inputs, include_cashflow=True)
        if full.get("status") != "success":
            return full, False, full
        metrics = _normalize_full_metrics(full)
        feasible = _feasible_from_normalized(metrics)
        return metrics, feasible, full

    # Default: MCP summary (lightweight)
    result = uw_client.run_summary(deal_inputs)
    if result.get("status") != "success":
        return result, False, None
    feasible = _feasible_from_normalized(result)
    return result, feasible, None


def _feasible_from_normalized(metrics: dict) -> bool:
    """Feasibility gate on the nested summary shape."""
    irr = metrics.get("irr", {}) or {}
    dscr = metrics.get("dscr", {}) or {}
    levered_irr = irr.get("levered_irr", 0) or 0
    min_dscr = dscr.get("minimum", 0) or 0
    return levered_irr >= 0.12 and min_dscr >= 1.20


def _normalize_full_metrics(full: dict) -> dict:
    """Reshape the engine's flat handle_run_deal response into the nested
    summary shape used by MCP run_deal_summary.

    Engine (flat)                   →  Normalized (nested)
    metrics.levered_irr             →  irr.levered_irr
    metrics.unlevered_irr           →  irr.unlevered_irr
    metrics.levered_em              →  equity_multiple.levered_em
    metrics.unlevered_em            →  equity_multiple.unlevered_em
    metrics.minimum_dscr            →  dscr.minimum
    metrics.average_dscr            →  dscr.average
    metrics.going_in_cap            →  yields.going_in_cap_rate
    """
    m = full.get("metrics", {}) or {}
    cf = full.get("cashflow_summary", {}) or {}
    return {
        "status": full.get("status", "success"),
        "irr": {
            "levered_irr": m.get("levered_irr"),
            "unlevered_irr": m.get("unlevered_irr"),
            "partnership_irr": m.get("partnership_irr"),
        },
        "equity_multiple": {
            "levered_em": m.get("levered_em"),
            "unlevered_em": m.get("unlevered_em"),
            "partnership_em": m.get("partnership_em"),
        },
        "dscr": {
            "minimum": m.get("minimum_dscr"),
            "average": m.get("average_dscr"),
        },
        "yields": {
            "going_in_cap_rate": m.get("going_in_cap"),
        },
        "cashflow_summary": cf,
    }


def _validate_rents_if_comps(
    inputs: DealInputs,
    comps_by_cohort: dict | None,
) -> list[RentValidationFinding]:
    """Run rent validation when comps are supplied; return [] otherwise."""
    if not comps_by_cohort:
        return []
    proposed = [
        ProposedRent(
            bedrooms=u.bedrooms, bathrooms=u.bathrooms, sqft=u.sqft,
            proposed_target_monthly_rent=u.target_monthly_rent,
        )
        for u in inputs.unit_mix
    ]
    request = RentValidationRequest(
        proposed_rents=proposed, comps_by_cohort=comps_by_cohort,
    )
    return validate_target_rents(request).findings


def _fold_rent_findings_into_decision(
    decision_summary: dict,
    findings: list[RentValidationFinding],
) -> None:
    """Append above_band findings as binding constraints. above_band is the
    only severity that warrants action — within_band needs no surfacing,
    below_band is conservative (good news), and insufficient_comps is a
    data-quality note, not a deal-blocker."""
    for f in findings:
        if f.verdict == "above_band":
            decision_summary.setdefault("binding_constraints", []).append(
                f"[rent] {f.cohort_key}: {f.message}"
            )


def _roi_diagnostics(roi_result: dict, threshold_pct: float, passes: bool) -> dict:
    """Promote decision-relevant ROI fields out of the nested roi_result dict.

    Costmodel already computes monthly/annual lift and the cost/rent deltas
    needed to pass — we just surface them at the top level so memo writers
    don't have to dig.
    """
    monthly_lift = float(roi_result.get("monthly_rent_lift", 0.0) or 0.0)
    annual_lift = float(roi_result.get("annual_rent_lift", monthly_lift * 12) or 0.0)
    roi_pct = float(roi_result.get("roi_pct", 0.0) or 0.0)

    # Per-unit cost (roi_result is computed per-unit by costmodel)
    cost_high = float(roi_result.get("total_cost_high", 0.0) or 0.0)
    payback = (cost_high / monthly_lift) if monthly_lift > 0 else None

    return {
        "monthly_rent_lift": monthly_lift,
        "annual_rent_lift": annual_lift,
        "roi_gap_pp": round(roi_pct - threshold_pct, 2),
        "simple_payback_months": round(payback, 1) if payback is not None else None,
        "cost_reduction_needed_to_pass": (
            None if passes else roi_result.get("cost_reduction_needed")
        ),
        "rent_increase_needed_to_pass": (
            None if passes else roi_result.get("rent_increase_needed")
        ),
    }


def _slice_for_unit_type(unit_estimates: list[dict], unit_mix, idx: int) -> list[dict]:
    """Return the expanded unit_estimates slice that belongs to unit_mix[idx]."""
    offset = sum(u.count for u in unit_mix[:idx])
    count = unit_mix[idx].count
    return unit_estimates[offset: offset + count]


def _build_summary(
    inputs: DealInputs,
    results: list[UnitTypeResult],
    prop_est: dict,
    all_pass: bool,
    uw_metrics: dict | None = None,
    sanity_flags: list[dict] | None = None,
    decision_summary: dict | None = None,
) -> str:
    total_high = prop_est.get("total_renovation_high", 0)
    ext_high = prop_est.get("exterior_capex_high", 0)
    risk_count = len(prop_est.get("risk_flags", []))

    lines = [
        f"Deal: {inputs.property_id} | {inputs.total_units} units | "
        f"Built {inputs.year_built or 'unknown'} | Class {inputs.property_class or 'unknown'}",
        f"Total renovation (high): ${total_high:,.0f}  |  Exterior CapEx: ${ext_high:,.0f}",
        f"ROI gate: {'ALL PASS' if all_pass else 'FAIL — see unit type details below'}",
    ]

    for r in results:
        status = "PASS" if r.roi_passes else "FAIL"
        lines.append(
            f"  {r.count}x {r.bedrooms}BR/{r.bathrooms}BA {r.sqft:.0f}sf — "
            f"cost ${r.cost_estimate_high:,.0f} (high) — ROI {r.roi_pct:.1f}% [{status}]"
        )

    if risk_count:
        lines.append(f"Risk flags: {risk_count} (see risk_flags in full output)")

    if uw_metrics and uw_metrics.get("status") == "success":
        irr = uw_metrics.get("irr", {})
        em = uw_metrics.get("equity_multiple", {})
        dscr = uw_metrics.get("dscr", {})
        yields = uw_metrics.get("yields", {})
        lines.append("--- Underwriting Metrics ---")
        if irr.get("levered_irr"):
            lines.append(f"  Levered IRR: {irr['levered_irr']:.1%}  |  Unlevered: {irr.get('unlevered_irr', 0):.1%}")
        if em.get("levered_em"):
            lines.append(f"  Equity Multiple: {em['levered_em']:.2f}x (levered)  |  {em.get('unlevered_em', 0):.2f}x (unlevered)")
        if dscr.get("minimum"):
            lines.append(f"  DSCR: {dscr['minimum']:.2f}x min  |  {dscr.get('average', 0):.2f}x avg")
        if yields.get("going_in_cap_rate"):
            lines.append(f"  Going-in cap: {yields['going_in_cap_rate']:.1%}  |  Exit cap: {yields.get('exit_cap_rate', 0):.1%}")

    if sanity_flags:
        errors = [f for f in sanity_flags if f.get("severity") == "error"]
        warnings = [f for f in sanity_flags if f.get("severity") == "warning"]
        lines.append(
            f"--- Sanity Flags: {len(errors)} error(s), {len(warnings)} warning(s) ---"
        )
        for f in sanity_flags:
            tag = f.get("severity", "warning").upper()
            lines.append(f"  [{tag}] {f.get('message', '')}")

    if decision_summary:
        lines.append("--- Decision Summary ---")
        headline = decision_summary.get("headline")
        if headline:
            lines.append(f"  {headline}")
        for c in decision_summary.get("binding_constraints", []):
            lines.append(f"  • {c}")
        recs = decision_summary.get("recommended_changes", [])
        if recs:
            lines.append("  Recommended changes:")
            for r in recs:
                lines.append(f"    → {r}")

    return "\n".join(lines)
