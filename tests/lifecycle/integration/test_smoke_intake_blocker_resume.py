"""§5.3 #2 -- intake blocker -> punchlist -> drop file -> --resume -> clear."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from plat_agent.lifecycle.atomic import atomic_write_json
from plat_agent.lifecycle.cache import write_provenance
from plat_agent.lifecycle.complete_marker import write_complete_marker
from plat_agent.lifecycle.protocol import StepResult
from plat_agent.lifecycle.punchlist import (
    read_punchlist_json,
    write_punchlist_json,
)
from plat_agent.lifecycle.runner import run_lifecycle
from plat_agent.lifecycle.state import BlockerItem


def test_lifecycle_handles_intake_blocker_and_resumes(
    missing_t12_data_room: Path,
    project_root: Path,
    make_default_steps,
) -> None:
    """Intake emits a missing_t12 blocker; resume clears it once T12 is dropped in."""

    call_state = {"n": 0}

    def intake_run(state, run_dir):
        d = run_dir / "intake"
        d.mkdir(exist_ok=True)
        atomic_write_json(d / "canonical_deal.json", {
            "metadata": {"address": "1234 Main St, Dallas, TX 75201", "year_built": 2005,
                         "market": "dallas_tx"},
            "unit_cohorts": [{"cohort_id": "1br", "unit_count": 240}],
            "purchase_assumptions": {"purchase_price": 28_000_000},
        })
        write_provenance(d, input_hash="x", status="ok")
        write_complete_marker(
            d, step="intake",
            file_manifest=["canonical_deal.json", "_provenance.json"],
        )
        call_state["n"] += 1
        if call_state["n"] == 1:
            blocker = BlockerItem(
                step="intake", id="missing_t12",
                description="No T12 file found in raw_inputs/.",
            )
            # Write the run-scoped punchlist (real intake step does this)
            write_punchlist_json(run_dir, [blocker])
            return StepResult(status="blocked", blockers=[blocker])
        # On resume: intake re-runs and clears the blocker by writing an
        # empty punchlist (or marking cleared). Emulate the real intake's
        # behavior where reconcile picks up that the file is now present.
        write_punchlist_json(run_dir, [])
        return StepResult(status="ok")

    intake_step = MagicMock()
    intake_step.name = "intake"
    intake_step.run.side_effect = intake_run
    # is_satisfied controls --resume cache check; first pass not satisfied,
    # second pass also re-runs intake to clear the blocker
    intake_step.is_satisfied = MagicMock(return_value=False)

    steps = make_default_steps()
    steps["intake"] = intake_step

    # First run -- emits blocker, finishes with memo_ready_with_blockers
    first = run_lifecycle(
        missing_t12_data_room,
        deal_slug="project_essex",
        project_root=project_root,
        steps=steps,
    )
    assert first.status == "memo_ready_with_blockers"
    # Per §4.3 row 2 spec: intake blocker -> exit 0 (analyst-actionable)
    assert first.exit_code == 0

    run_dir = (project_root / "runs" / "deals" / "project_essex"
               / "outputs" / first.run_id)
    blockers = read_punchlist_json(run_dir)
    assert any(b.id == "missing_t12" and not b.cleared for b in blockers)

    # Drop T12 into the run-scoped raw_inputs/
    from tests.fixtures.lifecycle.builders import write_clean_t12
    write_clean_t12(run_dir / "raw_inputs" / "T12.csv")

    # Resume -- intake runs again (call #2 returns ok), blocker is cleared
    second_steps = make_default_steps()
    second_steps["intake"] = intake_step  # same mock keeps call_state across resume

    second = run_lifecycle(
        missing_t12_data_room,
        deal_slug="project_essex",
        project_root=project_root,
        steps=second_steps,
        resume_run_id=first.run_id,
    )
    assert second.status == "memo_ready"
    assert second.run_id == first.run_id  # same run

    blockers_after = read_punchlist_json(run_dir)
    # All `missing_t12` blockers either cleared or absent after resume
    assert all(b.cleared for b in blockers_after if b.id == "missing_t12")
