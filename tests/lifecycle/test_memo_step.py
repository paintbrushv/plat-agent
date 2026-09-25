# tests/lifecycle/test_memo_step.py
"""MemoStep adapter — implements LifecycleStep, drives the full memo pipeline."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from unittest.mock import patch

import pytest

from plat_agent.lifecycle.memo import MemoStep
from plat_agent.lifecycle.protocol import LifecycleStep
from plat_agent.lifecycle.state import LifecycleState


FIX = Path(__file__).parent / "fixtures" / "memo" / "clean"


def _build_run(tmp_path: Path) -> Path:
    run = tmp_path / "run_002"
    (run / "intake").mkdir(parents=True)
    (run / "comps").mkdir()
    (run / "judgment").mkdir()
    (run / "underwriting").mkdir()
    for src, dst in [
        (FIX / "canonical_deal.json", run / "intake" / "canonical_deal.json"),
        (FIX / "comps.json", run / "comps" / "comps.json"),
        (FIX / "positioning.json", run / "judgment" / "positioning.json"),
        (FIX / "thesis.md", run / "judgment" / "thesis.md"),
        (FIX / "deal_summary.json", run / "underwriting" / "deal_summary.json"),
        (FIX / "_provenance.json", run / "underwriting" / "_provenance.json"),
        (FIX / "_lifecycle_state.json", run / "_lifecycle_state.json"),
    ]:
        shutil.copy(src, dst)
    return run


def test_memo_step_implements_protocol() -> None:
    step: LifecycleStep = MemoStep()
    assert step.name == "memo"


def test_memo_step_run_writes_full_artifact_set(tmp_path: Path) -> None:
    run = _build_run(tmp_path)
    state = LifecycleState(deal_slug="project_essex", run_id="run_002",
                           steps_completed=["intake", "comps", "judgment", "underwriting"])

    # Force fallback path so no mfu dependency in this test
    with patch("plat_agent.lifecycle.memo._import_mfu_onepager", return_value=None):
        result = MemoStep().run(state, run)

    assert result.status == "ok"
    assert (run / "memo" / "memo.md").exists()
    # PDF may be ok or failed depending on reportlab; either way no crash
    assert (run / "memo" / "_provenance.json").exists()
    assert (run / "memo" / "_complete").exists()


def test_memo_step_provenance_carries_input_hash_and_pdf_status(tmp_path: Path) -> None:
    run = _build_run(tmp_path)
    state = LifecycleState(deal_slug="d", run_id="r")

    with patch("plat_agent.lifecycle.memo._import_mfu_onepager", return_value=None), \
         patch("plat_agent.lifecycle.memo._reportlab_fallback") as fb:
        fb.side_effect = lambda c, out: out.write_bytes(b"%PDF-test")
        MemoStep().run(state, run)

    prov = json.loads((run / "memo" / "_provenance.json").read_text())
    assert "input_hash" in prov
    assert len(prov["input_hash"]) == 64
    assert "contract_version" in prov
    assert prov["pdf_status"] == "fallback"
    assert "judgment_mode" in prov
    # Per plan-04 contract: CRM step reads memo_path/pdf_path from provenance,
    # so MemoStep MUST persist them.
    assert prov["memo_path"].endswith("/memo/memo.md")
    assert Path(prov["memo_path"]).is_absolute()
    assert prov["pdf_path"].endswith("/memo/memo.pdf")


def test_memo_step_provenance_pdf_path_null_when_pdf_failed(tmp_path: Path) -> None:
    """When PDF rendering fails, pdf_path must be null (not a stale string)."""
    run = _build_run(tmp_path)
    state = LifecycleState(deal_slug="d", run_id="r")

    with patch("plat_agent.lifecycle.memo._import_mfu_onepager", return_value=None), \
         patch("plat_agent.lifecycle.memo._reportlab_fallback",
               side_effect=RuntimeError("no reportlab")):
        MemoStep().run(state, run)

    prov = json.loads((run / "memo" / "_provenance.json").read_text())
    assert prov["pdf_status"] == "failed"
    assert prov["pdf_path"] is None
    assert prov["memo_path"].endswith("/memo/memo.md")


def test_memo_step_complete_marker_lists_full_manifest(tmp_path: Path) -> None:
    run = _build_run(tmp_path)
    state = LifecycleState(deal_slug="d", run_id="r")

    with patch("plat_agent.lifecycle.memo._import_mfu_onepager", return_value=None), \
         patch("plat_agent.lifecycle.memo._reportlab_fallback") as fb:
        fb.side_effect = lambda c, out: out.write_bytes(b"%PDF")
        MemoStep().run(state, run)

    marker = json.loads((run / "memo" / "_complete").read_text())
    assert marker["step"] == "memo"
    assert "memo.md" in marker["file_manifest"]
    assert "_provenance.json" in marker["file_manifest"]


def test_memo_step_complete_marker_omits_pdf_when_failed(tmp_path: Path) -> None:
    """When PDF rendering fails, _complete must not list memo.pdf."""
    run = _build_run(tmp_path)
    state = LifecycleState(deal_slug="d", run_id="r")

    with patch("plat_agent.lifecycle.memo._import_mfu_onepager", return_value=None), \
         patch("plat_agent.lifecycle.memo._reportlab_fallback",
               side_effect=RuntimeError("no reportlab")):
        result = MemoStep().run(state, run)

    assert result.status == "ok"  # memo is still "ok" per §2.5
    marker = json.loads((run / "memo" / "_complete").read_text())
    assert "memo.pdf" not in marker["file_manifest"]


def test_memo_step_run_idempotent(tmp_path: Path) -> None:
    """Running twice produces identical memo.md (deterministic)."""
    run = _build_run(tmp_path)
    state = LifecycleState(deal_slug="d", run_id="r")

    with patch("plat_agent.lifecycle.memo._import_mfu_onepager", return_value=None), \
         patch("plat_agent.lifecycle.memo._reportlab_fallback") as fb:
        fb.side_effect = lambda c, out: out.write_bytes(b"%PDF")
        MemoStep().run(state, run)
        first = (run / "memo" / "memo.md").read_text()
        MemoStep().run(state, run)
        second = (run / "memo" / "memo.md").read_text()

    assert first == second


def test_memo_renders_draft_when_canonical_absent(tmp_path: Path) -> None:
    """V1.1 Sample Annex Tower shape: when intake blocks, canonical_deal.json is absent.
    MemoStep must still render memo.md with a 'Draft — intake blocked'
    banner, NEEDS_DATA recommendation, and the punchlist embedded so the
    analyst can see what to drop in. Per spec §1 + §4.6."""
    from plat_agent.lifecycle.punchlist import write_punchlist_json
    from plat_agent.lifecycle.state import BlockerItem

    run = tmp_path / "run_001"
    run.mkdir()
    # No intake/canonical_deal.json, no judgment/positioning.json.
    # Intake step did, however, drop a punchlist.
    write_punchlist_json(run, [
        BlockerItem(
            step="intake", id="rent_roll_missing",
            description="No rent roll detected in raw_inputs/.",
            resolution_hint="Drop broker rent roll into raw_inputs/ and rerun.",
        ),
    ])
    state = LifecycleState(deal_slug="sample_annex_tower_building", run_id="run_001",
                           status="failed_at_intake")

    result = MemoStep().run(state, run)

    assert result.status == "ok"
    memo_md = (run / "memo" / "memo.md").read_text()
    # Banner present
    assert "Draft — intake blocked" in memo_md
    # Recommendation defaulted
    assert "NEEDS_DATA" in memo_md
    # Punchlist embedded by id
    assert "rent_roll_missing" in memo_md


def test_memo_step_returns_ok_in_canonical_absent_draft_mode(tmp_path: Path) -> None:
    """V1.1: even in canonical-absent draft mode, MemoStep returns
    StepResult.ok — the draft memo was produced, which is the contract."""
    run = tmp_path / "run_001"
    run.mkdir()
    state = LifecycleState(deal_slug="d", run_id="run_001",
                           status="failed_at_intake")
    result = MemoStep().run(state, run)

    assert result.status == "ok"
    # Provenance records the draft-mode flag
    prov = json.loads((run / "memo" / "_provenance.json").read_text())
    assert prov["draft_mode_canonical_absent"] is True
    # PDF was skipped (no metrics to render)
    assert prov["pdf_status"] == "skipped"
    assert prov["pdf_path"] is None


def test_memo_step_is_satisfied_delegates_to_cache(tmp_path: Path) -> None:
    """Default is_satisfied delegates to lifecycle.cache.is_satisfied."""
    run = _build_run(tmp_path)
    state = LifecycleState(deal_slug="d", run_id="r", steps_completed=[])
    # No prior run → not satisfied
    assert MemoStep().is_satisfied(state, run) is False
