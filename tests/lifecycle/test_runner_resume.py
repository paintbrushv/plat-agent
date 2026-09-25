import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from plat_agent.lifecycle.protocol import StepResult
from plat_agent.lifecycle.runner import run_lifecycle


def _data_room(tmp_path: Path) -> Path:
    src = tmp_path / "data_room"; src.mkdir(exist_ok=True)
    (src / "OM.pdf").write_bytes(b"x")
    return src


def _step(name: str, run_count: list[int]):
    def run(state, run_dir):
        run_count.append(name)
        (run_dir / name).mkdir(exist_ok=True)
        return StepResult(status="ok")
    m = MagicMock(); m.name = name; m.run.side_effect = run
    m.is_satisfied = MagicMock(return_value=False)
    return m


def _judgment_step(run_count: list[int]):
    def run(state, run_dir):
        run_count.append("judgment")
        jd = run_dir / "judgment"; jd.mkdir(exist_ok=True)
        (jd / "positioning.json").write_text(json.dumps({
            "positioning": {"confidence": 0.85},
            "leverage": {"source": "v1_hardcoded"}, "blockers": []}))
        return StepResult(status="ok")
    m = MagicMock(); m.name = "judgment"; m.run.side_effect = run
    m.is_satisfied = MagicMock(return_value=False); return m


def _uw_step(run_count: list[int]):
    def run(state, run_dir):
        run_count.append("underwriting")
        uw = run_dir / "underwriting"; uw.mkdir(exist_ok=True)
        (uw / "deal_summary.json").write_text(json.dumps({
            "metrics": {"irr": {"levered_irr": 0.15}, "dscr": {"minimum_dscr": 1.25}, "coc": {"cash_on_cash_year_1": 0.08}}}))
        return StepResult(status="ok")
    m = MagicMock(); m.name = "underwriting"; m.run.side_effect = run
    m.is_satisfied = MagicMock(return_value=False); return m


def test_resume_skips_satisfied_steps(tmp_path: Path) -> None:
    """All steps satisfied (cached) → none re-run."""
    project = tmp_path / "p"; project.mkdir()
    counts: list[str] = []
    steps = {
        "intake": _step("intake", counts),
        "comps": _step("comps", counts),
        "judgment": _judgment_step(counts),
        "underwriting": _uw_step(counts),
        "memo": _step("memo", counts),
        "crm": _step("crm", counts),
    }
    # First run
    first = run_lifecycle(_data_room(tmp_path), deal_slug="d",
                         project_root=project, steps=steps)

    # Reset counts. Make all is_satisfied() return True
    counts.clear()
    for s in steps.values():
        s.is_satisfied.return_value = True

    # Resume run
    second = run_lifecycle(_data_room(tmp_path), deal_slug="d",
                          project_root=project, steps=steps,
                          resume_run_id=first.run_id)
    # No step's run() called
    assert counts == []
    assert second.run_id == first.run_id


def test_resume_runs_only_unsatisfied_steps(tmp_path: Path) -> None:
    """intake/comps satisfied; judgment onward not satisfied → only those rerun."""
    project = tmp_path / "p"; project.mkdir()
    counts: list[str] = []
    steps = {
        "intake": _step("intake", counts),
        "comps": _step("comps", counts),
        "judgment": _judgment_step(counts),
        "underwriting": _uw_step(counts),
        "memo": _step("memo", counts),
        "crm": _step("crm", counts),
    }
    # First run
    first = run_lifecycle(_data_room(tmp_path), deal_slug="d",
                         project_root=project, steps=steps)
    counts.clear()
    # Mark intake and comps satisfied; everything else unsatisfied
    steps["intake"].is_satisfied.return_value = True
    steps["comps"].is_satisfied.return_value = True
    steps["judgment"].is_satisfied.return_value = False
    steps["underwriting"].is_satisfied.return_value = False
    steps["memo"].is_satisfied.return_value = False
    steps["crm"].is_satisfied.return_value = False

    run_lifecycle(_data_room(tmp_path), deal_slug="d",
                 project_root=project, steps=steps,
                 resume_run_id=first.run_id)
    assert "intake" not in counts
    assert "comps" not in counts
    assert "judgment" in counts
    assert "underwriting" in counts
    assert "memo" in counts
    assert "crm" in counts


def test_resume_reconciles_punchlist_before_running_steps(tmp_path: Path,
                                                          monkeypatch) -> None:
    """When --resume runs, punchlist.md edits are reconciled into JSON first."""
    from plat_agent.lifecycle import punchlist as pl

    project = tmp_path / "p"; project.mkdir()
    counts: list[str] = []
    steps = {
        "intake": _step("intake", counts),
        "comps": _step("comps", counts),
        "judgment": _judgment_step(counts),
        "underwriting": _uw_step(counts),
        "memo": _step("memo", counts),
        "crm": _step("crm", counts),
    }
    # First run with intake blocker
    first = run_lifecycle(_data_room(tmp_path), deal_slug="d",
                         project_root=project, steps=steps)
    run_dir = project / "runs" / "deals" / "d" / "outputs" / first.run_id

    # Inject a punchlist.json with one uncleared blocker
    from plat_agent.lifecycle.state import BlockerItem
    pl.write_punchlist_json(run_dir, [
        BlockerItem(step="intake", id="missing_t12",
                    description="No T12 found.", cleared=False)
    ])
    # Analyst edits the markdown to clear it
    (run_dir / "punchlist.md").write_text(
        "# Punchlist for d / r\n\n## intake\n- [x] **missing_t12**: No T12 found.\n"
    )

    # Resume — should reconcile and clear
    counts.clear()
    for s in steps.values():
        s.is_satisfied.return_value = True
    run_lifecycle(_data_room(tmp_path), deal_slug="d",
                 project_root=project, steps=steps,
                 resume_run_id=first.run_id)
    reconciled = pl.read_punchlist_json(run_dir)
    assert reconciled[0].cleared is True
