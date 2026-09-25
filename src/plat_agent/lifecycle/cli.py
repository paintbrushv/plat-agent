"""`plat lifecycle <data_room>` CLI command (spec §1)."""

from __future__ import annotations

import sys
import os
from pathlib import Path

import click

from plat_agent.lifecycle.runner import run_lifecycle


_VALID_STEPS = ["intake", "comps", "judgment", "underwriting", "memo", "crm"]


def _resolve_project_root(project_root: Path | None) -> Path | None:
    """Resolve the lifecycle project root for CLI runs.

    Preference order:
    1. Explicit ``--project-root``.
    2. ``PLAT_LIFECYCLE_PROJECT_ROOT`` env var.
    3. If running from the ``plat-agent`` repo and a sibling
       ``multifamily-underwriting`` repo exists, use that sibling so CLI runs
       land in the canonical deal workspace by default.
    4. Otherwise let ``run_lifecycle()`` fall back to ``Path.cwd()``.
    """
    if project_root is not None:
        return project_root

    env_root = os.getenv("PLAT_LIFECYCLE_PROJECT_ROOT")
    if env_root:
        return Path(env_root).expanduser().resolve()

    cwd = Path.cwd().resolve()
    sibling_mfu = cwd.parent / "multifamily-underwriting"
    if cwd.name == "plat-agent" and (sibling_mfu / "engine").exists():
        return sibling_mfu

    return None


@click.command("lifecycle")
@click.argument("data_room", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option("--resume", "resume_spec", default=None,
              help="Resume an existing run. Format: <deal_slug>/<run_id>")
@click.option("--rerun-from", default=None,
              type=click.Choice(_VALID_STEPS),
              help="With --resume: force-rerun this step + downstream.")
@click.option("--address", "address", default=None,
              help="Override canonical_deal.json metadata.address. Use when "
                   "OM extraction can't determine the subject address (e.g. "
                   "scanned OM, missing OM).")
@click.option("--market", "market", default=None,
              help="Override canonical_deal.json metadata.market. Required "
                   "when comp-finder cannot resolve a metro from intake "
                   "(e.g. 'DFW', 'birmingham_al').")
@click.option(
    "--millage-rate",
    "millage_rate",
    default=None,
    help="Combined property-tax mills per $1,000 of assessed value, e.g. 25.31.",
)
@click.option("--project-root", "project_root", default=None,
              type=click.Path(file_okay=False, path_type=Path),
              envvar="PLAT_LIFECYCLE_PROJECT_ROOT",
              help="Override the lifecycle run root. Defaults to the sibling "
                   "`multifamily-underwriting` repo when invoked from "
                   "`plat-agent`, otherwise falls back to the current working "
                   "directory.")
def lifecycle_cmd(data_room: Path,
                  resume_spec: str | None,
                  rerun_from: str | None,
                  address: str | None,
                  market: str | None,
                  millage_rate: str | None,
                  project_root: Path | None) -> None:
    """Run the full deal lifecycle for the given data room.

    Without --resume, allocates a new run_id and runs all 6 steps.
    With --resume <slug>/<run_id>, re-enters an existing run, skipping
    cached steps. Add --rerun-from <step> to force-rerun a step + downstream.

    --address / --market: V1.3 analyst-supplied subject metadata. Patched
    into canonical_deal.json after intake completes, before comps runs.
    Use when intake can't extract these fields (e.g. scanned OM PDF).
    """
    if rerun_from and not resume_spec:
        raise click.UsageError("--rerun-from requires --resume")

    deal_slug: str | None = None
    resume_run_id: str | None = None
    if resume_spec:
        if "/" not in resume_spec:
            raise click.UsageError("--resume format: <deal_slug>/<run_id>")
        deal_slug, resume_run_id = resume_spec.split("/", 1)

    resolved_project_root = _resolve_project_root(project_root)
    result = run_lifecycle(
        data_room=data_room,
        deal_slug=deal_slug,
        resume_run_id=resume_run_id,
        rerun_from=rerun_from,
        address=address,
        market=market,
        millage_rate=millage_rate,
        project_root=resolved_project_root,
    )

    click.echo(f"deal_slug:    {result.deal_slug}")
    click.echo(f"run_id:       {result.run_id}")
    click.echo(f"status:       {result.status}")
    click.echo(f"blockers:     {result.blocker_count}")
    if result.memo_path:
        click.echo(f"memo:         {result.memo_path}")
    if result.deal_summary_path:
        click.echo(f"deal_summary: {result.deal_summary_path}")
    if result.crm_row_appended:
        click.echo("crm:          row appended")

    sys.exit(result.exit_code)
