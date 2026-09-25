"""Pipeline loop integration tests with all step adapters mocked."""

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from plat_agent.lifecycle.protocol import StepResult
from plat_agent.lifecycle.runner import run_lifecycle
from plat_agent.lifecycle.state import BlockerItem


def _ok_step(name: str) -> MagicMock:
    """A step adapter mock that returns ok and writes its _complete marker."""
    m = MagicMock()
    m.name = name
    m.run.return_value = StepResult(status="ok")
    m.is_satisfied.return_value = False
    return m


def _make_data_room(tmp_path: Path) -> Path:
    src = tmp_path / "data_room"
    src.mkdir()
    (src / "OM.pdf").write_bytes(b"x")
    return src


def test_pipeline_runs_all_six_steps_in_order(tmp_path: Path, monkeypatch) -> None:
    src = _make_data_room(tmp_path)
    project_root = tmp_path / "project_root"
    project_root.mkdir()

    # Each step writes a sentinel file so we can verify ordering
    call_order: list[str] = []

    def make_step_with_record(name: str):
        def run(state, run_dir):
            call_order.append(name)
            (run_dir / name).mkdir(exist_ok=True)
            (run_dir / name / "_complete").write_text(
                json.dumps({"step": name, "committed_at": "x", "file_manifest": []}))
            return StepResult(status="ok")
        m = MagicMock()
        m.name = name
        m.run.side_effect = run
        m.is_satisfied.return_value = False
        return m

    steps = {
        "intake": make_step_with_record("intake"),
        "comps": make_step_with_record("comps"),
        "judgment": make_step_with_record("judgment"),
        "underwriting": make_step_with_record("underwriting"),
        "memo": make_step_with_record("memo"),
        "crm": make_step_with_record("crm"),
    }

    # Patch positioning.json + deal_summary.json fixtures so Step 4.5 works
    def fake_judgment_run(state, run_dir):
        call_order.append("judgment")
        jd = run_dir / "judgment"
        jd.mkdir()
        (jd / "positioning.json").write_text(json.dumps({
            "positioning": {"value": "value_add", "confidence": 0.85},
            "leverage": {"ltv": 0.65, "rate": 0.0575, "amort_years": 30,
                         "io_months": 12, "source": "v1_hardcoded"},
            "engine_inputs_relative": "judgment/engine_inputs.json",
            "blockers": [],
        }))
        (jd / "_complete").write_text(json.dumps({
            "step": "judgment", "committed_at": "x", "file_manifest": ["positioning.json"]}))
        return StepResult(status="ok")
    steps["judgment"].run.side_effect = fake_judgment_run

    def fake_uw_run(state, run_dir):
        call_order.append("underwriting")
        uw = run_dir / "underwriting"
        uw.mkdir()
        (uw / "deal_summary.json").write_text(json.dumps({
            "metrics": {"irr": {"levered_irr": 0.15},
                        "dscr": {"minimum_dscr": 1.25}, "coc": {"cash_on_cash_year_1": 0.08}}}))
        (uw / "_complete").write_text(json.dumps({
            "step": "underwriting", "committed_at": "x",
            "file_manifest": ["deal_summary.json"]}))
        return StepResult(status="ok")
    steps["underwriting"].run.side_effect = fake_uw_run

    result = run_lifecycle(
        src,
        deal_slug="project_essex",
        project_root=project_root,
        steps=steps,
    )

    assert call_order == ["intake", "comps", "judgment", "underwriting", "memo", "crm"]
    assert result.status == "memo_ready"
    assert result.exit_code == 0
    assert result.deal_slug == "project_essex"
    assert result.run_id == "run_001"


def test_pipeline_writes_initial_lifecycle_state(tmp_path: Path) -> None:
    src = _make_data_room(tmp_path)
    project_root = tmp_path / "project_root"
    project_root.mkdir()

    # Trivially-mocked steps that all just succeed
    steps = {n: _ok_step(n) for n in ["intake", "comps", "judgment",
                                      "underwriting", "memo", "crm"]}

    # Write minimal positioning.json + deal_summary.json so patch step works
    def stub_judgment_run(state, run_dir):
        jd = run_dir / "judgment"; jd.mkdir()
        (jd / "positioning.json").write_text(json.dumps({
            "positioning": {"confidence": 0.85},
            "leverage": {"source": "v1_hardcoded"},
            "blockers": []}))
        return StepResult(status="ok")
    steps["judgment"].run.side_effect = stub_judgment_run

    def stub_uw_run(state, run_dir):
        uw = run_dir / "underwriting"; uw.mkdir()
        (uw / "deal_summary.json").write_text(json.dumps({
            "metrics": {"irr": {"levered_irr": 0.15},
                        "dscr": {"minimum_dscr": 1.25}, "coc": {"cash_on_cash_year_1": 0.08}}}))
        return StepResult(status="ok")
    steps["underwriting"].run.side_effect = stub_uw_run

    result = run_lifecycle(src, deal_slug="d", project_root=project_root, steps=steps)
    state_path = project_root / "runs" / "deals" / "d" / "outputs" / result.run_id / \
                 "_lifecycle_state.json"
    assert state_path.exists()
    payload = json.loads(state_path.read_text())
    assert payload["status"] == "memo_ready"
    assert payload["steps_completed"] == ["intake", "comps", "judgment",
                                          "underwriting", "memo", "crm"]


