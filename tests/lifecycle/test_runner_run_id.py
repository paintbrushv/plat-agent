from pathlib import Path

import pytest

from plat_agent.lifecycle.runner import LifecycleResult, allocate_run_id, deal_outputs_dir


def test_lifecycle_result_fields() -> None:
    r = LifecycleResult(
        deal_slug="project_essex",
        run_id="run_001",
        status="memo_ready",
        memo_path=Path("/x/memo.md"),
        deal_summary_path=Path("/x/deal_summary.json"),
        crm_row_appended=True,
        blocker_count=0,
        exit_code=0,
    )
    assert r.deal_slug == "project_essex"
    assert r.exit_code == 0


def test_allocate_run_id_first_run(tmp_path: Path) -> None:
    outputs_dir = tmp_path / "outputs"
    rid = allocate_run_id(outputs_dir)
    assert rid == "run_001"


def test_allocate_run_id_increments(tmp_path: Path) -> None:
    outputs_dir = tmp_path / "outputs"
    (outputs_dir / "run_001").mkdir(parents=True)
    (outputs_dir / "run_002").mkdir()
    (outputs_dir / "run_007").mkdir()
    rid = allocate_run_id(outputs_dir)
    assert rid == "run_008"  # max + 1


def test_allocate_run_id_skips_non_run_dirs(tmp_path: Path) -> None:
    outputs_dir = tmp_path / "outputs"
    (outputs_dir / "run_001").mkdir(parents=True)
    (outputs_dir / "scratch").mkdir()
    (outputs_dir / "RUN_ARCHIVE").mkdir()
    rid = allocate_run_id(outputs_dir)
    assert rid == "run_002"


def test_deal_outputs_dir_uses_runs_deals_convention(tmp_path: Path) -> None:
    project_root = tmp_path
    out = deal_outputs_dir(project_root, "project_essex")
    assert out == project_root / "runs" / "deals" / "project_essex" / "outputs"
