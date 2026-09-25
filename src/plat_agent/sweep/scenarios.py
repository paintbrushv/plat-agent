"""Bull/Base/Bear scenario sweep.

Runs the engine 3 times — once on the un-perturbed merged deal (Base) and
once for each preset in the named family (bull, bear) — and returns a
ScenarioSweepResult with feasibility flags.

Scenarios are only run if the ROI gate passes; otherwise the result is
returned with status='roi_gate_failed' and no engine calls.
"""

import copy
from concurrent.futures import ThreadPoolExecutor

from ..analyzer import analyze_deal
from ..models import DealAnalysis, DealInputs
from ..schema_mapper import merge_renovation_programs
from .constants import DEFAULT_DSCR_HURDLE, DEFAULT_IRR_HURDLE
from .backend import LocalRunBackend, RunBackend
from .models import ScenarioRun, ScenarioSweepResult
from .perturb import apply_preset, get_preset_family


def _stub_analysis_for_canonical(property_id: str, total_units: int, canonical: dict) -> DealAnalysis:
    """Build a passing DealAnalysis stub from a canonical dict.

    The canonical is assumed pre-validated; renovation_programs (if any) live
    on the canonical itself, so the stub's renovation_programs list is empty
    and merge_renovation_programs becomes a no-op.
    """
    return DealAnalysis(
        property_id=property_id,
        total_units=total_units,
        ready_to_underwrite=True,
        unit_type_results=[],
        total_renovation_cost_low=0.0,
        total_renovation_cost_high=0.0,
        exterior_capex_low=0.0,
        exterior_capex_high=0.0,
        risk_flags=[],
        renovation_programs=[],
        summary="canonical entry point — ROI gate skipped",
    )


def _deal_inputs_from_canonical(canonical: dict) -> DealInputs:
    """Synthesize a minimal DealInputs stub wrapping a canonical dict.

    Validates that canonical has metadata.deal_id and unit_cohorts. Pulls
    start_month from time_grid.analysis_start_date when present (sweeps don't
    actually read start_month after extracting base_deal_inputs, but DealInputs
    requires it). monthly_pace defaults to 1; unit_mix is intentionally empty.

    Caller responsibility to keep cohort IDs consistent across canonical's
    unit_cohorts and renovation_programs — perturb.apply_target_rent looks up
    cohort_ids in unit_cohorts and a missing match raises ValueError.
    """
    metadata = canonical.get("metadata") or {}
    deal_id = metadata.get("deal_id")
    cohorts = canonical.get("unit_cohorts")
    if not deal_id:
        raise ValueError("canonical['metadata']['deal_id'] is required.")
    if not cohorts:
        raise ValueError("canonical['unit_cohorts'] is required and must be non-empty.")

    total_units = sum(int(c.get("unit_count", 0)) for c in cohorts)
    time_grid = canonical.get("time_grid") or {}
    start_month = time_grid.get("analysis_start_date") or "2026-01"

    return DealInputs(
        property_id=deal_id,
        total_units=total_units,
        unit_mix=[],
        start_month=start_month,
        monthly_pace=1,
        base_deal_inputs=canonical,
    )


# Feasibility gate — identical to analyzer._feasible_from_normalized.
_IRR_HURDLE = DEFAULT_IRR_HURDLE
_DSCR_HURDLE = DEFAULT_DSCR_HURDLE


def _is_feasible(metrics: dict) -> bool:
    irr = (metrics.get("irr") or {}).get("levered_irr") or 0
    dscr = (metrics.get("dscr") or {}).get("minimum") or 0
    return irr >= _IRR_HURDLE and dscr >= _DSCR_HURDLE


def _preset_to_overrides(preset) -> dict:
    """Render a ScenarioPresets dataclass as a plain dict for result payload."""
    return {
        "rent_growth_delta_bps": preset.rent_growth_delta_bps,
        "exit_cap_delta_bps": preset.exit_cap_delta_bps,
        "vacancy_delta_bps": preset.vacancy_delta_bps,
        "opex_growth_delta_bps": preset.opex_growth_delta_bps,
    }


def _run_one(backend: RunBackend, deal: dict, name: str, overrides: dict) -> ScenarioRun:
    """Run one scenario and package the result."""
    metrics = backend.run_deal(deal)
    if metrics.get("status") != "success":
        return ScenarioRun(
            name=name, overrides=overrides, metrics=metrics,
            feasible=False, status="error",
            error=metrics.get("error", "engine returned non-success status"),
        )
    return ScenarioRun(
        name=name, overrides=overrides, metrics=metrics,
        feasible=_is_feasible(metrics), status="success",
    )