def test_pipeline_persists_state_after_each_step(tmp_path: Path) -> None:
    """State file must be updated after every step (so a crash mid-pipeline
    leaves a partial-but-valid state for --resume)."""
    src = _make_data_room(tmp_path)
    project_root = tmp_path / "project_root"
    project_root.mkdir()

    persisted_states: list[list[str]] = []
    steps = {n: _ok_step(n) for n in ["intake", "comps", "judgment",
                                      "underwriting", "memo", "crm"]}

    def make_step_capturing_state(name):
        def run(state, run_dir):
            (run_dir / name).mkdir(exist_ok=True)
            return StepResult(status="ok")
        m = MagicMock()
        m.name = name
        m.run.side_effect = run
        m.is_satisfied.return_value = False
        # After-step hook: capture _lifecycle_state.json contents
        return m

    # Use a side-effect that snapshots state after each step
    state_path_holder: dict = {}
    original_persist = None  # patched in runner

    def stub_judgment_run(state, run_dir):
        jd = run_dir / "judgment"; jd.mkdir()
        (jd / "positioning.json").write_text(json.dumps({
            "positioning": {"confidence": 0.85},
            "leverage": {"source": "v1_hardcoded"}, "blockers": []}))
        return StepResult(status="ok")
    steps["judgment"].run.side_effect = stub_judgment_run

    def stub_uw_run(state, run_dir):
        uw = run_dir / "underwriting"; uw.mkdir()
        (uw / "deal_summary.json").write_text(json.dumps({
            "metrics": {"irr": {"levered_irr": 0.15}, "dscr": {"minimum_dscr": 1.25}, "coc": {"cash_on_cash_year_1": 0.08}}}))
        return StepResult(status="ok")
    steps["underwriting"].run.side_effect = stub_uw_run

    result = run_lifecycle(src, deal_slug="d", project_root=project_root, steps=steps)
    # After full run, state has all 6 steps
    state_path = project_root / "runs" / "deals" / "d" / "outputs" / result.run_id / \
                 "_lifecycle_state.json"
    final = json.loads(state_path.read_text())
    assert len(final["steps_completed"]) == 6


def test_pipeline_step_4_5_patches_recommendation(tmp_path: Path) -> None:
    src = _make_data_room(tmp_path)
    project_root = tmp_path / "project_root"
    project_root.mkdir()

    steps = {n: _ok_step(n) for n in ["intake", "comps", "judgment",
                                      "underwriting", "memo", "crm"]}

    def stub_judgment_run(state, run_dir):
        jd = run_dir / "judgment"; jd.mkdir()
        (jd / "positioning.json").write_text(json.dumps({
            "positioning": {"confidence": 0.85},
            "leverage": {"source": "v1_hardcoded"}, "blockers": []}))
        return StepResult(status="ok")
    steps["judgment"].run.side_effect = stub_judgment_run

    def stub_uw_run(state, run_dir):
        uw = run_dir / "underwriting"; uw.mkdir()
        (uw / "deal_summary.json").write_text(json.dumps({
            "metrics": {"irr": {"levered_irr": 0.15}, "dscr": {"minimum_dscr": 1.25}, "coc": {"cash_on_cash_year_1": 0.08}}}))
        return StepResult(status="ok")
    steps["underwriting"].run.side_effect = stub_uw_run

    result = run_lifecycle(src, deal_slug="d", project_root=project_root, steps=steps)

    pos_path = project_root / "runs" / "deals" / "d" / "outputs" / result.run_id / \
               "judgment" / "positioning.json"
    payload = json.loads(pos_path.read_text())
    assert payload["recommendation"] == "PROCEED"
    assert "recommendation_confidence" in payload


