import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from plat_agent.lifecycle.protocol import StepResult
from plat_agent.lifecycle.runner import run_lifecycle
from plat_agent.lifecycle.state import BlockerItem


def _data_room(tmp_path: Path) -> Path:
    src = tmp_path / "data_room"
    src.mkdir()
    (src / "OM.pdf").write_bytes(b"x")
    return src


def _ok_with_writes(name: str):
    def run(state, run_dir):
        (run_dir / name).mkdir(exist_ok=True)
        return StepResult(status="ok")
    m = MagicMock()
    m.name = name
    m.run.side_effect = run
    m.is_satisfied.return_value = False
    return m


def _err_step(name: str, msg: str = "kaboom"):
    m = MagicMock()
    m.name = name
    m.run.return_value = StepResult(status="error", error_message=msg)
    m.is_satisfied.return_value = False
    return m


def _judgment_writer():
    def run(state, run_dir):
        jd = run_dir / "judgment"; jd.mkdir(exist_ok=True)
        (jd / "positioning.json").write_text(json.dumps({
            "positioning": {"confidence": 0.85},
            "leverage": {"source": "v1_hardcoded"}, "blockers": []}))
        return StepResult(status="ok")
    m = MagicMock()
    m.name = "judgment"
    m.run.side_effect = run
    m.is_satisfied.return_value = False
    return m


def _uw_writer(irr=0.15, dscr=1.25):
    def run(state, run_dir):
        uw = run_dir / "underwriting"; uw.mkdir(exist_ok=True)
        (uw / "deal_summary.json").write_text(json.dumps({
            "metrics": {"irr": {"levered_irr": irr}, "dscr": {"minimum_dscr": dscr}}}))
        return StepResult(status="ok")
    m = MagicMock()
    m.name = "underwriting"
    m.run.side_effect = run
    m.is_satisfied.return_value = False
    return m


def test_intake_hard_error_halts_lifecycle(tmp_path: Path) -> None:
    project = tmp_path / "p"; project.mkdir()
    steps = {
        "intake": _err_step("intake"),
        "comps": _ok_with_writes("comps"),
        "judgment": _judgment_writer(),
        "underwriting": _uw_writer(),
        "memo": _ok_with_writes("memo"),
        "crm": _ok_with_writes("crm"),
    }
    result = run_lifecycle(_data_room(tmp_path), deal_slug="d",
                           project_root=project, steps=steps)
    assert result.status == "failed_at_intake"
    assert result.exit_code == 1
    # Comps and downstream never called
    steps["comps"].run.assert_not_called()


def test_comps_hard_error_continues_with_comps_unavailable(tmp_path: Path) -> None:
    project = tmp_path / "p"; project.mkdir()
    steps = {
        "intake": _ok_with_writes("intake"),
        "comps": _err_step("comps"),
        "judgment": _judgment_writer(),
        "underwriting": _uw_writer(),
        "memo": _ok_with_writes("memo"),
        "crm": _ok_with_writes("crm"),
    }
    result = run_lifecycle(_data_room(tmp_path), deal_slug="d",
                           project_root=project, steps=steps)
    # Judgment + underwriting + memo + crm all still ran
    steps["judgment"].run.assert_called()
    steps["memo"].run.assert_called()
    # Recommendation forced NEEDS_DATA per §4.3 comps-hard-error path
    pos = json.loads((project / "runs" / "deals" / "d" / "outputs" / result.run_id /
                      "judgment" / "positioning.json").read_text())
    assert pos["recommendation"] == "NEEDS_DATA"
    assert result.status == "memo_ready_with_blockers"


def test_judgment_hard_error_skips_underwriting_but_runs_memo(tmp_path: Path) -> None:
    project = tmp_path / "p"; project.mkdir()
    # Judgment must still write a stub positioning.json so Step 4.5 can patch
    def judgment_err(state, run_dir):
        jd = run_dir / "judgment"; jd.mkdir(exist_ok=True)
        (jd / "positioning.json").write_text(json.dumps({
            "positioning": {"confidence": 0.0},
            "leverage": {"source": "v1_hardcoded"}, "blockers": []}))
        return StepResult(status="error", error_message="judgment boom")

    judgment_step = MagicMock()
    judgment_step.name = "judgment"
    judgment_step.run.side_effect = judgment_err
    judgment_step.is_satisfied.return_value = False

    steps = {
        "intake": _ok_with_writes("intake"),
        "comps": _ok_with_writes("comps"),
        "judgment": judgment_step,
        "underwriting": _uw_writer(),
        "memo": _ok_with_writes("memo"),
        "crm": _ok_with_writes("crm"),
    }
    result = run_lifecycle(_data_room(tmp_path), deal_slug="d",
                           project_root=project, steps=steps)
    # Underwriting NOT called
    steps["underwriting"].run.assert_not_called()
    # Memo + crm did run
    steps["memo"].run.assert_called()
    assert result.status == "failed_at_judgment"
    assert result.exit_code == 1


def test_underwriting_hard_error_runs_draft_memo(tmp_path: Path) -> None:
    project = tmp_path / "p"; project.mkdir()
    steps = {
        "intake": _ok_with_writes("intake"),
        "comps": _ok_with_writes("comps"),
        "judgment": _judgment_writer(),
        "underwriting": _err_step("underwriting"),
        "memo": _ok_with_writes("memo"),
        "crm": _ok_with_writes("crm"),
    }
    result = run_lifecycle(_data_room(tmp_path), deal_slug="d",
                           project_root=project, steps=steps)
    steps["memo"].run.assert_called()
    assert result.status == "failed_at_underwriting"


def test_memo_hard_error_logs_failure(tmp_path: Path) -> None:
    project = tmp_path / "p"; project.mkdir()
    steps = {
        "intake": _ok_with_writes("intake"),
        "comps": _ok_with_writes("comps"),
        "judgment": _judgment_writer(),
        "underwriting": _uw_writer(),
        "memo": _err_step("memo"),
        "crm": _ok_with_writes("crm"),
    }
    result = run_lifecycle(_data_room(tmp_path), deal_slug="d",
                           project_root=project, steps=steps)
    assert result.status == "failed_at_memo"
    assert result.exit_code == 1


def test_crm_hard_error_exit_code_zero(tmp_path: Path) -> None:
    """§4.3: CRM failure is warning-only; memo on disk; exit 0."""
    project = tmp_path / "p"; project.mkdir()
    steps = {
        "intake": _ok_with_writes("intake"),
        "comps": _ok_with_writes("comps"),
        "judgment": _judgment_writer(),
        "underwriting": _uw_writer(),
        "memo": _ok_with_writes("memo"),
        "crm": _err_step("crm"),
    }
    result = run_lifecycle(_data_room(tmp_path), deal_slug="d",
                           project_root=project, steps=steps)
    assert result.exit_code == 0
