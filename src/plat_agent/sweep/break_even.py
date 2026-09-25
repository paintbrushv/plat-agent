"""One-axis break-even search via bisection.

For a given axis (target_rent, exit_cap, monthly_pace), finds the axis value
at which the selected objective meets its threshold. Compound objective is
IRR >= threshold AND min DSCR >= threshold — computed as max of the two
individual break-evens, since each metric is monotonic in the supported axes.

Returns structured status values instead of raising for search-level failures:
  - "success": breakpoint found
  - "not_bracketed": threshold outside [lo, hi] (analyst must widen bounds)
  - "bracket_error": engine returned status="error" during endpoint or bisection
  - "roi_gate_failed": analysis.ready_to_underwrite was False
"""

import copy

from ..analyzer import analyze_deal
from ..models import DealAnalysis, DealInputs
from ..schema_mapper import merge_renovation_programs
from .constants import DEFAULT_DSCR_HURDLE, DEFAULT_IRR_HURDLE
from .backend import LocalRunBackend, RunBackend
from .models import BreakEvenResult
from .perturb import AXIS_MUTATORS
from .scenarios import _deal_inputs_from_canonical, _stub_analysis_for_canonical


_IRR_HURDLE = DEFAULT_IRR_HURDLE
_DSCR_HURDLE = DEFAULT_DSCR_HURDLE


def _build_mutator(axis: str, cohort_id: str | None):
    """Return a single-arg callable: value -> perturbed_deal, closed over the axis."""
    mutator = AXIS_MUTATORS[axis]
    if axis == "target_rent":
        return lambda deal, value: mutator(deal, value=value, cohort_id=cohort_id)
    return lambda deal, value: mutator(deal, value=value)

def _axis_improves_when_higher(axis: str) -> bool:
    return axis in {"target_rent", "monthly_pace"}



def _extract_metric(metrics: dict, name: str) -> float | None:
    if name == "levered_irr":
        return (metrics.get("irr") or {}).get("levered_irr")
    if name == "min_dscr":
        return (metrics.get("dscr") or {}).get("minimum")
    raise ValueError(f"Unknown metric name: {name!r}")


def _validate_inputs(inputs: DealInputs, axis: str, bounds: tuple[float, float],
                     cohort_id: str | None) -> None:
    if inputs.base_deal_inputs is None:
        raise ValueError("base_deal_inputs is required for break-even search.")
    if axis not in AXIS_MUTATORS:
        raise ValueError(f"Unknown axis: {axis!r}. Must be one of {list(AXIS_MUTATORS)}.")
    lo, hi = bounds
    if not (lo < hi):
        raise ValueError(f"bounds must be (lo, hi) with lo < hi; got {bounds}.")
    if axis == "target_rent":
        if cohort_id is None:
            raise ValueError("cohort_id is required when axis='target_rent'.")
        cohort_ids = [c.get("cohort_id") for c in inputs.base_deal_inputs.get("unit_cohorts", [])]
        if cohort_id not in cohort_ids:
            raise ValueError(
                f"cohort_id {cohort_id!r} not found in unit_cohorts; "
                f"present: {cohort_ids}"
            )


