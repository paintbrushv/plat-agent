"""CLI interface for Plat deal analysis."""

import json
import sys

import click

from .analyzer import analyze_deal
from .models import DealInputs, UnitMixEntry


@click.group()
def cli():
    """Plat — multifamily deal analysis agent."""


@cli.command("analyze")
@click.option("--deal-file", required=True, type=click.Path(exists=True),
              help="Path to deal JSON file (DealInputs schema)")
@click.option("--json-output", is_flag=True, default=False,
              help="Output full JSON instead of human summary")
@click.option("--costmodel-cmd", default=None,
              help="Override plat-costmodel server command (space-separated)")
@click.option("--full-cashflow", is_flag=True, default=False,
              help="Run the underwriting engine's full-cashflow path (direct import) "
                   "instead of the lightweight MCP summary. Requires base_deal_inputs.")
@click.option("--comps-file", default=None, type=click.Path(exists=True),
              help="Optional comp set JSON (comps_by_cohort dict, or "
                   "{comps_by_cohort: ...}). When provided, target_monthly_rent "
                   "is validated per cohort; above_band findings appear in "
                   "decision_summary.binding_constraints.")
def analyze_cmd(deal_file, json_output, costmodel_cmd, full_cashflow, comps_file):
    """Analyze a deal from a JSON input file."""
    from .costmodel_client import CostModelClient

    with open(deal_file) as f:
        raw = json.load(f)

    if full_cashflow:
        raw["full_cashflow"] = True

    try:
        inputs = DealInputs(**raw)
    except Exception as e:
        click.echo(f"Invalid deal input: {e}", err=True)
        sys.exit(1)

    comps_by_cohort = None
    if comps_file:
        with open(comps_file) as f:
            comps_raw = json.load(f)
        comps_by_cohort = comps_raw.get("comps_by_cohort", comps_raw)

    server_command = costmodel_cmd.split() if costmodel_cmd else None
    client = CostModelClient(server_command=server_command)

    try:
        result = analyze_deal(inputs, client=client, comps_by_cohort=comps_by_cohort)
    except Exception as e:
        click.echo(f"Analysis failed: {e}", err=True)
        sys.exit(1)

    if json_output:
        click.echo(result.model_dump_json(indent=2))
    else:
        click.echo(result.summary)
        if not result.ready_to_underwrite:
            click.echo("\nROI gate failed for one or more unit types. See --json-output for details.")
            sys.exit(1)


@cli.command("validate-rents")
@click.option("--deal-file", required=True, type=click.Path(exists=True),
              help="Path to deal JSON file (DealInputs schema). Proposed "
                   "target rents are read from unit_mix[].target_monthly_rent.")
@click.option("--comps-file", required=True, type=click.Path(exists=True),
              help="Path to JSON with comps_by_cohort dict (e.g. comp-finder "
                   "output). Either {comps_by_cohort: {...}} or just the "
                   "cohort-keyed dict at the top level.")
@click.option("--json-output", is_flag=True, default=False)
def validate_rents_cmd(deal_file, comps_file, json_output):
    """Validate per-cohort target_monthly_rent against a comp set."""
    from .contracts.domain.market_study import (
        ProposedRent, RentValidationRequest,
    )
    from .rent_validation import validate_target_rents

    with open(deal_file) as f:
        deal_raw = json.load(f)
    try:
        inputs = DealInputs(**deal_raw)
    except Exception as e:
        click.echo(f"Invalid deal input: {e}", err=True)
        sys.exit(1)

    with open(comps_file) as f:
        comps_raw = json.load(f)
    comps_by_cohort = comps_raw.get("comps_by_cohort", comps_raw)

    proposed = [
        ProposedRent(
            bedrooms=u.bedrooms, bathrooms=u.bathrooms, sqft=u.sqft,
            proposed_target_monthly_rent=u.target_monthly_rent,
        )
        for u in inputs.unit_mix
    ]

    try:
        request = RentValidationRequest(
            proposed_rents=proposed, comps_by_cohort=comps_by_cohort,
        )
    except Exception as e:
        click.echo(f"Invalid comps payload: {e}", err=True)
        sys.exit(1)

    response = validate_target_rents(request)

    if json_output:
        click.echo(response.model_dump_json(indent=2))
        return

    click.echo(response.summary)
    for f in response.findings:
        click.echo(f"  [{f.verdict}|{f.confidence}] {f.cohort_key}: {f.message}")

    # Non-zero exit if any cohort is above_band — analyst should re-check.
    if any(f.verdict == "above_band" for f in response.findings):
        sys.exit(2)


