"""§5.3 #4 -- --rerun-from comps invalidates comps + downstream, preserves intake."""

from __future__ import annotations

import time
from pathlib import Path

from plat_agent.lifecycle.runner import run_lifecycle


def test_lifecycle_rerun_from_comps_overwrites_downstream(
    clean_data_room: Path,
    project_root: Path,
    make_default_steps,
) -> None:
    comps_calls: list[int] = []
    steps = make_default_steps(comps_call_counter=comps_calls)

    first = run_lifecycle(
        clean_data_room,
        deal_slug="project_essex",
        project_root=project_root,
        steps=steps,
    )
    run_dir = (project_root / "runs" / "deals" / "project_essex"
               / "outputs" / first.run_id)

    intake_complete_mtime = (run_dir / "intake" / "_complete").stat().st_mtime
    comps_complete_mtime = (run_dir / "comps" / "_complete").stat().st_mtime
    memo_md_mtime = (run_dir / "memo" / "memo.md").stat().st_mtime

    # Sleep briefly so mtimes can move forward observably (HFS+/APFS have
    # ~1-second mtime resolution on some configurations)
    time.sleep(1.1)

    # On --resume the orchestrator's is_satisfied() check decides whether to
    # re-run; intake should be cached (skipped), comps + downstream rerun.
    # The make_default_steps factory returns mocks with is_satisfied=False,
    # so we tighten intake's is_satisfied to True so it acts as cached.
    second_steps = make_default_steps(comps_call_counter=comps_calls)
    second_steps["intake"].is_satisfied = lambda state, run_dir: True

    second = run_lifecycle(
        clean_data_room,
        deal_slug="project_essex",
        project_root=project_root,
        steps=second_steps,
        resume_run_id=first.run_id,
        rerun_from="comps",
    )
    assert second.run_id == first.run_id
    assert second.status == "memo_ready"

    # Intake preserved (same mtime — was not re-run)
    assert (run_dir / "intake" / "_complete").stat().st_mtime == intake_complete_mtime
    # Comps + downstream re-written (mtime moved forward)
    assert (run_dir / "comps" / "_complete").stat().st_mtime > comps_complete_mtime
    assert (run_dir / "memo" / "memo.md").stat().st_mtime > memo_md_mtime

    # comps was called twice total (once per run)
    assert len(comps_calls) == 2
