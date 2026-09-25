"""Memo-ready decision synthesis for a DealAnalysis.

build_decision_summary() takes the raw analysis pieces and returns a single
dict with three keys:
    headline               — one-sentence verdict an analyst could lead a memo with
    binding_constraints    — plain-English reasons this deal is or isn't ready
    recommended_changes    — specific actions that would unblock the deal

This is structured (not free-form prose) so an LLM working from the JSON has
the "why" already decomposed, but a human reading the JSON also gets value.

This module has zero external dependencies — all inputs are plain dicts /
already-built UnitTypeResult objects. Easy to unit-test and easy to call
from non-analyzer paths (e.g. sweep summarization later).

Federated emission: the federated workflow does NOT call this function
(it bypasses `analyze_deal()`). Federated `decision_summary.json` is
produced instead by `plat_agent.synthesize.synthesize_from_federated_artifacts`,
invoked via the `plat synthesize-decision` CLI from the
`synthesize-decision` orchestrator agent (between underwriting-orchestrator
and deal-memo-writer). Both paths emit the same `{headline,
binding_constraints, recommended_changes}` shape; the federated path
adds `data_gaps`. Keep this module and `synthesize.py` in lockstep when
the decision-shape evolves.
"""

from .models import UnitTypeResult


def build_decision_summary(
    *,
    ready_to_underwrite: bool,
    underwriting_feasible: bool | None,
    unit_type_results: list[UnitTypeResult],
    risk_flags: list[dict],
    sanity_flags: list[dict],
) -> dict:
    """Return {headline, binding_constraints, recommended_changes}."""
    failing = [u for u in unit_type_results if not u.roi_passes]
    sanity_errors = [f for f in sanity_flags if f.get("severity") == "error"]
    sanity_warnings = [f for f in sanity_flags if f.get("severity") == "warning"]

    headline = _headline(
        ready_to_underwrite=ready_to_underwrite,
        underwriting_feasible=underwriting_feasible,
        n_failing=len(failing),
        n_unit_types=len(unit_type_results),
        n_sanity_errors=len(sanity_errors),
        n_sanity_warnings=len(sanity_warnings),
    )

    binding_constraints: list[str] = []
    binding_constraints.extend(_roi_failure_constraints(failing))
    binding_constraints.extend(_sanity_constraints(sanity_flags))
    binding_constraints.extend(_risk_flag_constraints(risk_flags))

    recommended_changes: list[str] = []
    recommended_changes.extend(_roi_path_to_pass(failing))
    recommended_changes.extend(_sanity_remediation(sanity_flags))

    return {
        "headline": headline,
        "binding_constraints": binding_constraints,
        "recommended_changes": recommended_changes,
    }


def _headline(
    *,
    ready_to_underwrite: bool,
    underwriting_feasible: bool | None,
    n_failing: int,
    n_unit_types: int,
    n_sanity_errors: int,
    n_sanity_warnings: int,
) -> str:
    if not ready_to_underwrite:
        return (
            f"Not ready to underwrite — {n_failing} of {n_unit_types} unit type(s) "
            "fail the ROI gate."
        )
    if underwriting_feasible is False:
        return "Underwriting ran but failed the IRR/DSCR feasibility gate."
    if n_sanity_errors:
        return (
            f"Underwriting passed but {n_sanity_errors} sanity error(s) suggest "
            "the inputs or outputs are out of range — verify before acting."
        )
    if n_sanity_warnings:
        return (
            f"Deal clears all gates with {n_sanity_warnings} sanity warning(s) "
            "worth a second look."
        )
    if underwriting_feasible:
        return "Deal clears the ROI gate and underwriting feasibility — ready to advance."
    return "Deal clears the ROI gate; underwriting not run."


def _roi_failure_constraints(failing: list[UnitTypeResult]) -> list[str]:
    out: list[str] = []
    for u in failing:
        label = f"{u.bedrooms}BR/{u.bathrooms}BA {u.sqft:.0f}sf"
        gap = u.roi_gap_pp
        out.append(
            f"{label} fails ROI gate by {abs(gap):.1f}pp "
            f"(ROI {u.roi_pct:.1f}%, gap {gap:.1f}pp vs threshold; "
            f"monthly rent lift ${u.monthly_rent_lift:,.0f})."
        )
    return out


def _sanity_constraints(sanity_flags: list[dict]) -> list[str]:
    return [
        f"[{f.get('severity', 'warning').upper()}] {f.get('message', '')}"
        for f in sanity_flags
    ]


def _risk_flag_constraints(risk_flags: list[dict]) -> list[str]:
    """Risk flags from costmodel — surface message text if present."""
    out: list[str] = []
    for f in risk_flags:
        msg = f.get("message") or f.get("description") or f.get("flag")
        if msg:
            out.append(f"[risk] {msg}")
    return out


def _roi_path_to_pass(failing: list[UnitTypeResult]) -> list[str]:
    out: list[str] = []
    for u in failing:
        label = f"{u.bedrooms}BR/{u.bathrooms}BA"
        rent_delta = u.rent_increase_needed_to_pass
        cost_delta = u.cost_reduction_needed_to_pass
        if rent_delta is not None and rent_delta > 0:
            out.append(
                f"{label}: raise target rent by ${rent_delta:,.0f}/mo to clear "
                "ROI gate at current cost."
            )
        if cost_delta is not None and cost_delta > 0:
            out.append(
                f"{label}: cut renovation cost by ${cost_delta:,.0f} to clear "
                "ROI gate at current target rent."
            )
    return out


def _sanity_remediation(sanity_flags: list[dict]) -> list[str]:
    out: list[str] = []
    for f in sanity_flags:
        metric = f.get("metric")
        if metric == "going_in_cap_rate":
            out.append(
                "Re-verify NOI assumption and purchase price — going-in cap "
                "is outside the typical 4-7% multifamily range."
            )
        elif metric == "min_dscr":
            out.append(
                "Re-check debt sizing or NOI in worst months — minimum DSCR "
                "is below the 1.20x lender threshold."
            )
        elif metric == "levered_irr":
            out.append(
                "Sanity-check rent growth, exit cap, and leverage — levered "
                "IRR is outside the plausible range."
            )
        elif metric == "levered_em":
            out.append(
                "Sanity-check inputs — levered equity multiple is outside "
                "the plausible range."
            )
    return out
