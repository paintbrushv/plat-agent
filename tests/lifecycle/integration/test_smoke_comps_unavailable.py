"""§5.3 #3 -- comps hard error; judgment proceeds OM-only; rec = NEEDS_DATA."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

from plat_agent.lifecycle.cache import write_provenance
from plat_agent.lifecycle.complete_marker import write_complete_marker
from plat_agent.lifecycle.protocol import StepResult
from plat_agent.lifecycle.runner import run_lifecycle


def test_lifecycle_handles_comps_unavailable(
    clean_data_room: Path,
    project_root: Path,
    make_default_steps,
) -> None:
    # Comps step returns hard error -> orchestrator marks comps_unavailable
    # and forces recommendation to NEEDS_DATA via the recommendation patch.
    def comps_error_run(state, run_dir):
        d = run_dir / "comps"
        d.mkdir(exist_ok=True)
        write_provenance(d, input_hash="x", status="error",
                          extra={"error": "scraper 403"})
        return StepResult(status="error", error="scraper 403")

    comps_step = MagicMock()
    comps_step.name = "comps"
    comps_step.run.side_effect = comps_error_run
    comps_step.is_satisfied = MagicMock(return_value=False)

    steps = make_default_steps()
    steps["comps"] = comps_step

    result = run_lifecycle(
        clean_data_room,
        deal_slug="project_essex",
        project_root=project_root,
        steps=steps,
    )
    assert result.status == "memo_ready_with_blockers"
    assert result.exit_code == 0

    run_dir = (project_root / "runs" / "deals" / "project_essex"
               / "outputs" / result.run_id)

    # Judgment positioning recommendation -> NEEDS_DATA after comps_unavailable patch
    positioning = json.loads((run_dir / "judgment" / "positioning.json").read_text())
    assert positioning["recommendation"] == "NEEDS_DATA"

    # Memo prominently flags comps gap (memo factory adds an "unavailable" line
    # whenever the comps step is in error state)
    memo = (run_dir / "memo" / "memo.md").read_text()
    assert "needs_data" in memo.lower() or "unavailable" in memo.lower()