@cli.command("synthesize-decision")
@click.option("--deal-root", required=True, type=click.Path(exists=True, file_okay=False),
              help="Deal root directory, e.g. multifamily-underwriting/runs/deals/<slug>/")
@click.option("--run-id", required=True,
              help="Federated run id, e.g. run_002_federation. Resolves to "
                   "<deal-root>/outputs/<run-id>/.")
@click.option("--threshold-pct", type=float, default=15.0, show_default=True,
              help="ROI gate threshold for per-cohort recomputation. "
                   "Defaults to plat-costmodel's DEFAULT_THRESHOLD_PCT.")
@click.option("--json-output", is_flag=True, default=False,
              help="Echo the synthesized decision_summary to stdout in addition "
                   "to writing the artifact.")
def synthesize_decision_cmd(deal_root, run_id, threshold_pct, json_output):
    """Read federated artifacts and emit decision_summary.json.

    Reads costmodel + underwriting artifacts under <deal-root>/outputs/<run-id>/
    and writes a synthesized decision_summary.json next to them. Designed to
    run between underwriting-orchestrator and deal-memo-writer in the
    federated chain. Fail-soft: missing artifacts contribute nothing and are
    listed under data_gaps.
    """
    from pathlib import Path
    from .dispatch import atomic_write_json
    from .synthesize import synthesize_from_federated_artifacts

    deal_root_path = Path(deal_root).resolve()
    summary = synthesize_from_federated_artifacts(
        deal_root_path, run_id, threshold_pct=threshold_pct,
    )

    out_path = deal_root_path / "outputs" / run_id / "decision_summary.json"
    if not out_path.parent.exists():
        click.echo(f"Run directory does not exist: {out_path.parent}", err=True)
        sys.exit(1)

    atomic_write_json(out_path, summary)
    click.echo(f"Wrote {out_path}")

    if json_output:
        click.echo(json.dumps(summary, indent=2))

    # Non-zero exit only if literally nothing was readable — a deliberately
    # mild signal so the orchestrator can still continue the chain.
    if len(summary["data_gaps"]) >= 4:
        sys.exit(2)


@cli.command("check-inputs")
@click.option("--deal-file", required=True, type=click.Path(exists=True),
              help="Path to deal JSON file to validate")
def check_inputs_cmd(deal_file):
    """Validate deal input JSON without running the full analysis."""
    with open(deal_file) as f:
        raw = json.load(f)

    try:
        inputs = DealInputs(**raw)
        click.echo(f"Valid. {inputs.property_id}: {inputs.total_units} units, "
                   f"{len(inputs.unit_mix)} unit type(s)")
    except Exception as e:
        click.echo(f"Invalid: {e}", err=True)
        sys.exit(1)


# ---------------------------------------------------------------------------
# Sweep subcommands
# ---------------------------------------------------------------------------

@cli.group("sweep")
def sweep_group():
    """Sensitivity and scenario sweeps on a deal."""


@sweep_group.command("scenarios")
@click.option("--deal-file", required=True, type=click.Path(exists=True),
              help="Path to deal JSON file (DealInputs schema)")
@click.option("--preset", type=click.Choice(["value_add", "stabilized"]),
              default="value_add", show_default=True)
@click.option("--json-output", is_flag=True, default=False,
              help="Output full JSON instead of human summary")
@click.option("--backend", type=click.Choice(["local", "azure", "auto"]),
              default="auto", show_default=True,
              help="Underwriting backend. 'auto' picks azure if "
                   "UNDERWRITING_AZURE_ENDPOINT is set, else local.")
