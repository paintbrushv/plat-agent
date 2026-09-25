"""Live smoke test — requires real plat-costmodel and underwriting servers.

Run with:
    python tests/smoke_test_live.py

Or with explicit server commands:
    PLAT_COSTMODEL_CMD="..." python tests/smoke_test_live.py
"""
import json
import os
import sys
import time

# Default server paths (sibling repos with .venv)
if not os.environ.get("PLAT_COSTMODEL_CMD"):
    os.environ["PLAT_COSTMODEL_CMD"] = (
        "/path/to/projects/plat-costmodel/.venv/bin/python "
        "-m plat_costmodel.server"
    )
if not os.environ.get("UNDERWRITING_ENGINE_PATH"):
    os.environ["UNDERWRITING_ENGINE_PATH"] = (
        "/path/to/projects/multifamily-underwriting"
    )

sys.path.insert(0, "/path/to/projects/plat-agent/src")

from plat_agent.analyzer import analyze_deal
from plat_agent.models import DealInputs
from plat_agent.costmodel_client import CostModelClient
from plat_agent.underwriting_client import UnderwritingClient

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "sample_hills_deal.json")


def run():
    with open(FIXTURE) as f:
        raw = json.load(f)

    inputs = DealInputs(**raw)
    print(f"Deal: {inputs.property_id} — {inputs.total_units} units, {len(inputs.unit_mix)} unit types")
    for u in inputs.unit_mix:
        print(f"  {u.bedrooms}BR/{u.bathrooms}BA {u.sqft}sf: {u.count} units "
              f"${u.current_monthly_rent:.0f}→${u.target_monthly_rent:.0f}")

    costmodel = CostModelClient()
    uw = UnderwritingClient()

    print("\nRunning plat-agent analysis (this spawns MCP servers)...")
    t0 = time.time()
    result = analyze_deal(inputs, client=costmodel, underwriting_client=uw)
    elapsed = time.time() - t0

    print(f"\n{'='*60}")
    print(result.summary)
    print(f"{'='*60}")
    print(f"\nrenovation_programs: {len(result.renovation_programs)}")
    print(f"ready_to_underwrite: {result.ready_to_underwrite}")
    print(f"unit_type_results: {len(result.unit_type_results)}")

    for utr in result.unit_type_results:
        status = "PASS" if utr.roi_passes else "FAIL"
        print(f"  [{status}] {utr.bedrooms}BR/{utr.bathrooms}BA {utr.sqft}sf: "
              f"ROI={utr.roi_pct:.1f}% cost_high=${utr.cost_estimate_high:,.0f}")

    if result.underwriting_metrics:
        irr = result.underwriting_metrics.get("irr", {})
        em = result.underwriting_metrics.get("equity_multiple", {})
        dscr = result.underwriting_metrics.get("dscr", {})
        levered = irr.get("levered_irr")
        print(f"\nUnderwriting metrics:")
        print(f"  Levered IRR: {levered:.1%}" if levered else "  Levered IRR: N/A")
        print(f"  Levered EM:  {em.get('levered_em', 'N/A')}")
        print(f"  Min DSCR:    {dscr.get('minimum', 'N/A')}")
        print(f"  Feasible:    {result.underwriting_feasible}")
    else:
        print("\nNo underwriting metrics (ROI gate failed or no base_deal_inputs)")

    # Sanity flags + decision synthesis layers (added Wave: validation/synthesis)
    print(f"\nSanity flags: {len(result.sanity_flags)}")
    for f in result.sanity_flags:
        print(f"  [{f.get('severity', '?').upper()}] {f.get('metric')}: {f.get('message')}")

    ds = result.decision_summary
    print(f"\nDecision summary:")
    print(f"  Headline: {ds.get('headline', 'n/a')}")
    if ds.get("binding_constraints"):
        print(f"  Binding constraints ({len(ds['binding_constraints'])}):")
        for c in ds["binding_constraints"]:
            print(f"    • {c}")
    if ds.get("recommended_changes"):
        print(f"  Recommended changes ({len(ds['recommended_changes'])}):")
        for r in ds["recommended_changes"]:
            print(f"    → {r}")

    print(f"\nTotal elapsed: {elapsed:.1f}s")

    # Assertions
    assert result.property_id == "Sample Hills Apartments", f"Wrong property_id: {result.property_id}"
    assert len(result.unit_type_results) == 3, f"Expected 3 unit types, got {len(result.unit_type_results)}"
    assert len(result.unit_type_results) > 0

    # New layers must be present (no specific verdict assumptions — live metrics may shift)
    assert isinstance(result.sanity_flags, list), "sanity_flags must be a list"
    assert set(result.decision_summary) >= {"headline", "binding_constraints", "recommended_changes"}, (
        f"decision_summary missing required keys; got {set(result.decision_summary)}"
    )

    # -----------------------------------------------------------------
    # Task 3: Sweep smoke tests
    # -----------------------------------------------------------------
    from plat_agent.sweep import find_break_even, run_scenarios

    print(f"\n--- Scenario sweep (value_add) ---")
    t0 = time.time()
    scen = run_scenarios(inputs, preset="value_add", analysis=result)
    print(scen.summary)
    print(f"(elapsed {time.time() - t0:.1f}s)")
    assert scen.status in ("success", "roi_gate_failed"), f"bad status: {scen.status}"

    # Pick the first cohort_id from base_deal_inputs for the break-even demo
    cohort = inputs.base_deal_inputs["unit_cohorts"][0]["cohort_id"]
    in_place = inputs.base_deal_inputs["unit_cohorts"][0]["initial_inplace_rent"]
    bounds = (in_place * 1.05, in_place * 1.50)  # +5% to +50% over in-place

    print(f"\n--- Break-even: target_rent for {cohort} ---")
    t0 = time.time()
    be = find_break_even(
        inputs, axis="target_rent", bounds=bounds, cohort_id=cohort,
        objective="compound", analysis=result,
    )
    print(be.summary)
    print(f"(elapsed {time.time() - t0:.1f}s)")
    assert be.status in ("success", "not_bracketed", "roi_gate_failed"), f"bad status: {be.status}"

    print("\nSMOKE TEST PASSED")
    return result


if __name__ == "__main__":
    run()
