"""V1.1 Sample Annex Tower-shape integration smoke (regression for §1 + §4.6).

Synthesizes a data room that mimics the real-world Sample Annex Tower Building deal:
OM PDF + T12 xlsx, NO rent roll. The intake sibling correctly raises a
B1 rent_roll_missing blocker. Pre-V1.1 the lifecycle halted at intake and
produced no draft memo or null-row CRM, breaking V1's contract.

This test exercises the full orchestrator (run_id alloc, raw_inputs
snapshot, dependency map, recommendation patch, CRM step) end-to-end with
the dispatch boundary mocked — same pattern used by every other lifecycle
integration test (see integration/conftest.py docstring). The intake step
is the only step we hand-roll: it writes a punchlist with the
rent_roll_missing blocker, returns status="blocked", and crucially does
NOT write canonical_deal.json (because real intake refuses to canonicalize
a deal without a rent roll).

Asserts:
  - intake produced a punchlist with B1 rent_roll_missing
  - judgment step ran but had no canonical to operate on (skipped writing
    positioning.json)
  - underwriting was skipped per §4.3 row 4 (judgment blocker → skip UW)
  - memo.md exists with "Draft — intake blocked" banner + embedded blocker
  - CRM has a row with status="memo_ready_with_blockers" or similar,
    null IRR/EM, recommendation="NEEDS_DATA"
  - final lifecycle state is memo_ready_with_blockers (NOT failed_at_memo)
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

from plat_agent.lifecycle.cache import write_provenance
from plat_agent.lifecycle.complete_marker import write_complete_marker
from plat_agent.lifecycle.crm import crm_path, list_deals
from plat_agent.lifecycle.protocol import StepResult
from plat_agent.lifecycle.punchlist import read_punchlist_json, write_punchlist_json
from plat_agent.lifecycle.runner import run_lifecycle
from plat_agent.lifecycle.state import BlockerItem


def _write_sample_annex_tower_data_room(root: Path) -> Path:
    """Build a synthetic Sample Annex Tower-shaped room: OM + T12, NO rent roll."""
    root.mkdir(parents=True, exist_ok=True)
    # OM "PDF" — content doesn't matter for this test (intake is mocked)
    (root / "Sample Annex Tower-OM.pdf").write_bytes(b"%PDF-1.4\n% synthetic Sample Annex Tower OM\n")
    # T12 "xlsx" — same: mocked intake doesn't actually parse it
    (root / "Sample Annex Tower-T12.xlsx").write_bytes(b"PK\x03\x04synthetic-xlsx-bytes")
    # Deliberately: NO rent roll. This is the Sample Annex Tower shape.
    return root


def test_lifecycle_produces_draft_memo_and_crm_row_for_sample_annex_tower_shape(
    tmp_path: Path,
    project_root: Path,
    make_default_steps,
) -> None:
    data_room = _write_sample_annex_tower_data_room(tmp_path / "the_sample_annex_tower_building")

    # Mock intake: returns blocked + writes punchlist; does NOT write
    # canonical_deal.json (real intake refuses to canonicalize without a
    # rent roll). This is the V1.1 stress-shape.
    def intake_run(state, run_dir):
        d = run_dir / "intake"
        d.mkdir(exist_ok=True)
        # Intake DID get far enough to write a manifest and a punchlist,
        # but NOT canonical_deal.json.
        (d / "manifest.md").write_text("# intake manifest (incomplete)\n")
        write_provenance(d, input_hash="x", status="blocked")
        write_complete_marker(
            d, step="intake",
            file_manifest=["manifest.md", "_provenance.json"],
        )
        blocker = BlockerItem(
            step="intake", id="rent_roll_missing",
            description="No rent roll detected in raw_inputs/.",
            resolution_hint=(
                "Drop a broker-provided rent roll (CSV or XLSX) into "
                "raw_inputs/ and rerun with --resume."
            ),
        )
        write_punchlist_json(run_dir, [blocker])
        return StepResult(status="blocked", blockers=[blocker])

    intake_step = MagicMock()
    intake_step.name = "intake"
    intake_step.run.side_effect = intake_run
    intake_step.is_satisfied = MagicMock(return_value=False)

    # Judgment step also can't run without canonical — emulate a soft
    # blocker that mirrors what real judgment does when positioning has
    # nothing to position on. Doesn't write positioning.json.
    def judgment_blocked_run(state, run_dir):
        d = run_dir / "judgment"
        d.mkdir(exist_ok=True)
        write_provenance(d, input_hash="x", status="blocked")
        write_complete_marker(
            d, step="judgment",
            file_manifest=["_provenance.json"],
        )
        b = BlockerItem(
            step="judgment", id="no_canonical",
            description="Cannot run judgment: canonical_deal.json absent.",
        )
        return StepResult(status="blocked", blockers=[b])

    judgment_step = MagicMock()
    judgment_step.name = "judgment"
    judgment_step.run.side_effect = judgment_blocked_run
    judgment_step.is_satisfied = MagicMock(return_value=False)

    steps = make_default_steps()
    steps["intake"] = intake_step
    steps["judgment"] = judgment_step
    # Use the REAL MemoStep + CRMStep so we exercise the V1.1 graceful
    # degradation paths added in this PR series.
    from plat_agent.lifecycle.memo import MemoStep
    from plat_agent.lifecycle.crm import CRMStep
    steps["memo"] = MemoStep()
    steps["crm"] = CRMStep(runs_deals_root=project_root / "runs" / "deals")

    result = run_lifecycle(
        data_room,
        deal_slug="the_sample_annex_tower_building",
        project_root=project_root,
        steps=steps,
    )

    run_dir = (
        project_root / "runs" / "deals" / "the_sample_annex_tower_building"
        / "outputs" / result.run_id
    )

    # --- Intake produced the punchlist -----------------------------------
    blockers = read_punchlist_json(run_dir)
    assert any(
        b.id == "rent_roll_missing" and b.step == "intake" and not b.cleared
        for b in blockers
    ), f"Expected rent_roll_missing blocker; got {blockers}"

    # --- Underwriting was skipped (judgment blocker → §4.3 row 4) -------
    steps["underwriting"].run.assert_not_called()

    # --- Memo DID render in canonical-absent draft mode -----------------
    memo_md = run_dir / "memo" / "memo.md"
    assert memo_md.exists(), "memo.md must exist even when intake blocked"
    body = memo_md.read_text()
    assert "Draft — intake blocked" in body
    assert "rent_roll_missing" in body
    assert "NEEDS_DATA" in body

    # --- CRM has a row with the right shape -----------------------------
    crm_rows = list_deals(crm_path(project_root / "runs" / "deals"))
    assert len(crm_rows) == 1, f"Expected exactly one CRM row, got {crm_rows}"
    row = crm_rows[0]
    assert row.deal_slug == "the_sample_annex_tower_building"
    assert row.recommendation == "NEEDS_DATA"
    assert row.levered_irr is None
    assert row.equity_multiple is None
    # Status reflects blocker-not-error path (intake produced a recoverable
    # blocker, not a hard error).
    assert row.status in {
        "memo_ready_with_blockers", "failed_at_intake",
    }, f"Unexpected status {row.status!r}"

    # --- Final lifecycle state is NOT failed_at_memo --------------------
    assert result.status != "failed_at_memo", (
        "Memo must not be reported as failed when it produced a draft+punchlist"
    )
    # Spec §4.3 row 4 (judgment blocker): pipeline finishes
    # memo_ready_with_blockers
    assert result.status == "memo_ready_with_blockers", result.status

    # --- Memo provenance flags the canonical-absent draft mode ----------
    memo_prov = json.loads((run_dir / "memo" / "_provenance.json").read_text())
    assert memo_prov["draft_mode_canonical_absent"] is True
    assert memo_prov["pdf_status"] == "skipped"
