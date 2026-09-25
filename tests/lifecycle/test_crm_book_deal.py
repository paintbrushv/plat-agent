# tests/lifecycle/test_crm_book_deal.py
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from plat_agent.lifecycle.crm import CRMRow, book_deal, crm_path
from plat_agent.lifecycle.state import LifecycleState

FIXTURES = Path(__file__).parent.parent / "fixtures" / "lifecycle" / "crm"

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


def _final_state(**overrides) -> LifecycleState:
    base = dict(
        deal_slug="project_essex",
        run_id="run_002",
        status="memo_ready",
        steps_completed=["intake", "comps", "judgment", "underwriting", "memo", "crm"],
        blockers=[],
        finished_at=datetime(2026, 5, 5, 16, 30, tzinfo=timezone.utc),
    )
    base.update(overrides)
    return LifecycleState(**base)


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def test_book_deal_extracts_required_fields(tmp_path: Path) -> None:
    crm_file = crm_path(tmp_path)
    row = book_deal(
        crm_jsonl_path=crm_file,
        canonical_deal=_load("canonical_deal.json"),
        positioning=_load("positioning.json"),
        deal_summary=_load("deal_summary.json"),
        memo_provenance=_load("memo_provenance.json"),
        underwriting_provenance=_load("underwriting_provenance.json"),
        judgment_provenance=_load("judgment_provenance.json"),
        final_state=_final_state(),
    )
    assert isinstance(row, CRMRow)
    assert row.deal_slug == "project_essex"
    assert row.run_id == "run_002"
    assert row.status == "memo_ready"
    assert row.recommendation == "PROCEED"
    assert row.address == "123 Main St, Dallas TX 75201"
    # 100 + 140 = 240 cohort units
    assert row.units == 240
    assert row.asking_price == 28_000_000
    assert row.memo_path == "/abs/runs/deals/d/outputs/r/memo/memo.md"


def test_book_deal_field_name_mismatch_levered_em_to_equity_multiple(tmp_path: Path) -> None:
    """Engine emits metrics.equity_multiple.levered_em; CRM column is equity_multiple."""
    row = book_deal(
        crm_jsonl_path=crm_path(tmp_path),
        canonical_deal=_load("canonical_deal.json"),
        positioning=_load("positioning.json"),
        deal_summary=_load("deal_summary.json"),
        memo_provenance=_load("memo_provenance.json"),
        underwriting_provenance=_load("underwriting_provenance.json"),
        judgment_provenance=_load("judgment_provenance.json"),
        final_state=_final_state(),
    )
    assert row.equity_multiple == 1.78
    assert row.levered_irr == 0.152
    assert row.going_in_cap == 0.052


def test_book_deal_computes_ppu(tmp_path: Path) -> None:
    row = book_deal(
        crm_jsonl_path=crm_path(tmp_path),
        canonical_deal=_load("canonical_deal.json"),
        positioning=_load("positioning.json"),
        deal_summary=_load("deal_summary.json"),
        memo_provenance=_load("memo_provenance.json"),
        underwriting_provenance=_load("underwriting_provenance.json"),
        judgment_provenance=_load("judgment_provenance.json"),
        final_state=_final_state(),
    )
    # 28_000_000 / 240 = 116666.67 (round to cents for stability)
    assert row.ppu == pytest.approx(28_000_000 / 240)


def test_book_deal_extracts_vintage_from_year_built(tmp_path: Path) -> None:
    row = book_deal(
        crm_jsonl_path=crm_path(tmp_path),
        canonical_deal=_load("canonical_deal.json"),
        positioning=_load("positioning.json"),
        deal_summary=_load("deal_summary.json"),
        memo_provenance=_load("memo_provenance.json"),
        underwriting_provenance=_load("underwriting_provenance.json"),
        judgment_provenance=_load("judgment_provenance.json"),
        final_state=_final_state(),
    )
    assert row.vintage == 2005


def test_book_deal_sanity_flag_count_from_deal_summary(tmp_path: Path) -> None:
    row = book_deal(
        crm_jsonl_path=crm_path(tmp_path),
        canonical_deal=_load("canonical_deal.json"),
        positioning=_load("positioning.json"),
        deal_summary=_load("deal_summary.json"),
        memo_provenance=_load("memo_provenance.json"),
        underwriting_provenance=_load("underwriting_provenance.json"),
        judgment_provenance=_load("judgment_provenance.json"),
        final_state=_final_state(),
    )
    assert row.sanity_flag_count == 1


