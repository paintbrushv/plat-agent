"""§5.3 #1 -- clean deal runs end-to-end, memo + CRM produced, exit code 0."""

from __future__ import annotations

import json
from pathlib import Path

from plat_agent.lifecycle.runner import run_lifecycle


def test_lifecycle_runs_clean_deal_end_to_end(
    clean_data_room: Path,
    project_root: Path,
    default_steps: dict,
) -> None:
    result = run_lifecycle(
        clean_data_room,
        deal_slug="project_essex",
        project_root=project_root,
        steps=default_steps,
    )

    # Pipeline reached the end
    assert result.status == "memo_ready"
    assert result.exit_code == 0
    assert result.deal_slug == "project_essex"

    # All six steps invoked exactly once
    for step_name in ["intake", "comps", "judgment", "underwriting", "memo", "crm"]:
        assert default_steps[step_name].run.call_count == 1, \
            f"{step_name} run not invoked exactly once"

    # Memo artifacts on disk
    run_dir = (project_root / "runs" / "deals" / "project_essex"
               / "outputs" / result.run_id)
    assert (run_dir / "memo" / "memo.md").exists()
    assert (run_dir / "memo" / "memo.pdf").exists()

    # CRM row appended
    crm = (project_root / "runs" / "deals" / "_crm.jsonl").read_text().strip().splitlines()
    assert len(crm) == 1
    row = json.loads(crm[0])
    assert row["deal_slug"] == "project_essex"
    assert row["status"] == "memo_ready"
    assert row["recommendation"] in {"PROCEED", "DECLINE", "NEEDS_DATA"}


def test_lifecycle_clean_deal_writes_complete_markers(
    clean_data_room: Path,
    project_root: Path,
    default_steps: dict,
) -> None:
    result = run_lifecycle(
        clean_data_room,
        deal_slug="project_essex",
        project_root=project_root,
        steps=default_steps,
    )
    run_dir = (project_root / "runs" / "deals" / "project_essex"
               / "outputs" / result.run_id)
    for step in ["intake", "comps", "judgment", "underwriting", "memo"]:
        assert (run_dir / step / "_complete").exists(), \
            f"{step} missing _complete"