@click.option("--concurrency", type=click.IntRange(min=1), default=1, show_default=True,
              help="Max parallel scenarios. Most useful with --backend azure "
                   "(local backend serializes through one MCP worker).")
def sweep_scenarios_cmd(deal_file, preset, json_output, backend, concurrency):
    """Run Bull/Base/Bear scenario sweep on a deal."""
    from .sweep import make_backend, run_scenarios

    with open(deal_file) as f:
        raw = json.load(f)

    try:
        inputs = DealInputs(**raw)
    except Exception as e:
        click.echo(f"Invalid deal input: {e}", err=True)
        sys.exit(1)

    try:
        backend_obj = make_backend(backend)
    except ValueError as e:
        click.echo(f"Backend error: {e}", err=True)
        sys.exit(1)

    try:
        result = run_scenarios(
            inputs, preset=preset, backend=backend_obj, concurrency=concurrency,
        )
    except ValueError as e:
        click.echo(f"Sweep failed: {e}", err=True)
        sys.exit(1)

    if json_output:
        click.echo(result.model_dump_json(indent=2))
    else:
        click.echo(result.summary)

    if result.status != "success":
        sys.exit(1)


@sweep_group.command("break-even")
@click.option("--deal-file", required=True, type=click.Path(exists=True),
              help="Path to deal JSON file (DealInputs schema)")
@click.option("--axis", required=True,
              type=click.Choice(["target_rent", "exit_cap", "monthly_pace"]))
@click.option("--bounds", required=True,
              help="Search bounds 'lo,hi' (e.g. '900,1400' for rent, '0.04,0.09' for cap)")
@click.option("--cohort", default=None,
              help="cohort_id (required when --axis=target_rent)")
@click.option("--objective",
              type=click.Choice(["compound", "levered_irr", "min_dscr"]),
              default="compound", show_default=True)
@click.option("--irr-threshold", type=float, default=0.12, show_default=True)
@click.option("--dscr-threshold", type=float, default=1.20, show_default=True)
@click.option("--tolerance", type=float, default=0.005, show_default=True)
@click.option("--max-iterations", type=int, default=20, show_default=True)
@click.option("--json-output", is_flag=True, default=False)
@click.option("--backend", type=click.Choice(["local", "azure", "auto"]),
              default="auto", show_default=True,
              help="Underwriting backend. 'auto' picks azure if "
                   "UNDERWRITING_AZURE_ENDPOINT is set, else local.")
def sweep_break_even_cmd(deal_file, axis, bounds, cohort, objective,
                         irr_threshold, dscr_threshold, tolerance,
                         max_iterations, json_output, backend):
    """Find the break-even axis value that clears the hurdle(s)."""
    from .sweep import find_break_even, make_backend

    with open(deal_file) as f:
        raw = json.load(f)

    try:
        inputs = DealInputs(**raw)
    except Exception as e:
        click.echo(f"Invalid deal input: {e}", err=True)
        sys.exit(1)

    try:
        lo_str, hi_str = bounds.split(",")
        bounds_tuple = (float(lo_str), float(hi_str))
    except ValueError:
        click.echo(f"--bounds must be 'lo,hi' (got {bounds!r})", err=True)
        sys.exit(1)

    try:
        backend_obj = make_backend(backend)
    except ValueError as e:
        click.echo(f"Backend error: {e}", err=True)
        sys.exit(1)

    try:
        result = find_break_even(
            inputs, axis=axis, bounds=bounds_tuple,
            cohort_id=cohort, objective=objective,
            irr_threshold=irr_threshold, dscr_threshold=dscr_threshold,
            tolerance=tolerance, max_iterations=max_iterations,
            backend=backend_obj,
        )
    except ValueError as e:
        click.echo(f"Break-even failed: {e}", err=True)
        sys.exit(1)

    if json_output:
        click.echo(result.model_dump_json(indent=2))
    else:
        click.echo(result.summary)

    if result.status != "success":
        sys.exit(1)


# Register lifecycle subcommand
from plat_agent.lifecycle.cli import lifecycle_cmd  # noqa: E402
cli.add_command(lifecycle_cmd)