def test_book_deal_derives_visible_exit_cap_compression_risk(tmp_path: Path) -> None:
    summary = _load("deal_summary.json")
    summary["sanity_flags"] = []
    summary["metrics"]["yields"] = {
        "going_in_cap_rate": 0.0873,
        "exit_cap_rate": 0.06,
    }
    provenance = _load("underwriting_provenance.json")
    provenance["feasibility_sanity_flags"] = []

    row = book_deal(
        crm_jsonl_path=crm_path(tmp_path),
        canonical_deal=_load("canonical_deal.json"),
        positioning=_load("positioning.json"),
        deal_summary=summary,
        memo_provenance=_load("memo_provenance.json"),
        underwriting_provenance=provenance,
        judgment_provenance=_load("judgment_provenance.json"),
        final_state=_final_state(),
    )

    assert row.sanity_flag_count == 1


def test_book_deal_blocker_count_from_final_state(tmp_path: Path) -> None:
    """blocker_count = len(positioning.blockers) + lifecycle blockers per §2.6."""
    from plat_agent.lifecycle.state import BlockerItem
    state = _final_state(
        status="memo_ready_with_blockers",
        blockers=[
            BlockerItem(step="intake", id="missing_t12", description="x"),
            BlockerItem(step="comps", id="scraper_blocked", description="y"),
        ],
    )
    pos = _load("positioning.json")
    pos["blockers"] = [{"step": "judgment", "id": "low_conf", "description": "z"}]
    row = book_deal(
        crm_jsonl_path=crm_path(tmp_path),
        canonical_deal=_load("canonical_deal.json"),
        positioning=pos,
        deal_summary=_load("deal_summary.json"),
        memo_provenance=_load("memo_provenance.json"),
        underwriting_provenance=_load("underwriting_provenance.json"),
        judgment_provenance=_load("judgment_provenance.json"),
        final_state=state,
    )
    # 1 from positioning + 2 from final_state = 3
    assert row.blocker_count == 3


def test_book_deal_paths_from_provenance(tmp_path: Path) -> None:
    row = book_deal(
        crm_jsonl_path=crm_path(tmp_path),
        canonical_deal=_load("canonical_deal.json"),
        positioning=_load("positioning.json"),
        deal_summary=_load("deal_summary.json"),
        memo_provenance=_load("memo_provenance.json"),
        underwriting_provenance=_load("underwriting_provenance.json"),
        judgment_provenance=_load("judgment_provenance.json"),
        final_state=_final_state(),
    )
    assert row.workbook_path == "/abs/runs/deals/d/outputs/r/underwriting/deal_workbook.xlsm"
    assert row.thesis_path == "/abs/runs/deals/d/outputs/r/judgment/thesis.md"


def test_book_deal_appends_row_to_jsonl(tmp_path: Path) -> None:
    crm_file = crm_path(tmp_path)
    book_deal(
        crm_jsonl_path=crm_file,
        canonical_deal=_load("canonical_deal.json"),
        positioning=_load("positioning.json"),
        deal_summary=_load("deal_summary.json"),
        memo_provenance=_load("memo_provenance.json"),
        underwriting_provenance=_load("underwriting_provenance.json"),
        judgment_provenance=_load("judgment_provenance.json"),
        final_state=_final_state(),
    )
    assert crm_file.exists()
    lines = [ln for ln in crm_file.read_text().splitlines() if ln.strip()]
    assert len(lines) == 1


def test_book_deal_uses_finished_at_for_ts(tmp_path: Path) -> None:
    state = _final_state(finished_at=datetime(2026, 5, 5, 12, 0, tzinfo=timezone.utc))
    row = book_deal(
        crm_jsonl_path=crm_path(tmp_path),
        canonical_deal=_load("canonical_deal.json"),
        positioning=_load("positioning.json"),
        deal_summary=_load("deal_summary.json"),
        memo_provenance=_load("memo_provenance.json"),
        underwriting_provenance=_load("underwriting_provenance.json"),
        judgment_provenance=_load("judgment_provenance.json"),
        final_state=state,
    )
    assert row.ts == datetime(2026, 5, 5, 12, 0, tzinfo=timezone.utc)


def test_book_deal_judgment_mode_override_when_passthrough(tmp_path: Path) -> None:
    """Per §5.4 + §2.6: when state.judgment_mode != 'deterministic_v1' set
    judgment_mode_override; otherwise leave it null. State is source of truth
    (orchestrator persists it from the run_lifecycle judgment_mode arg)."""
    jp = _load("judgment_provenance.json")
    jp["judgment_engine"] = "passthrough"
    pos = _load("positioning.json")
    pos["recommendation"] = "NEEDS_DATA"  # passthrough forces NEEDS_DATA
    row = book_deal(
        crm_jsonl_path=crm_path(tmp_path),
        canonical_deal=_load("canonical_deal.json"),
        positioning=pos,
        deal_summary=_load("deal_summary.json"),
        memo_provenance=_load("memo_provenance.json"),
        underwriting_provenance=_load("underwriting_provenance.json"),
        judgment_provenance=jp,
        final_state=_final_state(
            status="memo_ready_with_blockers",
            judgment_mode="passthrough",
        ),
    )
    assert row.judgment_mode_override == "passthrough"