def find_break_even(
    inputs: DealInputs,
    axis: str,
    bounds: tuple[float, float],
    cohort_id: str | None = None,
    objective: str = "compound",
    irr_threshold: float = _IRR_HURDLE,
    dscr_threshold: float = _DSCR_HURDLE,
    tolerance: float = 0.005,
    max_iterations: int = 20,
    analysis: DealAnalysis | None = None,
    backend: RunBackend | None = None,
) -> BreakEvenResult:
    """Find the axis value at which objective meets its threshold(s).

    See module docstring for status-code semantics.
    """
    _validate_inputs(inputs, axis, bounds, cohort_id)

    if analysis is None:
        analysis = analyze_deal(inputs)

    if not analysis.ready_to_underwrite:
        return BreakEvenResult(
            property_id=inputs.property_id,
            axis=axis, cohort_id=cohort_id, objective=objective,
            status="roi_gate_failed",
            summary=f"Deal: {inputs.property_id} | ROI gate failed — no break-even run.",
        )

    if backend is None:
        backend = LocalRunBackend()

    # Build the merged deal once
    merged = copy.deepcopy(inputs.base_deal_inputs)
    merge_renovation_programs(merged, analysis.renovation_programs)

    mutator = _build_mutator(axis, cohort_id)
    lo, hi = bounds

    # Evaluate endpoints
    lo_metrics = backend.run_deal(mutator(merged, lo))
    if lo_metrics.get("status") != "success":
        return BreakEvenResult(
            property_id=inputs.property_id, axis=axis, cohort_id=cohort_id,
            objective=objective, status="bracket_error",
            bracket_low={"axis_value": lo, "metrics": lo_metrics},
            summary=f"Low-endpoint engine error: {lo_metrics.get('error', 'unknown')}",
        )
    hi_metrics = backend.run_deal(mutator(merged, hi))
    if hi_metrics.get("status") != "success":
        return BreakEvenResult(
            property_id=inputs.property_id, axis=axis, cohort_id=cohort_id,
            objective=objective, status="bracket_error",
            bracket_low={"axis_value": lo, "metrics": lo_metrics},
            bracket_high={"axis_value": hi, "metrics": hi_metrics},
            summary=f"High-endpoint engine error: {hi_metrics.get('error', 'unknown')}",
        )

    bracket_low_dict = {"axis_value": lo, "metrics": lo_metrics}
    bracket_high_dict = {"axis_value": hi, "metrics": hi_metrics}

    # Determine which constraints to bisect, based on objective
    constraints: list[tuple[str, str, float]] = []  # (name, metric_key, threshold)
    if objective in ("compound", "levered_irr"):
        constraints.append(("irr", "levered_irr", irr_threshold))
    if objective in ("compound", "min_dscr"):
        constraints.append(("dscr", "min_dscr", dscr_threshold))
    if not constraints:
        raise ValueError(
            f"Unknown objective: {objective!r}. Must be 'compound', 'levered_irr', or 'min_dscr'."
        )

    # Check bracketing for each required constraint
    not_bracketed: list[str] = []
    bracket_info: dict[str, tuple[float, float]] = {}
    for name, key, threshold in constraints:
        lo_val = _extract_metric(lo_metrics, key)
        hi_val = _extract_metric(hi_metrics, key)
        if lo_val is None or hi_val is None:
            not_bracketed.append(name)
            continue
        # Bracketed iff threshold is between lo and hi (inclusive tolerance)
        is_bracketed = (lo_val - threshold) * (hi_val - threshold) <= 0
        if not is_bracketed:
            not_bracketed.append(name)
        bracket_info[name] = (lo_val, hi_val)

    if not_bracketed:
        missing = ", ".join(not_bracketed)
        endpoints_str = "; ".join(f"{n}: {lv:.4f} @ {lo:.4f} → {hv:.4f} @ {hi:.4f}"
                                  for n, (lv, hv) in bracket_info.items())
        return BreakEvenResult(
            property_id=inputs.property_id, axis=axis, cohort_id=cohort_id,
            objective=objective, status="not_bracketed",
            bracket_low=bracket_low_dict, bracket_high=bracket_high_dict,
            summary=(
                f"Not bracketed on: {missing}. Widen bounds. "
                f"Endpoint values: {endpoints_str}"
            ),
        )

    # Bisect each bracketed constraint
    breakpoints: dict[str, float] = {}
    iterations_by: dict[str, int] = {}
    mid_error: dict | None = None

    for name, key, threshold in constraints:
        a, b = lo, hi
        a_val = _extract_metric(lo_metrics, key)
        b_val = _extract_metric(hi_metrics, key)
        iters = 0
        bp = (a + b) / 2
        while (b - a) > tolerance and iters < max_iterations:
            mid = (a + b) / 2
            mid_metrics = backend.run_deal(mutator(merged, mid))
            iters += 1
            if mid_metrics.get("status") != "success":
                mid_error = mid_metrics
                break
            mid_val = _extract_metric(mid_metrics, key)
            # Root bracketed on the side where sign(val - threshold) differs
            if (a_val - threshold) * (mid_val - threshold) <= 0:
                b, b_val = mid, mid_val
            else:
                a, a_val = mid, mid_val
            bp = (a + b) / 2
        if mid_error is not None:
            break
        breakpoints[name] = bp
        iterations_by[name] = iters

    if mid_error is not None:
        return BreakEvenResult(
            property_id=inputs.property_id, axis=axis, cohort_id=cohort_id,
            objective=objective, status="bracket_error",
            bracket_low=bracket_low_dict, bracket_high=bracket_high_dict,
            summary=f"Mid-bisection engine error: {mid_error.get('error', 'unknown')}",
        )

    # Resolve breakpoint, binding_constraint, and breakpoint_metrics
    irr_bp = breakpoints.get("irr")
    dscr_bp = breakpoints.get("dscr")
    if objective == "compound":
        if _axis_improves_when_higher(axis):
            if irr_bp >= dscr_bp:
                final_bp, binding = irr_bp, "irr"
            else:
                final_bp, binding = dscr_bp, "dscr"
        else:
            if irr_bp <= dscr_bp:
                final_bp, binding = irr_bp, "irr"
            else:
                final_bp, binding = dscr_bp, "dscr"
    elif objective == "levered_irr":
        final_bp, binding = irr_bp, None
    else:  # min_dscr
        final_bp, binding = dscr_bp, None

    # Re-run at the final breakpoint so breakpoint_metrics reflects it accurately
    final_metrics = backend.run_deal(mutator(merged, final_bp))
    if final_metrics.get("status") != "success":
        return BreakEvenResult(
            property_id=inputs.property_id, axis=axis, cohort_id=cohort_id,
            objective=objective, status="bracket_error",
            bracket_low=bracket_low_dict, bracket_high=bracket_high_dict,
            irr_breakpoint=irr_bp, irr_iterations=iterations_by.get("irr", 0),
            dscr_breakpoint=dscr_bp, dscr_iterations=iterations_by.get("dscr", 0),
            summary=f"Re-run at breakpoint failed: {final_metrics.get('error', 'unknown')}",
        )

    return BreakEvenResult(
        property_id=inputs.property_id, axis=axis, cohort_id=cohort_id,
        objective=objective, status="success",
        binding_constraint=binding,
        breakpoint=final_bp, breakpoint_metrics=final_metrics,
        irr_breakpoint=irr_bp, irr_iterations=iterations_by.get("irr", 0),
        dscr_breakpoint=dscr_bp, dscr_iterations=iterations_by.get("dscr", 0),
        bracket_low=bracket_low_dict, bracket_high=bracket_high_dict,
        summary=_build_break_even_summary(
            inputs.property_id, axis, cohort_id, objective,
            final_bp, binding, iterations_by,
        ),
    )


