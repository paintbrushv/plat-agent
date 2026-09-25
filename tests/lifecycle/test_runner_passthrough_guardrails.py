import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from plat_agent.lifecycle.crm import CRMStep, crm_path, list_deals
from plat_agent.lifecycle.memo import MemoStep
from plat_agent.lifecycle.protocol import StepResult
from plat_agent.lifecycle.runner import run_lifecycle


def _data_room(tmp_path: Path) -> Path:
    src = tmp_path / "data_room"; src.mkdir()
    (src / "OM.pdf").write_bytes(b"x")
    return src


def _make_step(name: str):
    def run(state, run_dir):
        (run_dir / name).mkdir(exist_ok=True)
        return StepResult(status="ok")
    m = MagicMock()
    m.name = name
    m.run.side_effect = run
    m.is_satisfied.return_value = False
    return m


def _judgment():
    def run(state, run_dir):
        jd = run_dir / "judgment"; jd.mkdir()
        (jd / "positioning.json").write_text(json.dumps({
            "positioning": {"confidence": 0.85},
            "leverage": {"source": "v1_hardcoded"}, "blockers": []}))
        return StepResult(status="ok")
    m = MagicMock(); m.name = "judgment"; m.run.side_effect = run
    m.is_satisfied.return_value = False; return m


def _uw():
    def run(state, run_dir):
        uw = run_dir / "underwriting"; uw.mkdir()
        (uw / "deal_summary.json").write_text(json.dumps({
            "metrics": {"irr": {"levered_irr": 0.20}, "dscr": {"minimum_dscr": 1.40}, "coc": {"cash_on_cash_year_1": 0.08}}}))
        return StepResult(status="ok")
    m = MagicMock(); m.name = "underwriting"; m.run.side_effect = run
    m.is_satisfied.return_value = False; return m


def test_passthrough_forces_recommendation_needs_data(tmp_path: Path) -> None:
    """Even with strong metrics (IRR 0.20, DSCR 1.40), passthrough forces NEEDS_DATA."""
    project = tmp_path / "p"; project.mkdir()
    steps = {
        "intake": _make_step("intake"),
        "comps": _make_step("comps"),
        "judgment": _judgment(),
        "underwriting": _uw(),
        "memo": _make_step("memo"),
        "crm": _make_step("crm"),
    }
    result = run_lifecycle(_data_room(tmp_path), deal_slug="d",
                           project_root=project, steps=steps,
                           judgment_mode="passthrough")
    pos = json.loads((project / "runs" / "deals" / "d" / "outputs" / result.run_id /
                      "judgment" / "positioning.json").read_text())
    assert pos["recommendation"] == "NEEDS_DATA"


def test_default_deterministic_mode_allows_proceed(tmp_path: Path) -> None:
    """Sanity: default mode with strong metrics → PROCEED."""
    project = tmp_path / "p"; project.mkdir()
    steps = {
        "intake": _make_step("intake"),
        "comps": _make_step("comps"),
        "judgment": _judgment(),
        "underwriting": _uw(),
        "memo": _make_step("memo"),
        "crm": _make_step("crm"),
    }
    result = run_lifecycle(_data_room(tmp_path), deal_slug="d",
                           project_root=project, steps=steps)
    pos = json.loads((project / "runs" / "deals" / "d" / "outputs" / result.run_id /
                      "judgment" / "positioning.json").read_text())
    assert pos["recommendation"] == "PROCEED"


# --- Tests for state.judgment_mode persistence (spec §5.4 follow-up) ---


def _intake_with_canonical():
    """IntakeStep mock that writes a real canonical_deal.json."""
    def run(state, run_dir):
        d = run_dir / "intake"; d.mkdir(parents=True, exist_ok=True)
        (d / "canonical_deal.json").write_text(json.dumps({
            "metadata": {
                "address": "123 Test St, Dallas TX",
                "year_built": 2005,
                "as_of_date": "2026-05-05",
                "analyst": "Analyst",
                "broker": "ACME",
            },
            "unit_cohorts": [
                {"cohort_id": "1BR", "unit_count": 50},
                {"cohort_id": "2BR", "unit_count": 50},
            ],
            "purchase_assumptions": {"purchase_price": 12_000_000},
        }))
        return StepResult(status="ok")
    m = MagicMock(); m.name = "intake"; m.run.side_effect = run
    m.is_satisfied.return_value = False
    return m


