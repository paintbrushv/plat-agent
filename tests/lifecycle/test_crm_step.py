# tests/lifecycle/test_crm_step.py
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

import pytest

from plat_agent.lifecycle.crm import (
    CRMStep,
    crm_path,
    list_deals,
)
from plat_agent.lifecycle.protocol import LifecycleStep
from plat_agent.lifecycle.state import LifecycleState

FIXTURES = Path(__file__).parent.parent / "fixtures" / "lifecycle" / "crm"


def _populate_run_dir(run_dir: Path) -> None:
    """Stage upstream subsystem artifacts in run_dir as the orchestrator would."""
    for sub in ("intake", "judgment", "underwriting", "memo"):
        (run_dir / sub).mkdir(parents=True, exist_ok=True)
    shutil.copy(FIXTURES / "canonical_deal.json", run_dir / "intake" / "canonical_deal.json")
    shutil.copy(FIXTURES / "positioning.json", run_dir / "judgment" / "positioning.json")
    shutil.copy(FIXTURES / "deal_summary.json", run_dir / "underwriting" / "deal_summary.json")
    shutil.copy(FIXTURES / "memo_provenance.json", run_dir / "memo" / "_provenance.json")
    shutil.copy(FIXTURES / "underwriting_provenance.json",
                run_dir / "underwriting" / "_provenance.json")
    shutil.copy(FIXTURES / "judgment_provenance.json", run_dir / "judgment" / "_provenance.json")


def _state(slug: str = "project_essex", run: str = "run_002") -> LifecycleState:
    return LifecycleState(
        deal_slug=slug, run_id=run,
        status="memo_ready",
        steps_completed=["intake", "comps", "judgment", "underwriting", "memo"],
        finished_at=datetime(2026, 5, 5, 16, 30, tzinfo=timezone.utc),
    )


def test_crm_step_implements_lifecycle_step_protocol() -> None:
    step = CRMStep(runs_deals_root=Path("/tmp/r"))
    assert isinstance(step, LifecycleStep)
    assert step.name == "crm"


def test_crm_step_run_appends_row_and_writes_provenance(tmp_path: Path) -> None:
    runs_root = tmp_path / "runs" / "deals"
    run_dir = runs_root / "project_essex" / "outputs" / "run_002"
    _populate_run_dir(run_dir)

    step = CRMStep(runs_deals_root=runs_root)
    result = step.run(_state(), run_dir)

    assert result.status == "ok"
    # JSONL has the row
    rows = list_deals(crm_path(runs_root))
    assert len(rows) == 1
    assert rows[0].deal_slug == "project_essex"
    # _provenance.json + _complete written under crm/
    crm_dir = run_dir / "crm"
    assert (crm_dir / "_provenance.json").exists()
    assert (crm_dir / "_complete").exists()


def test_crm_step_provenance_records_status_ok(tmp_path: Path) -> None:
    runs_root = tmp_path / "runs" / "deals"
    run_dir = runs_root / "d" / "outputs" / "r"
    _populate_run_dir(run_dir)
    step = CRMStep(runs_deals_root=runs_root)
    step.run(_state(slug="d", run="r"), run_dir)

    prov = json.loads((run_dir / "crm" / "_provenance.json").read_text())
    assert prov["status"] == "ok"
    assert "input_hash" in prov
    assert "contract_version" in prov


def test_crm_step_complete_marker_lists_files(tmp_path: Path) -> None:
    runs_root = tmp_path / "runs" / "deals"
    run_dir = runs_root / "d" / "outputs" / "r"
    _populate_run_dir(run_dir)
    step = CRMStep(runs_deals_root=runs_root)
    step.run(_state(slug="d", run="r"), run_dir)

    marker = json.loads((run_dir / "crm" / "_complete").read_text())
    assert marker["step"] == "crm"
    assert "_provenance.json" in marker["file_manifest"]
    # crm_row.json is the step-local witness of the row appended to the
    # shared _crm.jsonl. Listing it in the manifest lets is_satisfied()
    # detect a partial-write or directory tampering. (_crm.jsonl itself
    # is shared across deals and lives outside step_dir, so it cannot be
    # listed here.)
    assert "crm_row.json" in marker["file_manifest"]
    assert (run_dir / "crm" / "crm_row.json").exists()


def test_crm_step_is_satisfied_after_run(tmp_path: Path) -> None:
    runs_root = tmp_path / "runs" / "deals"
    run_dir = runs_root / "d" / "outputs" / "r"
    _populate_run_dir(run_dir)
    step = CRMStep(runs_deals_root=runs_root)
    step.run(_state(slug="d", run="r"), run_dir)

    # State must include "crm" in steps_completed for is_satisfied to pass
    state = _state(slug="d", run="r")
    state.steps_completed = state.steps_completed + ["crm"]
    assert step.is_satisfied(state, run_dir) is True


def test_crm_step_is_satisfied_false_before_run(tmp_path: Path) -> None:
    runs_root = tmp_path / "runs" / "deals"
    run_dir = runs_root / "d" / "outputs" / "r"
    run_dir.mkdir(parents=True)
    step = CRMStep(runs_deals_root=runs_root)
    assert step.is_satisfied(_state(slug="d", run="r"), run_dir) is False


