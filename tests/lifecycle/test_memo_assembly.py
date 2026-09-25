# tests/lifecycle/test_memo_assembly.py
"""Memo content assembler — reads upstream artifacts into a typed MemoContent."""

from __future__ import annotations

import shutil
import json
from pathlib import Path

import pytest

from plat_agent.lifecycle.memo import (
    MemoContent,
    assemble_memo_content,
)


FIX = Path(__file__).parent / "fixtures" / "memo" / "clean"

PROPERTY_TAX_CALCULATION = {
    "millage_rate_mills": 25.31,
    "decimal_tax_rate": 0.02531,
    "assessment_ratio": 1.0,
    "purchase_price_basis": 10_000_000.0,
    "assessed_value_basis": 10_000_000.0,
    "annual_ad_valorem_tax": 253_100.0,
    "source": "county_tax_notice",
    "source_locator": "raw_inputs/2026_tax_notice.pdf, page 2",
    "analyst_override": False,
}


def _build_run(tmp_path: Path) -> Path:
    run = tmp_path / "run_002"
    (run / "intake").mkdir(parents=True)
    (run / "comps").mkdir()
    (run / "judgment").mkdir()
    (run / "underwriting").mkdir()
    shutil.copy(FIX / "canonical_deal.json", run / "intake" / "canonical_deal.json")
    shutil.copy(FIX / "comps.json", run / "comps" / "comps.json")
    shutil.copy(FIX / "positioning.json", run / "judgment" / "positioning.json")
    shutil.copy(FIX / "thesis.md", run / "judgment" / "thesis.md")
    shutil.copy(FIX / "deal_summary.json", run / "underwriting" / "deal_summary.json")
    shutil.copy(FIX / "_provenance.json", run / "underwriting" / "_provenance.json")
    shutil.copy(FIX / "_lifecycle_state.json", run / "_lifecycle_state.json")
    return run




def test_assemble_memo_content_clean_path(tmp_path: Path) -> None:
    run = _build_run(tmp_path)
    content = assemble_memo_content(run)
    assert isinstance(content, MemoContent)
    assert content.deal_slug == "project_essex"
    assert content.address == "1234 Essex Ave, Dallas, TX 75201"
    assert content.units == 240
    assert content.vintage == 2005
    assert content.broker == "CBRE"
    assert content.asking_price == 28_000_000
    assert content.ppu == pytest.approx(28_000_000 / 240)
    assert content.recommendation == "PROCEED"
    assert content.recommendation_confidence == pytest.approx(0.78)
    assert content.thesis_text.startswith("Subject is a 240-unit")
    assert content.metrics["levered_irr"] == pytest.approx(0.152)
    assert content.metrics["min_dscr"] == pytest.approx(1.28)
    assert content.sanity_flags == ["exit_cap_below_going_in"]
    assert content.uncleared_blockers == []
    assert content.judgment_mode == "deterministic_v1"
    assert content.passthrough is False


def test_assemble_memo_content_projects_property_tax_calculation(
    tmp_path: Path,
) -> None:
    run = _build_run(tmp_path)
    deal_summary_path = run / "underwriting" / "deal_summary.json"
    deal_summary = json.loads(deal_summary_path.read_text())
    deal_summary["property_tax_calculation"] = PROPERTY_TAX_CALCULATION
    deal_summary_path.write_text(json.dumps(deal_summary))

    content = assemble_memo_content(run)

    assert content.property_tax == PROPERTY_TAX_CALCULATION


def test_assemble_derives_exit_cap_compression_risk_when_sidecars_omit_it(
    tmp_path: Path,
) -> None:
    import json

    run = _build_run(tmp_path)
    provenance_path = run / "underwriting" / "_provenance.json"
    provenance = json.loads(provenance_path.read_text())
    provenance["feasibility_sanity_flags"] = []
    provenance_path.write_text(json.dumps(provenance))
    summary_path = run / "underwriting" / "deal_summary.json"
    summary = json.loads(summary_path.read_text())
    summary["sanity_flags"] = []
    summary["metrics"]["yields"] = {
        "going_in_cap_rate": 0.0873,
        "exit_cap_rate": 0.06,
    }
    summary_path.write_text(json.dumps(summary))

    content = assemble_memo_content(run)

    assert "EXIT_CAP_LOWER_THAN_GOING_IN" in content.sanity_flags


def test_assemble_handles_missing_underwriting(tmp_path: Path) -> None:
    """Per §4.3: underwriting hard-error → memo runs in 'draft' mode."""
    run = _build_run(tmp_path)
    (run / "underwriting" / "deal_summary.json").unlink()
    content = assemble_memo_content(run)
    assert content.metrics == {}  # all metrics absent
    assert content.draft_mode is True


def test_assemble_handles_empty_unit_surface(tmp_path: Path) -> None:
    run = _build_run(tmp_path)
    import json

    canonical_path = run / "intake" / "canonical_deal.json"
    canonical = json.loads(canonical_path.read_text())
    canonical["unit_cohorts"] = []
    canonical["metadata"]["property_summary"] = {
        "house_box_score": {"total_units": None},
        "box_score": {"total_units": None},
    }
    canonical_path.write_text(json.dumps(canonical))

    content = assemble_memo_content(run)

    assert content.units == 0
    assert content.ppu is None


def test_assemble_handles_missing_thesis(tmp_path: Path) -> None:
    run = _build_run(tmp_path)
    (run / "judgment" / "thesis.md").unlink()
    content = assemble_memo_content(run)
    assert content.thesis_text == "(thesis unavailable)"


def test_assemble_collects_blockers_from_punchlist(tmp_path: Path) -> None:
    from plat_agent.lifecycle.punchlist import write_punchlist_json
    from plat_agent.lifecycle.state import BlockerItem

    run = _build_run(tmp_path)
    write_punchlist_json(run, [
        BlockerItem(step="intake", id="missing_t12", description="No T12.", cleared=False),
        BlockerItem(step="comps", id="cleared_thing", description="x", cleared=True),
    ])
    content = assemble_memo_content(run)
    ids = {b.id for b in content.uncleared_blockers}
    assert ids == {"missing_t12"}  # cleared item filtered out


def test_assemble_when_canonical_missing_returns_draft_mode(tmp_path: Path) -> None:
    """V1.1 (Demo Annex): when intake/canonical_deal.json is absent, assembly does
    NOT raise. Instead it returns a MemoContent with
    `draft_mode_canonical_absent=True` so MemoStep can render a draft memo
    + punchlist for the analyst (per spec §1 + §4.6)."""
    run = tmp_path / "run_002"
    run.mkdir()
    content = assemble_memo_content(run)
    assert content.draft_mode_canonical_absent is True
    assert content.recommendation == "NEEDS_DATA"
    # No data → empty/zero defaults, not a raise
    assert content.units == 0
    assert content.address == ""


def test_assemble_passthrough_mode_flags_content(tmp_path: Path) -> None:
    run = _build_run(tmp_path)
    # Simulate orchestrator setting judgment_mode in lifecycle state
    import json
    state_path = run / "_lifecycle_state.json"
    state = json.loads(state_path.read_text())
    state["judgment_mode"] = "passthrough"
    state_path.write_text(json.dumps(state))
    content = assemble_memo_content(run)
    assert content.judgment_mode == "passthrough"
    assert content.passthrough is True
