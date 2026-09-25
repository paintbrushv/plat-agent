import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from plat_agent.lifecycle.protocol import StepResult
from plat_agent.lifecycle.runner import read_state, run_lifecycle
from plat_agent.lifecycle.state import BlockerItem


def _data_room(tmp_path: Path) -> Path:
    src = tmp_path / "data_room"; src.mkdir(exist_ok=True)
    (src / "OM.pdf").write_bytes(b"x")
    return src


def _step(name: str, calls: list[str]):
    def run(state, run_dir):
        calls.append(name)
        (run_dir / name).mkdir(exist_ok=True)
        return StepResult(status="ok")
    m = MagicMock(); m.name = name; m.run.side_effect = run
    m.is_satisfied = MagicMock(return_value=True)  # Default cached
    return m


def _judgment(calls: list[str]):
    def run(state, run_dir):
        calls.append("judgment")
        jd = run_dir / "judgment"; jd.mkdir(exist_ok=True)
        (jd / "positioning.json").write_text(json.dumps({
            "positioning": {"confidence": 0.85},
            "leverage": {"source": "v1_hardcoded"}, "blockers": []}))
        return StepResult(status="ok")
    m = MagicMock(); m.name = "judgment"; m.run.side_effect = run
    m.is_satisfied = MagicMock(return_value=True); return m


def _uw(calls: list[str]):
    def run(state, run_dir):
        calls.append("underwriting")
        uw = run_dir / "underwriting"; uw.mkdir(exist_ok=True)
        (uw / "deal_summary.json").write_text(json.dumps({
            "metrics": {"irr": {"levered_irr": 0.15}, "dscr": {"minimum_dscr": 1.25}, "coc": {"cash_on_cash_year_1": 0.08}}}))
        return StepResult(status="ok")
    m = MagicMock(); m.name = "underwriting"; m.run.side_effect = run
    m.is_satisfied = MagicMock(return_value=True); return m


def test_rerun_from_comps_runs_comps_and_downstream(tmp_path: Path) -> None:
    project = tmp_path / "p"; project.mkdir()
    calls: list[str] = []
    steps = {
        "intake": _step("intake", calls),
        "comps": _step("comps", calls),
        "judgment": _judgment(calls),
        "underwriting": _uw(calls),
        "memo": _step("memo", calls),
        "crm": _step("crm", calls),
    }
    # Force first-run path
    for s in steps.values():
        s.is_satisfied.return_value = False
    first = run_lifecycle(_data_room(tmp_path), deal_slug="d",
                         project_root=project, steps=steps)

    calls.clear()
    # Mark all as satisfied — but rerun-from comps should ignore that for
    # comps and downstream
    for s in steps.values():
        s.is_satisfied.return_value = True

    run_lifecycle(_data_room(tmp_path), deal_slug="d",
                 project_root=project, steps=steps,
                 resume_run_id=first.run_id, rerun_from="comps")
    # intake skipped (cached)
    assert "intake" not in calls
    # comps + downstream all ran
    assert calls == ["comps", "judgment", "underwriting", "memo", "crm"]


def test_rerun_from_memo_never_reexecutes_invalidated_upstream_steps(
    tmp_path: Path,
) -> None:
    project = tmp_path / "p"
    project.mkdir()
    calls: list[str] = []
    steps = {
        "intake": _step("intake", calls),
        "comps": _step("comps", calls),
        "judgment": _judgment(calls),
        "underwriting": _uw(calls),
        "memo": _step("memo", calls),
        "crm": _step("crm", calls),
    }
    for step in steps.values():
        step.is_satisfied.return_value = False
    first = run_lifecycle(
        _data_room(tmp_path),
        deal_slug="d",
        project_root=project,
        steps=steps,
    )
    state_path = (
        project / "runs" / "deals" / "d" / "outputs" / first.run_id
        / "_lifecycle_state.json"
    )
    persisted = json.loads(state_path.read_text())
    persisted["steps_completed"].insert(4, "judgment")
    state_path.write_text(json.dumps(persisted))

    calls.clear()
    for name in ("intake", "comps", "judgment", "underwriting"):
        steps[name].is_satisfied.return_value = False

    run_lifecycle(
        _data_room(tmp_path),
        deal_slug="d",
        project_root=project,
        steps=steps,
        resume_run_id=first.run_id,
        rerun_from="memo",
    )

    assert calls == ["memo", "crm"]
    assert json.loads(state_path.read_text())["steps_completed"] == [
        "intake",
        "comps",
        "judgment",
        "underwriting",
        "memo",
        "crm",
    ]


def test_rerun_from_memo_passes_persisted_blockers_to_recommendation_patch(
    tmp_path: Path,
) -> None:
    from unittest.mock import patch

    project = tmp_path / "p"
    project.mkdir()
    calls: list[str] = []
    steps = {
        "intake": _step("intake", calls),
        "comps": _step("comps", calls),
        "judgment": _judgment(calls),
        "underwriting": _uw(calls),
        "memo": _step("memo", calls),
        "crm": _step("crm", calls),
    }
    for step in steps.values():
        step.is_satisfied.return_value = False
    first = run_lifecycle(
        _data_room(tmp_path),
        deal_slug="d",
        project_root=project,
        steps=steps,
    )
    state_path = (
        project / "runs" / "deals" / "d" / "outputs" / first.run_id
        / "_lifecycle_state.json"
    )
    state = json.loads(state_path.read_text())
    state["blockers"] = [
        {
            "step": "intake",
            "id": "missing_t12",
            "description": "Missing T12",
            "resolution_hint": None,
            "cleared": False,
            "created_at": None,
        }
    ]
    state_path.write_text(json.dumps(state))

    with patch("plat_agent.lifecycle.runner.patch_recommendation") as patched:
        run_lifecycle(
            _data_room(tmp_path),
            deal_slug="d",
            project_root=project,
            steps=steps,
            resume_run_id=first.run_id,
            rerun_from="memo",
        )

    assert patched.call_args.kwargs["blocker_count"] == 1