def test_crm_step_writes_null_row_when_canonical_missing(tmp_path: Path) -> None:
    """V1.1 Sample Annex Tower shape: intake blocked → no canonical_deal.json on disk.
    CRMStep must still write a row (with null/empty data fields) instead of
    crashing with FileNotFoundError. Per spec §2.6 graceful degradation."""
    runs_root = tmp_path / "runs" / "deals"
    run_dir = runs_root / "sample_annex_tower_building" / "outputs" / "run_001"
    # Create only run_dir; do NOT populate intake/canonical_deal.json
    run_dir.mkdir(parents=True)

    step = CRMStep(runs_deals_root=runs_root)
    state = LifecycleState(
        deal_slug="sample_annex_tower_building",
        run_id="run_001",
        status="failed_at_intake",
        steps_completed=[],
        finished_at=datetime(2026, 5, 5, 16, 30, tzinfo=timezone.utc),
    )
    result = step.run(state, run_dir)

    assert result.status == "ok"
    rows = list_deals(crm_path(runs_root))
    assert len(rows) == 1
    row = rows[0]
    assert row.deal_slug == "sample_annex_tower_building"
    assert row.status == "failed_at_intake"
    # Recommendation defaults to NEEDS_DATA when positioning is absent
    assert row.recommendation == "NEEDS_DATA"
    # Required-but-defaulted fields when canonical is absent
    assert row.address == ""
    assert row.units == 0
    assert row.asking_price == 0
    # Optional fields are null
    assert row.levered_irr is None
    assert row.equity_multiple is None
    assert row.going_in_cap is None
    assert row.ppu is None
    assert row.vintage is None


def test_crm_step_writes_null_row_when_underwriting_missing(tmp_path: Path) -> None:
    """Underwriting was skipped (e.g., judgment blocker) → deal_summary.json
    and underwriting/_provenance.json absent. CRMStep still writes a row."""
    runs_root = tmp_path / "runs" / "deals"
    run_dir = runs_root / "d" / "outputs" / "r"
    run_dir.mkdir(parents=True)
    # Populate only intake + judgment (memo provenance) — skip underwriting
    (run_dir / "intake").mkdir()
    (run_dir / "judgment").mkdir()
    (run_dir / "memo").mkdir()
    shutil.copy(FIXTURES / "canonical_deal.json", run_dir / "intake" / "canonical_deal.json")
    shutil.copy(FIXTURES / "positioning.json", run_dir / "judgment" / "positioning.json")
    shutil.copy(FIXTURES / "memo_provenance.json", run_dir / "memo" / "_provenance.json")
    shutil.copy(FIXTURES / "judgment_provenance.json", run_dir / "judgment" / "_provenance.json")
    # No underwriting/ dir at all

    step = CRMStep(runs_deals_root=runs_root)
    state = LifecycleState(
        deal_slug="d", run_id="r",
        status="memo_ready_with_blockers",
        steps_completed=["intake", "comps", "judgment", "memo"],
        finished_at=datetime(2026, 5, 5, 16, 30, tzinfo=timezone.utc),
    )
    result = step.run(state, run_dir)

    assert result.status == "ok"
    rows = list_deals(crm_path(runs_root))
    assert len(rows) == 1
    # Underwriting metrics absent → all null
    assert rows[0].levered_irr is None
    assert rows[0].equity_multiple is None
    assert rows[0].going_in_cap is None
    # Canonical present → required fields populated
    assert rows[0].address != ""
    assert rows[0].units > 0


def test_crm_step_returns_ok_even_with_null_inputs(tmp_path: Path) -> None:
    """Pathological case: every upstream artifact absent. CRMStep still
    returns StepResult.ok and writes one row. The row write succeeded;
    the deal-data absence is a status, not a CRM failure."""
    runs_root = tmp_path / "runs" / "deals"
    run_dir = runs_root / "ghost_deal" / "outputs" / "run_001"
    run_dir.mkdir(parents=True)

    step = CRMStep(runs_deals_root=runs_root)
    state = LifecycleState(
        deal_slug="ghost_deal", run_id="run_001",
        status="failed_at_intake",
        steps_completed=[],
        finished_at=datetime(2026, 5, 5, 16, 30, tzinfo=timezone.utc),
    )
    result = step.run(state, run_dir)

    assert result.status == "ok"
    assert (run_dir / "crm" / "_provenance.json").exists()
    assert (run_dir / "crm" / "_complete").exists()
    rows = list_deals(crm_path(runs_root))
    assert len(rows) == 1
    assert rows[0].recommendation == "NEEDS_DATA"


def test_crm_step_run_passes_through_passthrough_judgment_mode(tmp_path: Path) -> None:
    """End-to-end: CRMStep + passthrough state.judgment_mode →
    judgment_mode_override on the row. State is the source of truth (set by
    the orchestrator from the run_lifecycle judgment_mode arg)."""
    runs_root = tmp_path / "runs" / "deals"
    run_dir = runs_root / "d" / "outputs" / "r"
    _populate_run_dir(run_dir)
    # Mutate the staged judgment provenance to passthrough (legacy fallback path)
    jp = json.loads((run_dir / "judgment" / "_provenance.json").read_text())
    jp["judgment_engine"] = "passthrough"
    (run_dir / "judgment" / "_provenance.json").write_text(json.dumps(jp))
    # Match positioning recommendation to passthrough rules
    pos = json.loads((run_dir / "judgment" / "positioning.json").read_text())
    pos["recommendation"] = "NEEDS_DATA"
    (run_dir / "judgment" / "positioning.json").write_text(json.dumps(pos))

    step = CRMStep(runs_deals_root=runs_root)
    state = _state(slug="d", run="r")
    state.status = "memo_ready_with_blockers"
    state.judgment_mode = "passthrough"
    step.run(state, run_dir)

    row = list_deals(crm_path(runs_root))[0]
    assert row.judgment_mode_override == "passthrough"