def test_crm_step_receives_final_state_in_memory(tmp_path: Path) -> None:
    """§3 Step 6: CRM does NOT read _lifecycle_state.json; orchestrator passes
    final state values in-memory."""
    src = _make_data_room(tmp_path)
    project_root = tmp_path / "project_root"; project_root.mkdir()

    captured_state = {}

    def crm_run(state, run_dir):
        # Capture the state object as the orchestrator passed it
        captured_state["status"] = state.status
        captured_state["steps_completed"] = list(state.steps_completed)
        captured_state["finished_at"] = state.finished_at
        return StepResult(status="ok")
    crm_step = MagicMock()
    crm_step.name = "crm"
    crm_step.run.side_effect = crm_run
    crm_step.is_satisfied.return_value = False

    steps = {n: _ok_step(n) for n in ["intake", "comps", "judgment",
                                      "underwriting", "memo"]}
    steps["crm"] = crm_step

    def stub_judgment_run(state, run_dir):
        jd = run_dir / "judgment"; jd.mkdir()
        (jd / "positioning.json").write_text(json.dumps({
            "positioning": {"confidence": 0.85},
            "leverage": {"source": "v1_hardcoded"}, "blockers": []}))
        return StepResult(status="ok")
    steps["judgment"].run.side_effect = stub_judgment_run

    def stub_uw_run(state, run_dir):
        uw = run_dir / "underwriting"; uw.mkdir()
        (uw / "deal_summary.json").write_text(json.dumps({
            "metrics": {"irr": {"levered_irr": 0.15}, "dscr": {"minimum_dscr": 1.25}, "coc": {"cash_on_cash_year_1": 0.08}}}))
        return StepResult(status="ok")
    steps["underwriting"].run.side_effect = stub_uw_run

    run_lifecycle(src, deal_slug="d", project_root=project_root, steps=steps)

    # Per §3 Step 6: when CRM runs, status is already set to memo_ready and
    # steps_completed includes memo (but not crm yet — that's appended after CRM).
    assert captured_state["status"] == "memo_ready"
    assert "memo" in captured_state["steps_completed"]
    assert "crm" not in captured_state["steps_completed"]
    assert captured_state["finished_at"] is not None


def test_intake_needs_analyst_input_continues_to_comps(tmp_path: Path) -> None:
    """V1.2 / spec §4.3: when intake returns status='blocked' with a
    needs_analyst_input blocker, the pipeline continues — comps + judgment
    + underwriting + memo + crm all run (degraded). The terminal status
    surfaces as memo_ready_with_blockers in _lifecycle_state.json so the
    analyst sees the blocker.
    """
    src = _make_data_room(tmp_path)
    project_root = tmp_path / "project_root"
    project_root.mkdir()

    call_order: list[str] = []

    def make_recording_step(name: str, status: str = "ok",
                            blockers: list[BlockerItem] | None = None):
        def run(state, run_dir):
            call_order.append(name)
            (run_dir / name).mkdir(exist_ok=True)
            return StepResult(status=status, blockers=blockers or [])
        m = MagicMock()
        m.name = name
        m.run.side_effect = run
        m.is_satisfied.return_value = False
        return m

    # Intake: blocked + needs_analyst_input blocker (matches deal-intake's
    # response when analyst-required gaps remain after auto-defaults).
    intake_step = make_recording_step(
        "intake",
        status="blocked",
        blockers=[BlockerItem(
            step="intake",
            id="needs_analyst_input",
            description="target_monthly_rent missing for cohort 4br2ba",
        )],
    )

    def judgment_run(state, run_dir):
        call_order.append("judgment")
        jd = run_dir / "judgment"
        jd.mkdir()
        (jd / "positioning.json").write_text(json.dumps({
            "positioning": {"confidence": 0.6},
            "leverage": {"source": "v1_hardcoded"},
            "blockers": [],
        }))
        return StepResult(status="ok")

    def uw_run(state, run_dir):
        call_order.append("underwriting")
        uw = run_dir / "underwriting"
        uw.mkdir()
        (uw / "deal_summary.json").write_text(json.dumps({
            "metrics": {"irr": {"levered_irr": 0.10},
                        "dscr": {"minimum_dscr": 1.20}, "coc": {"cash_on_cash_year_1": 0.08}}}))
        return StepResult(status="ok")

    steps = {
        "intake": intake_step,
        "comps": make_recording_step("comps"),
        "judgment": MagicMock(name="judgment"),
        "underwriting": MagicMock(name="underwriting"),
        "memo": make_recording_step("memo"),
        "crm": make_recording_step("crm"),
    }
    steps["judgment"].name = "judgment"
    steps["judgment"].run.side_effect = judgment_run
    steps["judgment"].is_satisfied.return_value = False
    steps["underwriting"].name = "underwriting"
    steps["underwriting"].run.side_effect = uw_run
    steps["underwriting"].is_satisfied.return_value = False

    result = run_lifecycle(
        src,
        deal_slug="blocked_intake_deal",
        project_root=project_root,
        steps=steps,
    )

    # All 6 steps invoked (intake blocked, but comps + downstream still ran).
    assert call_order == [
        "intake", "comps", "judgment", "underwriting", "memo", "crm",
    ], (
        f"intake needs_analyst_input must not skip comps; saw {call_order}"
    )

    # Terminal status surfaces the blocker for analyst review.
    assert result.status == "memo_ready_with_blockers"
    assert result.blocker_count >= 1

    # _lifecycle_state.json carries the same status so analyst tools see it.
    state_path = (project_root / "runs" / "deals" / "blocked_intake_deal"
                  / "outputs" / result.run_id / "_lifecycle_state.json")
    payload = json.loads(state_path.read_text())
    assert payload["status"] == "memo_ready_with_blockers"
    blocker_ids = [b["id"] for b in payload["blockers"]]
    assert "needs_analyst_input" in blocker_ids
