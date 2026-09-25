"""Integration test: resume after analyst clears a blocker."""

import json
import shutil
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from plat_agent.lifecycle.atomic import atomic_write_json, atomic_write_text
from plat_agent.lifecycle.cache import write_provenance
from plat_agent.lifecycle.complete_marker import write_complete_marker
from plat_agent.lifecycle.protocol import StepResult
from plat_agent.lifecycle.runner import run_lifecycle
from plat_agent.lifecycle.state import BlockerItem


@pytest.fixture
def project_root(tmp_path: Path) -> Path:
    p = tmp_path / "project_root"; p.mkdir(); return p


@pytest.fixture
def data_room(tmp_path: Path) -> Path:
    src = tmp_path / "data_room"; src.mkdir()
    (src / "OM.pdf").write_bytes(b"x")
    return src


def _make_blocker_intake(blocker_on_first_call: bool):
    """An intake step that emits a blocker on first call, ok on second."""
    call_count = {"n": 0}
    def run(state, run_dir):
        d = run_dir / "intake"; d.mkdir(exist_ok=True)
        atomic_write_json(d / "canonical_deal.json", {
            "metadata": {"address": "1 Test Way", "year_built": 2005},
            "unit_cohorts": [{"cohort_id": "c1", "unit_count": 100}],
            "purchase_assumptions": {"purchase_price": 25_000_000},
        })
        write_provenance(d, input_hash="x", status="ok")
        write_complete_marker(d, step="intake",
                             file_manifest=["canonical_deal.json", "_provenance.json"])
        call_count["n"] += 1
        if blocker_on_first_call and call_count["n"] == 1:
            return StepResult(
                status="blocked",
                blockers=[BlockerItem(step="intake", id="missing_t12",
                                      description="No T12 found.")],
            )
        return StepResult(status="ok")
    m = MagicMock(); m.name = "intake"; m.run.side_effect = run
    m.is_satisfied = MagicMock(return_value=False)
    return m, call_count


def _ok_writing_step(name: str, writer):
    def run(state, run_dir):
        writer(run_dir)
        return StepResult(status="ok")
    m = MagicMock(); m.name = name; m.run.side_effect = run
    m.is_satisfied = MagicMock(return_value=False); return m


def test_resume_after_blocker_reaches_memo_ready(data_room: Path,
                                                 project_root: Path) -> None:
    """First run blocked at intake; analyst clears punchlist; resume completes."""
    intake_step, _ = _make_blocker_intake(blocker_on_first_call=True)

    def comps_writer(run_dir):
        d = run_dir / "comps"; d.mkdir(exist_ok=True)
        atomic_write_json(d / "comps.json", {
            "subject": {"address": "1 Test Way", "metro_slug": "dallas_tx"},
            "as_of": "2026-05-05",
            "comps": [{"comp_id": "c1", "name": "x", "address": "x", "units": 240}],
        })
        write_provenance(d, input_hash="x", status="ok")
        write_complete_marker(d, step="comps",
                             file_manifest=["comps.json", "_provenance.json"])

    def judgment_writer(run_dir):
        d = run_dir / "judgment"; d.mkdir(exist_ok=True)
        atomic_write_json(d / "positioning.json", {
            "positioning": {"confidence": 0.85},
            "leverage": {"source": "v1_hardcoded"}, "blockers": []})
        atomic_write_json(d / "engine_inputs.json", {})
        write_provenance(d, input_hash="x", status="ok")
        write_complete_marker(d, step="judgment",
                             file_manifest=["positioning.json", "engine_inputs.json",
                                            "_provenance.json"])

    def uw_writer(run_dir):
        d = run_dir / "underwriting"; d.mkdir(exist_ok=True)
        atomic_write_json(d / "deal_summary.json", {
            "metrics": {"irr": {"levered_irr": 0.15},
                        "dscr": {"minimum_dscr": 1.25}, "coc": {"cash_on_cash_year_1": 0.08}}})
        write_provenance(d, input_hash="x", status="ok")
        write_complete_marker(d, step="underwriting",
                             file_manifest=["deal_summary.json", "_provenance.json"])

    def memo_writer(run_dir):
        d = run_dir / "memo"; d.mkdir(exist_ok=True)
        atomic_write_text(d / "memo.md", "# memo")
        write_provenance(d, input_hash="x", status="ok")
        write_complete_marker(d, step="memo",
                             file_manifest=["memo.md", "_provenance.json"])

    def crm_writer(run_dir):
        d = run_dir / "crm"; d.mkdir(exist_ok=True)
        write_provenance(d, input_hash="x", status="ok")
        write_complete_marker(d, step="crm", file_manifest=["_provenance.json"])

    steps = {
        "intake": intake_step,
        "comps": _ok_writing_step("comps", comps_writer),
        "judgment": _ok_writing_step("judgment", judgment_writer),
        "underwriting": _ok_writing_step("underwriting", uw_writer),
        "memo": _ok_writing_step("memo", memo_writer),
        "crm": _ok_writing_step("crm", crm_writer),
    }

    # First run — intake blocks
    first = run_lifecycle(data_room, deal_slug="d", project_root=project_root,
                         steps=steps)
    assert first.status == "memo_ready_with_blockers"
    assert first.blocker_count == 1

    # Punchlist sidecar should reflect the blocker
    run_dir = project_root / "runs" / "deals" / "d" / "outputs" / first.run_id
    from plat_agent.lifecycle.punchlist import (
        read_punchlist_json,
        write_punchlist_json,
        write_punchlist_markdown,
    )
    # NOTE: in real usage, the IntakeStep adapter writes the punchlist; here
    # we simulate that by writing it ourselves.
    write_punchlist_json(run_dir, [
        BlockerItem(step="intake", id="missing_t12",
                   description="No T12 found.", cleared=False)
    ])
    write_punchlist_markdown(run_dir, "d", first.run_id, [
        BlockerItem(step="intake", id="missing_t12",
                   description="No T12 found.", cleared=False)
    ])

    # Analyst checks the box
    md_path = run_dir / "punchlist.md"
    md_text = md_path.read_text().replace("- [ ] **missing_t12**",
                                          "- [x] **missing_t12**")
    md_path.write_text(md_text)

    # Mark intake satisfied so resume skips it; everything else runs
    steps["intake"].is_satisfied.return_value = True

    second = run_lifecycle(data_room, deal_slug="d", project_root=project_root,
                          steps=steps, resume_run_id=first.run_id)
    assert second.status == "memo_ready"
    # Punchlist now cleared
    reconciled = read_punchlist_json(run_dir)
    assert all(b.cleared for b in reconciled)
