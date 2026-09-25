# tests/lifecycle/test_memo_markdown.py
"""End-to-end Markdown rendering: MemoContent → memo.md text."""

from __future__ import annotations

import shutil
import json
from pathlib import Path

import pytest

from plat_agent.lifecycle.memo import (
    assemble_memo_content,
    render_memo_markdown,
    write_memo_markdown,
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


def test_render_markdown_clean_path_contains_expected_sections(tmp_path: Path) -> None:
    run = _build_run(tmp_path)
    content = assemble_memo_content(run)
    md = render_memo_markdown(content)

    assert "# Deal Memo — project_essex" in md
    assert "1234 Essex Ave" in md
    assert "240" in md  # units
    assert "## Recommendation" in md
    assert "**PROCEED**" in md
    assert "## Returns at a glance" in md
    assert "15.2%" in md  # levered IRR
    assert "1.28x" in md  # min DSCR
    assert "## Business plan thesis" in md
    assert "240-unit value-add" in md
    assert "## Broker claim validation" in md
    assert "Rent growth" in md
    assert "broker_optimistic" in md
    assert "## Risks & blockers" in md
    assert "exit_cap_below_going_in" in md
    assert "## Path to artifacts" in md


def test_render_markdown_projects_property_tax_provenance(tmp_path: Path) -> None:
    run = _build_run(tmp_path)
    deal_summary_path = run / "underwriting" / "deal_summary.json"
    deal_summary = json.loads(deal_summary_path.read_text())
    deal_summary["property_tax_calculation"] = PROPERTY_TAX_CALCULATION
    deal_summary_path.write_text(json.dumps(deal_summary))

    md = render_memo_markdown(assemble_memo_content(run))

    assert (
        "Property tax: 25.310 mills (2.5310%) × 100.00% assessment ratio"
        in md
    )
    assert (
        "Basis: $10,000,000 purchase price → $10,000,000 assessed-value basis"
        in md
    )
    assert "Annual ad valorem tax: $253,100.00" in md
    assert (
        "Source: county_tax_notice — raw_inputs/2026_tax_notice.pdf, page 2"
        in md
    )
    assert "Assessment-ratio override: No" in md


def test_render_markdown_reconciles_ancillary_income_bridge(tmp_path: Path) -> None:
    """Material ancillary income is decision-useful and must be memo-visible."""
    import json

    run = _build_run(tmp_path)
    bridge_dir = run / "reconciliation_house_case"
    bridge_dir.mkdir()
    (bridge_dir / "revenue_quality_bridge.json").write_text(
        json.dumps(
            {
                "source_summary": {
                    "broker_year1_other_income": 849_745.0,
                    "t12_grouped_ancillary_income": 799_551.67,
                    "house_annual_credit": 677_891.34,
                },
                "lines": [
                    {
                        "line_item": "utility_hoa_reimbursements",
                        "broker_amount": 506_906.0,
                        "t12_amount": 478_924.88,
                        "house_credit": 478_924.88,
                        "paired_expense": 685_688.02,
                        "decision": "keep",
                    },
                    {
                        "line_item": "cable_tv_income",
                        "broker_amount": 12_408.0,
                        "t12_amount": 11_608.77,
                        "house_credit": 0.0,
                        "decision": "exclude",
                    },
                ],
            }
        )
    )
    summary_path = run / "underwriting" / "deal_summary.json"
    summary = json.loads(summary_path.read_text())
    summary["cashflow"] = {
        "by_year": [
            {"year": "2026", "net_programs": 280_336.30},
            {"year": "2027", "net_programs": 672_807.12},
        ]
    }
    summary_path.write_text(json.dumps(summary))
    canonical_path = run / "intake" / "canonical_deal.json"
    canonical = json.loads(canonical_path.read_text())
    canonical["time_grid"] = {"analysis_start_date": "2026-08-01"}
    canonical_path.write_text(json.dumps(canonical))
    (run / "judgment" / "engine_inputs.json").write_text(
        json.dumps(
            {
                "metadata": {
                    "property_summary": {
                        "year1_insurance_per_unit_applied": 900.0,
                        "insurance_claim_review_required": True,
                    }
                }
            }
        )
    )

    md = render_memo_markdown(assemble_memo_content(run))

    assert "## Ancillary income bridge" in md
    assert "$849,745" in md
    assert "$799,552" in md
    assert "$677,891" in md
    assert "$672,807" in md
    assert "$685,688" in md
    assert "retained in OpEx" in md
    assert "$900/unit insurance assumption requires claims-history review" in md
    assert "utility_hoa_reimbursements" in md
    assert "cable_tv_income" in md
    assert "exclude" in md


def test_ancillary_engine_net_uses_first_twelve_analysis_months(
    tmp_path: Path,
) -> None:
    import json

    run = _build_run(tmp_path)
    bridge_dir = run / "reconciliation_house_case"
    bridge_dir.mkdir()
    (bridge_dir / "revenue_quality_bridge.json").write_text(
        json.dumps({"source_summary": {}, "lines": []})
    )
    summary_path = run / "underwriting" / "deal_summary.json"
    summary = json.loads(summary_path.read_text())
    summary["cashflow"] = {
        "by_month": [
            {"month": f"2026-{month:02d}", "net_programs": month * 100.0}
            for month in range(1, 13)
        ],
        "by_year": [
            {"year": "2026", "net_programs": 5_000.0},
            {"year": "2027", "net_programs": 15_000.0},
        ],
    }
    summary_path.write_text(json.dumps(summary))
    canonical_path = run / "intake" / "canonical_deal.json"
    canonical = json.loads(canonical_path.read_text())
    canonical["time_grid"] = {"analysis_start_date": "2026-08-01"}
    canonical_path.write_text(json.dumps(canonical))

    content = assemble_memo_content(run)

    assert content.revenue_quality is not None
    assert content.revenue_quality["net_house_credit"] == 7_800.0

def test_render_markdown_includes_state_blockers_when_punchlist_empty(tmp_path: Path) -> None:
    """State blockers are memo-visible even before punchlist persistence catches up."""
    from plat_agent.lifecycle.punchlist import write_punchlist_json
    from plat_agent.lifecycle.state import BlockerItem, LifecycleState

    run = _build_run(tmp_path)
    write_punchlist_json(run, [])
    state = LifecycleState(
        deal_slug="project_essex",
        run_id="run_002",
        blockers=[
            BlockerItem(
                step="comps",
                id="comps_dispatch_failed",
                description="Sibling dispatch failed before comps artifacts were written.",
            ),
        ],
    )

    content = assemble_memo_content(run, state=state)
    md = render_memo_markdown(content)

    assert [f"{b.step}/{b.id}" for b in content.uncleared_blockers] == [
        "comps/comps_dispatch_failed"
    ]
    assert "- `comps/comps_dispatch_failed`: Sibling dispatch failed" in md


def test_render_markdown_passthrough_banner_present(tmp_path: Path) -> None:
    run = _build_run(tmp_path)
    import json
    state_path = run / "_lifecycle_state.json"
    state = json.loads(state_path.read_text())
    state["judgment_mode"] = "passthrough"
    state_path.write_text(json.dumps(state))
    content = assemble_memo_content(run)
    md = render_memo_markdown(content)
    assert "TEST RUN" in md
    assert "judgment layer bypassed" in md
    # Banner must come before the title
    assert md.index("TEST RUN") < md.index("# Deal Memo")


def test_render_markdown_no_banner_in_deterministic_mode(tmp_path: Path) -> None:
    run = _build_run(tmp_path)
    content = assemble_memo_content(run)
    md = render_memo_markdown(content)
    assert "TEST RUN" not in md


def test_write_memo_markdown_atomic(tmp_path: Path) -> None:
    run = _build_run(tmp_path)
    content = assemble_memo_content(run)
    path = write_memo_markdown(run, content)
    assert path == run / "memo" / "memo.md"
    assert path.read_text().startswith("# Deal Memo") or path.read_text().startswith("> **TEST RUN")
    # No tmp leftovers
    assert list((run / "memo").glob("*.tmp*")) == []


def test_render_markdown_handles_no_broker_claims(tmp_path: Path) -> None:
    """Per §4.3 judgment-blocker degraded path: positioning.json may have no triples."""
    run = _build_run(tmp_path)
    import json
    pos_path = run / "judgment" / "positioning.json"
    pos = json.loads(pos_path.read_text())
    for k in ["rent_growth", "capex_per_unit", "exit_cap", "renovation_pace_units_per_month"]:
        pos.pop(k, None)
    pos_path.write_text(json.dumps(pos))
    content = assemble_memo_content(run)
    md = render_memo_markdown(content)
    assert "No broker claims to reconcile" in md


def test_render_markdown_draft_mode_all_dashes(tmp_path: Path) -> None:
    run = _build_run(tmp_path)
    (run / "underwriting" / "deal_summary.json").unlink()
    content = assemble_memo_content(run)
    md = render_memo_markdown(content)
    # All five returns rows should render as '—'
    assert md.count("| — |") >= 4


def test_passthrough_mode_end_to_end_banner(tmp_path: Path) -> None:
    """§5.4 contract: passthrough mode injects banner; deterministic mode does not."""
    fix_pt = Path(__file__).parent / "fixtures" / "memo" / "passthrough"
    fix_clean = Path(__file__).parent / "fixtures" / "memo" / "clean"

    run = tmp_path / "run_002"
    (run / "intake").mkdir(parents=True)
    (run / "comps").mkdir()
    (run / "judgment").mkdir()
    (run / "underwriting").mkdir()
    shutil.copy(fix_clean / "canonical_deal.json", run / "intake" / "canonical_deal.json")
    shutil.copy(fix_clean / "comps.json", run / "comps" / "comps.json")
    shutil.copy(fix_clean / "positioning.json", run / "judgment" / "positioning.json")
    shutil.copy(fix_clean / "thesis.md", run / "judgment" / "thesis.md")
    shutil.copy(fix_clean / "deal_summary.json", run / "underwriting" / "deal_summary.json")
    shutil.copy(fix_clean / "_provenance.json", run / "underwriting" / "_provenance.json")
    shutil.copy(fix_pt / "_lifecycle_state.json", run / "_lifecycle_state.json")

    content = assemble_memo_content(run)
    assert content.passthrough is True
    md = render_memo_markdown(content)
    assert "> **TEST RUN" in md.splitlines()[0]  # banner is first line
    assert "passthrough" in md.lower()