def test_rerun_from_removes_steps_from_steps_completed(tmp_path: Path) -> None:
    project = tmp_path / "p"; project.mkdir()
    calls: list[str] = []
    steps = {n: _step(n, calls) for n in ["intake", "comps", "memo", "crm"]}
    steps["judgment"] = _judgment(calls)
    steps["underwriting"] = _uw(calls)
    for s in steps.values():
        s.is_satisfied.return_value = False
    first = run_lifecycle(_data_room(tmp_path), deal_slug="d",
                         project_root=project, steps=steps)

    # Verify steps_completed was full
    state_path = (project / "runs" / "deals" / "d" / "outputs" / first.run_id /
                  "_lifecycle_state.json")
    pre = json.loads(state_path.read_text())
    assert pre["steps_completed"] == ["intake", "comps", "judgment",
                                      "underwriting", "memo", "crm"]

    # Rerun-from underwriting — when state is loaded, only intake/comps/judgment remain
    for s in steps.values():
        s.is_satisfied.return_value = True
    calls.clear()
    run_lifecycle(_data_room(tmp_path), deal_slug="d",
                 project_root=project, steps=steps,
                 resume_run_id=first.run_id, rerun_from="underwriting")
    assert calls == ["underwriting", "memo", "crm"]


def test_rerun_from_does_not_delete_cached_files(tmp_path: Path) -> None:
    """Per §4.5: --rerun-from does not delete output files (kept for diff)."""
    project = tmp_path / "p"; project.mkdir()
    calls: list[str] = []
    steps = {n: _step(n, calls) for n in ["intake", "comps", "memo", "crm"]}
    steps["judgment"] = _judgment(calls)
    steps["underwriting"] = _uw(calls)
    for s in steps.values():
        s.is_satisfied.return_value = False
    first = run_lifecycle(_data_room(tmp_path), deal_slug="d",
                         project_root=project, steps=steps)

    run_dir = project / "runs" / "deals" / "d" / "outputs" / first.run_id
    comps_file = run_dir / "comps"
    assert comps_file.exists()
    # Rerun from comps — directory should still be there before the new run
    # (Whether the new step run overwrites it is its own concern)
    run_lifecycle(_data_room(tmp_path), deal_slug="d",
                 project_root=project, steps=steps,
                 resume_run_id=first.run_id, rerun_from="comps")
    assert comps_file.exists()


def test_rerun_from_replaces_step_blockers_instead_of_accumulating(tmp_path: Path) -> None:
    project = tmp_path / "p"; project.mkdir()
    calls: list[str] = []

    def _intake_step():
        def run(state, run_dir):
            calls.append("intake")
            (run_dir / "intake").mkdir(exist_ok=True)
            return StepResult(
                status="blocked",
                blockers=[BlockerItem(step="intake", id="missing_t12", description="Missing T12")],
            )
        m = MagicMock(); m.name = "intake"; m.run.side_effect = run
        m.is_satisfied = MagicMock(return_value=False)
        return m

    steps = {
        "intake": _intake_step(),
        "comps": _step("comps", calls),
        "judgment": _judgment(calls),
        "underwriting": _uw(calls),
        "memo": _step("memo", calls),
        "crm": _step("crm", calls),
    }
    for s in steps.values():
        s.is_satisfied.return_value = False

    first = run_lifecycle(_data_room(tmp_path), deal_slug="d",
                          project_root=project, steps=steps)
    run_dir = project / "runs" / "deals" / "d" / "outputs" / first.run_id
    first_state = json.loads((run_dir / "_lifecycle_state.json").read_text())
    assert [b["id"] for b in first_state["blockers"]] == ["missing_t12"]

    calls.clear()
    run_lifecycle(_data_room(tmp_path), deal_slug="d",
                  project_root=project, steps=steps,
                  resume_run_id=first.run_id, rerun_from="intake")
    second_state = json.loads((run_dir / "_lifecycle_state.json").read_text())
    assert [b["id"] for b in second_state["blockers"]] == ["missing_t12"]


def test_read_state_normalizes_historical_duplicate_blockers(tmp_path: Path) -> None:
    run_dir = tmp_path / "outputs" / "run_001"
    run_dir.mkdir(parents=True)
    state_path = run_dir / "_lifecycle_state.json"
    state_path.write_text(json.dumps({
        "deal_slug": "d",
        "run_id": "run_001",
        "status": "memo_ready_with_blockers",
        "steps_completed": ["intake"],
        "blockers": [
            {"step": "intake", "id": "missing_t12", "description": "Missing T12"},
            {"step": "intake", "id": "missing_t12", "description": "Missing T12"},
            {"step": "comps", "id": "scraper_blocked", "description": "Blocked"},
        ],
    }))

    state = read_state(run_dir)

    assert [(b.step, b.id) for b in state.blockers] == [
        ("intake", "missing_t12"),
        ("comps", "scraper_blocked"),
    ]