def test_book_deal_judgment_mode_override_null_for_v1(tmp_path: Path) -> None:
    row = book_deal(
        crm_jsonl_path=crm_path(tmp_path),
        canonical_deal=_load("canonical_deal.json"),
        positioning=_load("positioning.json"),
        deal_summary=_load("deal_summary.json"),
        memo_provenance=_load("memo_provenance.json"),
        underwriting_provenance=_load("underwriting_provenance.json"),
        judgment_provenance=_load("judgment_provenance.json"),
        final_state=_final_state(),
    )
    assert row.judgment_mode_override is None


def test_book_deal_tolerates_absent_optional_metrics(tmp_path: Path) -> None:
    """Per §2.6: levered_irr/em/cap may be null when underwriting failed."""
    row = book_deal(
        crm_jsonl_path=crm_path(tmp_path),
        canonical_deal=_load("canonical_deal.json"),
        positioning=_load("positioning.json"),
        deal_summary=None,
        memo_provenance=_load("memo_provenance.json"),
        underwriting_provenance=None,
        judgment_provenance=_load("judgment_provenance.json"),
        final_state=_final_state(status="failed_at_underwriting"),
    )
    assert row.levered_irr is None
    assert row.equity_multiple is None
    assert row.going_in_cap is None
    assert row.workbook_path is None
    assert row.sanity_flag_count == 0


def test_book_deal_projects_property_tax_calculation_only(
    tmp_path: Path,
) -> None:
    deal_summary = _load("deal_summary.json")
    deal_summary["property_tax_calculation"] = PROPERTY_TAX_CALCULATION

    row = book_deal(
        crm_jsonl_path=crm_path(tmp_path),
        canonical_deal=_load("canonical_deal.json"),
        positioning=_load("positioning.json"),
        deal_summary=deal_summary,
        memo_provenance=_load("memo_provenance.json"),
        underwriting_provenance=_load("underwriting_provenance.json"),
        judgment_provenance=_load("judgment_provenance.json"),
        final_state=_final_state(),
    )

    assert row.property_tax_millage_rate_mills == 25.31
    assert row.property_tax_rate_pct == pytest.approx(2.531)
    assert row.property_tax_assessment_ratio == 1.0
    assert row.property_tax_purchase_price_basis == 10_000_000.0
    assert row.property_tax_assessed_value_basis == 10_000_000.0
    assert row.property_tax_annual_ad_valorem_tax == 253_100.0
    assert row.property_tax_source == "county_tax_notice"
    assert (
        row.property_tax_source_locator
        == "raw_inputs/2026_tax_notice.pdf, page 2"
    )
    assert row.property_tax_analyst_override is False


def test_book_deal_blocked_path_has_no_property_tax_economics(
    tmp_path: Path,
) -> None:
    row = book_deal(
        crm_jsonl_path=crm_path(tmp_path),
        canonical_deal=None,
        positioning=None,
        deal_summary=None,
        memo_provenance=None,
        underwriting_provenance=None,
        judgment_provenance=None,
        final_state=_final_state(status="needs_analyst_input"),
    )

    assert row.recommendation == "NEEDS_DATA"
    assert row.property_tax_millage_rate_mills is None
    assert row.property_tax_rate_pct is None
    assert row.property_tax_assessment_ratio is None
    assert row.property_tax_purchase_price_basis is None
    assert row.property_tax_assessed_value_basis is None
    assert row.property_tax_annual_ad_valorem_tax is None
    assert row.property_tax_source is None
    assert row.property_tax_source_locator is None
    assert row.property_tax_analyst_override is None


def test_book_deal_ignores_property_tax_aliases_without_calculation(
    tmp_path: Path,
) -> None:
    deal_summary = {
        "property_tax_millage_rate": 99.0,
        "property_tax_rate_pct": 9.9,
        "trailing_property_tax": 999_999.0,
        "broker_property_tax": 888_888.0,
    }

    row = book_deal(
        crm_jsonl_path=crm_path(tmp_path),
        canonical_deal=_load("canonical_deal.json"),
        positioning=_load("positioning.json"),
        deal_summary=deal_summary,
        memo_provenance=_load("memo_provenance.json"),
        underwriting_provenance=_load("underwriting_provenance.json"),
        judgment_provenance=_load("judgment_provenance.json"),
        final_state=_final_state(),
    )

    assert row.property_tax_millage_rate_mills is None
    assert row.property_tax_rate_pct is None
    assert row.property_tax_annual_ad_valorem_tax is None