def _build_summary(property_id: str, preset_family: str, status: str, scenarios: list[ScenarioRun]) -> str:
    if status == "roi_gate_failed":
        return f"Deal: {property_id} | ROI gate failed — no scenarios run."

    lines = [f"Deal: {property_id} | Preset family: {preset_family}"]
    lines.append(f"{'Scenario':<8}  {'Levered IRR':>12}  {'Min DSCR':>9}  {'Feasible':>9}")
    lines.append("-" * 44)
    for s in scenarios:
        if s.status == "error":
            lines.append(f"{s.name:<8}  ERROR: {s.error}")
            continue
        irr = (s.metrics.get("irr") or {}).get("levered_irr")
        dscr = (s.metrics.get("dscr") or {}).get("minimum")
        irr_str = f"{irr:>12.1%}" if irr is not None else f"{'n/a':>12}"
        dscr_str = f"{dscr:>9.2f}" if dscr is not None else f"{'n/a':>9}"
        feas = "YES" if s.feasible else "no"
        lines.append(f"{s.name:<8}  {irr_str}  {dscr_str}  {feas:>9}")
    return "\n".join(lines)


def run_scenarios(
    inputs: DealInputs,
    preset: str = "value_add",
    analysis: DealAnalysis | None = None,
    backend: RunBackend | None = None,
    concurrency: int = 1,
) -> ScenarioSweepResult:
    """Run Bull/Base/Bear scenario sweep on one deal.

    Args:
        inputs: Deal inputs. Must include base_deal_inputs.
        preset: Preset family name — "value_add" (default) or "stabilized".
        analysis: Optional pre-computed DealAnalysis. If ready_to_underwrite,
            its renovation_programs are reused; otherwise analyze_deal() is
            called. Saves ~5-10s when chaining analyze -> scenarios.
        backend: Optional RunBackend. Defaults to LocalRunBackend().
        concurrency: Max parallel scenarios. Default 1 (sequential).
            Useful with AzureRunBackend where each scenario polls
            independently — concurrency=3 collapses 3 polls' wall time into
            roughly the slowest one. LocalRunBackend serializes through a
            single MCP worker so concurrency >1 will not speed it up.

    Raises:
        ValueError: base_deal_inputs missing, or unknown preset family.
    """
    if concurrency < 1:
        raise ValueError(f"concurrency must be >=1, got {concurrency}")
    if inputs.base_deal_inputs is None:
        raise ValueError("base_deal_inputs is required for scenario sweeps.")

    preset_family = get_preset_family(preset)  # raises ValueError if unknown

    # Ensure we have an analysis (ROI gate + renovation_programs)
    if analysis is None:
        analysis = analyze_deal(inputs)

    if not analysis.ready_to_underwrite:
        return ScenarioSweepResult(
            property_id=inputs.property_id,
            preset_family=preset,
            status="roi_gate_failed",
            scenarios=[],
            summary=_build_summary(inputs.property_id, preset, "roi_gate_failed", []),
        )

    if backend is None:
        backend = LocalRunBackend()

    # Build the merged deal once
    merged = copy.deepcopy(inputs.base_deal_inputs)
    merge_renovation_programs(merged, analysis.renovation_programs)

    # Materialize all scenarios up front so the dispatch is uniform across
    # sequential vs parallel paths. Order is preserved end-to-end.
    jobs: list[tuple[str, dict, dict]] = [("Base", merged, {})]
    for _key, preset_obj in preset_family.items():
        perturbed = apply_preset(merged, preset_obj)
        jobs.append((preset_obj.name, perturbed, _preset_to_overrides(preset_obj)))

    if concurrency == 1:
        scenarios = [_run_one(backend, deal, name, overrides) for name, deal, overrides in jobs]
    else:
        with ThreadPoolExecutor(max_workers=min(concurrency, len(jobs))) as ex:
            # executor.map preserves input order — critical for deterministic output
            scenarios = list(ex.map(
                lambda job: _run_one(backend, job[1], job[0], job[2]),
                jobs,
            ))

    return ScenarioSweepResult(
        property_id=inputs.property_id,
        preset_family=preset,
        status="success",
        scenarios=scenarios,
        summary=_build_summary(inputs.property_id, preset, "success", scenarios),
    )


def run_scenarios_from_canonical(
    canonical: dict,
    preset: str = "value_add",
    *,
    analysis: DealAnalysis | None = None,
    backend: RunBackend | None = None,
    concurrency: int = 1,
) -> ScenarioSweepResult:
    """Run Bull/Base/Bear scenario sweep directly on a canonical schema v0.1 dict.

    Use this when you already have a federation-ready canonical_inputs.json and
    want to skip the legacy DealInputs assembly. The ROI gate is skipped:
    canonical is assumed pre-validated, and any renovation_programs are assumed
    already embedded in the canonical itself.

    For analyst-driven flows that include cost-modeling and ROI gating, use
    run_scenarios() with a fully populated DealInputs instead.
    """
    inputs = _deal_inputs_from_canonical(canonical)
    if analysis is None:
        analysis = _stub_analysis_for_canonical(inputs.property_id, inputs.total_units, canonical)
    return run_scenarios(
        inputs, preset=preset, analysis=analysis,
        backend=backend, concurrency=concurrency,
    )