def find_break_even_from_canonical(
    canonical: dict,
    axis: str,
    bounds: tuple[float, float],
    *,
    cohort_id: str | None = None,
    objective: str = "compound",
    irr_threshold: float = _IRR_HURDLE,
    dscr_threshold: float = _DSCR_HURDLE,
    tolerance: float = 0.005,
    max_iterations: int = 20,
    analysis: DealAnalysis | None = None,
    backend: RunBackend | None = None,
) -> BreakEvenResult:
    """One-axis break-even search on a canonical schema v0.1 dict directly.

    Use this when you already have a federation-ready canonical_inputs.json and
    want to skip the legacy DealInputs assembly. The ROI gate is skipped:
    canonical is assumed pre-validated, and any renovation_programs are assumed
    already embedded in the canonical itself.

    For analyst-driven flows that include cost-modeling and ROI gating, use
    find_break_even() with a fully populated DealInputs instead.

    Caller responsibility to keep cohort IDs consistent across canonical's
    unit_cohorts and renovation_programs (perturb.apply_target_rent looks up
    target_cohort against unit_cohorts).
    """
    inputs = _deal_inputs_from_canonical(canonical)
    if analysis is None:
        analysis = _stub_analysis_for_canonical(inputs.property_id, inputs.total_units, canonical)
    return find_break_even(
        inputs,
        axis=axis,
        bounds=bounds,
        cohort_id=cohort_id,
        objective=objective,
        irr_threshold=irr_threshold,
        dscr_threshold=dscr_threshold,
        tolerance=tolerance,
        max_iterations=max_iterations,
        analysis=analysis,
        backend=backend,
    )


def _build_break_even_summary(
    property_id: str, axis: str, cohort_id: str | None, objective: str,
    breakpoint: float, binding: str | None, iterations_by: dict,
) -> str:
    axis_label = f"{axis}" if cohort_id is None else f"{axis} ({cohort_id})"
    obj_label = {"compound": "compound (IRR & DSCR)",
                 "levered_irr": "levered IRR",
                 "min_dscr": "min DSCR"}.get(objective, objective)
    bind = f"; binding: {binding.upper()}" if binding else ""
    total_iters = sum(iterations_by.values())
    return (
        f"Deal: {property_id} | Break-even on {axis_label} vs {obj_label}: "
        f"{breakpoint:.4f} ({total_iters} bisection iterations{bind})"
    )
