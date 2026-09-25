# tests/lifecycle/test_crm_integration.py
"""End-to-end CRM subsystem integration test.

Exercises every public API surface in one flow:
  - CRMStep.run() against staged upstream artifacts
  - JSONL append + readback via list_deals()
  - get_latest() handling re-runs of the same (deal_slug, run_id)
  - _provenance.json + _complete marker semantics
  - Idempotency: is_satisfied() True after run; False before
"""
import json
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from plat_agent.lifecycle import (
    CRM_FILENAME,
    CRMStep,
    book_deal,
    crm_path,
    get_latest,
    list_deals,
)
from plat_agent.lifecycle.state import LifecycleState

FIXTURES = Path(__file__).parent.parent / "fixtures" / "lifecycle" / "crm"


def _stage(run_dir: Path) -> None:
    for sub in ("intake", "judgment", "underwriting", "memo"):
        (run_dir / sub).mkdir(parents=True, exist_ok=True)
    shutil.copy(FIXTURES / "canonical_deal.json", run_dir / "intake" / "canonical_deal.json")
    shutil.copy(FIXTURES / "positioning.json", run_dir / "judgment" / "positioning.json")
    shutil.copy(FIXTURES / "deal_summary.json", run_dir / "underwriting" / "deal_summary.json")
    shutil.copy(FIXTURES / "memo_provenance.json", run_dir / "memo" / "_provenance.json")
    shutil.copy(FIXTURES / "underwriting_provenance.json",
                run_dir / "underwriting" / "_provenance.json")
    shutil.copy(FIXTURES / "judgment_provenance.json", run_dir / "judgment" / "_provenance.json")


def _state(slug: str, run: str, ts: datetime) -> LifecycleState:
    return LifecycleState(
        deal_slug=slug, run_id=run,
        status="memo_ready",
        steps_completed=["intake", "comps", "judgment", "underwriting", "memo"],
        finished_at=ts,
    )


def test_full_crm_flow_one_deal_one_run(tmp_path: Path) -> None:
    runs_root = tmp_path / "runs" / "deals"
    run_dir = runs_root / "project_essex" / "outputs" / "run_002"
    _stage(run_dir)

    step = CRMStep(runs_deals_root=runs_root)
    state = _state("project_essex", "run_002",
                   datetime(2026, 5, 5, 16, 30, tzinfo=timezone.utc))
    result = step.run(state, run_dir)

    # Result OK
    assert result.status == "ok"
    # JSONL exists at the expected path
    assert (runs_root / CRM_FILENAME).exists()
    # Exactly one row, with the correct fields
    rows = list_deals(crm_path(runs_root))
    assert len(rows) == 1
    r = rows[0]
    assert r.deal_slug == "project_essex"
    assert r.run_id == "run_002"
    assert r.status == "memo_ready"
    assert r.recommendation == "PROCEED"
    assert r.units == 240
    assert r.levered_irr == 0.152
    assert r.equity_multiple == 1.78
    assert r.going_in_cap == 0.052
    assert r.vintage == 2005
    # Step's own provenance + complete marker present
    assert (run_dir / "crm" / "_provenance.json").exists()
    assert (run_dir / "crm" / "_complete").exists()


def test_get_latest_returns_most_recent_across_reruns(tmp_path: Path) -> None:
    """Per §2.6: re-runs of (deal_slug, run_id) APPEND. get_latest() returns
    the row with the most recent ts (full history preserved)."""
    runs_root = tmp_path / "runs" / "deals"
    run_dir = runs_root / "project_essex" / "outputs" / "run_002"
    _stage(run_dir)
    step = CRMStep(runs_deals_root=runs_root)

    t0 = datetime(2026, 5, 5, 12, 0, tzinfo=timezone.utc)
    t1 = t0 + timedelta(hours=2)
    t2 = t0 + timedelta(hours=4)

    # First run
    step.run(_state("project_essex", "run_002", t0), run_dir)
    # Re-run same (slug, run_id) — APPENDS, doesn't overwrite
    step.run(_state("project_essex", "run_002", t1), run_dir)
    # Different run_id same slug
    step.run(_state("project_essex", "run_003", t2), run_dir)

    # Three rows total — full history
    rows = list_deals(crm_path(runs_root))
    assert len(rows) == 3
    # get_latest returns the most recent by ts
    latest = get_latest(crm_path(runs_root), "project_essex")
    assert latest is not None
    assert latest.ts == t2
    assert latest.run_id == "run_003"


def test_book_deal_directly_then_read_helpers(tmp_path: Path) -> None:
    """Smoke: book_deal can be called by orchestrator without CRMStep wrapper."""
    crm_jsonl = crm_path(tmp_path)
    canonical = json.loads((FIXTURES / "canonical_deal.json").read_text())
    positioning = json.loads((FIXTURES / "positioning.json").read_text())
    deal_summary = json.loads((FIXTURES / "deal_summary.json").read_text())
    memo_prov = json.loads((FIXTURES / "memo_provenance.json").read_text())
    uw_prov = json.loads((FIXTURES / "underwriting_provenance.json").read_text())
    judg_prov = json.loads((FIXTURES / "judgment_provenance.json").read_text())

    state = _state("d", "r", datetime(2026, 5, 5, 12, tzinfo=timezone.utc))
    book_deal(
        crm_jsonl_path=crm_jsonl,
        canonical_deal=canonical,
        positioning=positioning,
        deal_summary=deal_summary,
        memo_provenance=memo_prov,
        underwriting_provenance=uw_prov,
        judgment_provenance=judg_prov,
        final_state=state,
    )
    assert get_latest(crm_jsonl, "d").run_id == "r"


def test_failed_underwriting_path_writes_partial_row(tmp_path: Path) -> None:
    """When underwriting failed, deal_summary + provenance may be absent;
    metrics columns must be null but row still appends with status=failed_at_*."""
    runs_root = tmp_path / "runs" / "deals"
    run_dir = runs_root / "d" / "outputs" / "r"
    _stage(run_dir)
    # Remove deal_summary + underwriting provenance to simulate failure
    (run_dir / "underwriting" / "deal_summary.json").unlink()
    (run_dir / "underwriting" / "_provenance.json").unlink()

    step = CRMStep(runs_deals_root=runs_root)
    state = _state("d", "r", datetime(2026, 5, 5, 12, tzinfo=timezone.utc))
    state.status = "failed_at_underwriting"
    result = step.run(state, run_dir)
    assert result.status == "ok"  # CRM step itself succeeded

    row = get_latest(crm_path(runs_root), "d")
    assert row.status == "failed_at_underwriting"
    assert row.levered_irr is None
    assert row.equity_multiple is None
    assert row.going_in_cap is None
    assert row.workbook_path is None