def _judgment_with_provenance(judgment_engine_value: str):
    """JudgmentStep mock that writes positioning.json + judgment provenance.

    The provenance.judgment_engine value is what the *engine itself* would
    record (e.g. "deterministic_v1"). The test verifies that CRM picks up
    state.judgment_mode (set by orchestrator) regardless.
    """
    def run(state, run_dir):
        jd = run_dir / "judgment"; jd.mkdir(parents=True, exist_ok=True)
        (jd / "positioning.json").write_text(json.dumps({
            "positioning": {"value": "value_add", "confidence": 0.85, "rationale": "x"},
            "leverage": {"ltv": 0.65, "rate": 0.0575, "amort_years": 30,
                         "io_months": 12, "source": "v1_hardcoded"},
            "engine_inputs_relative": "judgment/engine_inputs.json",
            "blockers": [],
        }))
        (jd / "thesis.md").write_text("# Thesis\n\nTest thesis.\n")
        (jd / "_provenance.json").write_text(json.dumps({
            "judgment_engine": judgment_engine_value,
            "thesis_path": str((jd / "thesis.md").resolve()),
        }))
        return StepResult(status="ok")
    m = MagicMock(); m.name = "judgment"; m.run.side_effect = run
    m.is_satisfied.return_value = False
    return m


def _uw_with_provenance():
    """UnderwritingStep mock that writes deal_summary + provenance."""
    def run(state, run_dir):
        uw = run_dir / "underwriting"; uw.mkdir(parents=True, exist_ok=True)
        (uw / "deal_summary.json").write_text(json.dumps({
            "metrics": {
                "irr": {"levered_irr": 0.20},
                "dscr": {"minimum_dscr": 1.40},
                "equity_multiple": {"levered_em": 2.1},
                "yields": {"going_in_cap_rate": 0.055, "exit_cap_rate": 0.06},
            },
            "sanity_flags": [],
        }))
        (uw / "_provenance.json").write_text(json.dumps({
            "feasibility_sanity_flags": [],
            "workbook_path": str((uw / "deal_workbook.xlsm").resolve()),
        }))
        return StepResult(status="ok")
    m = MagicMock(); m.name = "underwriting"; m.run.side_effect = run
    m.is_satisfied.return_value = False
    return m


def test_passthrough_propagates_to_state_memo_and_crm(tmp_path: Path) -> None:
    """End-to-end: run_lifecycle(..., judgment_mode='passthrough') results in
      1) judgment_mode='passthrough' in _lifecycle_state.json
      2) the §5.4 passthrough banner ("TEST RUN") in memo.md
      3) CRM row.judgment_mode_override == 'passthrough'
    """
    project = tmp_path / "p"; project.mkdir()
    runs_root = project / "runs" / "deals"
    steps = {
        "intake": _intake_with_canonical(),
        "comps": _make_step("comps"),
        "judgment": _judgment_with_provenance("deterministic_v1"),
        "underwriting": _uw_with_provenance(),
        # Real MemoStep + CRMStep so we exercise the read paths under test
        "memo": MemoStep(),
        "crm": CRMStep(runs_deals_root=runs_root),
    }
    result = run_lifecycle(
        _data_room(tmp_path),
        deal_slug="d",
        project_root=project,
        steps=steps,
        judgment_mode="passthrough",
    )

    run_dir = runs_root / "d" / "outputs" / result.run_id

    # 1) state file has judgment_mode persisted
    state_payload = json.loads((run_dir / "_lifecycle_state.json").read_text())
    assert state_payload["judgment_mode"] == "passthrough"

    # 2) memo banner present
    memo_md = (run_dir / "memo" / "memo.md").read_text()
    assert "TEST RUN" in memo_md
    assert "passthrough" in memo_md

    # 3) CRM row has the override
    rows = list_deals(crm_path(runs_root))
    assert len(rows) == 1
    assert rows[0].judgment_mode_override == "passthrough"


def test_default_mode_no_banner_no_override(tmp_path: Path) -> None:
    """Sanity counterpart: default judgment_mode → no banner, no override."""
    project = tmp_path / "p"; project.mkdir()
    runs_root = project / "runs" / "deals"
    steps = {
        "intake": _intake_with_canonical(),
        "comps": _make_step("comps"),
        "judgment": _judgment_with_provenance("deterministic_v1"),
        "underwriting": _uw_with_provenance(),
        "memo": MemoStep(),
        "crm": CRMStep(runs_deals_root=runs_root),
    }
    result = run_lifecycle(
        _data_room(tmp_path),
        deal_slug="d",
        project_root=project,
        steps=steps,
    )

    run_dir = runs_root / "d" / "outputs" / result.run_id

    state_payload = json.loads((run_dir / "_lifecycle_state.json").read_text())
    assert state_payload["judgment_mode"] == "deterministic_v1"

    memo_md = (run_dir / "memo" / "memo.md").read_text()
    assert "TEST RUN" not in memo_md

    rows = list_deals(crm_path(runs_root))
    assert len(rows) == 1
    assert rows[0].judgment_mode_override is None
