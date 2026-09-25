# tests/lifecycle/test_crm_row.py
import json
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from plat_agent.lifecycle.crm import CRMRow
from plat_agent.lifecycle.state import RecommendationEnum

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


def test_crm_row_required_fields_only() -> None:
    """Required field subset per spec §2.6 must construct without error."""
    row = CRMRow(
        deal_slug="project_essex",
        run_id="run_002",
        ts=datetime(2026, 5, 5, 16, 30, tzinfo=timezone.utc),
        status="memo_ready",
        recommendation="PROCEED",
        memo_path="/abs/path/memo.md",
        address="123 Main St, Dallas TX",
        units=240,
        asking_price=28_000_000,
    )
    assert row.deal_slug == "project_essex"
    # Optional fields default-null or 0
    assert row.levered_irr is None
    assert row.equity_multiple is None
    assert row.going_in_cap is None
    assert row.ppu is None
    assert row.vintage is None
    assert row.blocker_count == 0
    assert row.sanity_flag_count == 0
    assert row.recommendation_confidence is None
    assert row.workbook_path is None
    assert row.thesis_path is None
    assert row.judgment_mode_override is None


def test_crm_row_full_fields() -> None:
    row = CRMRow(
        deal_slug="project_essex",
        run_id="run_002",
        ts=datetime(2026, 5, 5, 16, 30, tzinfo=timezone.utc),
        status="memo_ready",
        recommendation="PROCEED",
        memo_path="/abs/memo.md",
        address="...",
        units=240,
        asking_price=28_000_000,
        levered_irr=0.152,
        equity_multiple=1.78,
        going_in_cap=0.052,
        ppu=116666.67,
        vintage=2005,
        blocker_count=0,
        sanity_flag_count=1,
        recommendation_confidence=0.78,
        workbook_path="/abs/workbook.xlsm",
        thesis_path="/abs/thesis.md",
    )
    assert row.levered_irr == 0.152
    assert row.recommendation_confidence == 0.78


def test_crm_row_exposes_typed_property_tax_fields() -> None:
    row = CRMRow(
        deal_slug="project_essex",
        run_id="run_002",
        ts=datetime(2026, 5, 5, 16, 30, tzinfo=timezone.utc),
        status="memo_ready",
        recommendation="PROCEED",
        memo_path="/abs/memo.md",
        address="...",
        units=240,
        asking_price=28_000_000,
        property_tax_millage_rate_mills=PROPERTY_TAX_CALCULATION[
            "millage_rate_mills"
        ],
        property_tax_rate_pct=PROPERTY_TAX_CALCULATION["decimal_tax_rate"] * 100,
        property_tax_assessment_ratio=PROPERTY_TAX_CALCULATION[
            "assessment_ratio"
        ],
        property_tax_purchase_price_basis=PROPERTY_TAX_CALCULATION[
            "purchase_price_basis"
        ],
        property_tax_assessed_value_basis=PROPERTY_TAX_CALCULATION[
            "assessed_value_basis"
        ],
        property_tax_annual_ad_valorem_tax=PROPERTY_TAX_CALCULATION[
            "annual_ad_valorem_tax"
        ],
        property_tax_source=PROPERTY_TAX_CALCULATION["source"],
        property_tax_source_locator=PROPERTY_TAX_CALCULATION["source_locator"],
        property_tax_analyst_override=PROPERTY_TAX_CALCULATION[
            "analyst_override"
        ],
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


def test_crm_row_recommendation_enum_values() -> None:
    """All three recommendation enum values per §2.3 must validate."""
    for rec in ("PROCEED", "DECLINE", "NEEDS_DATA"):
        row = CRMRow(
            deal_slug="d", run_id="r",
            ts=datetime.now(timezone.utc),
            status="memo_ready",
            recommendation=rec,
            memo_path="/m", address="x", units=1, asking_price=1,
        )
        assert row.recommendation == rec


def test_crm_row_invalid_recommendation_rejected() -> None:
    with pytest.raises(ValidationError):
        CRMRow(
            deal_slug="d", run_id="r",
            ts=datetime.now(timezone.utc),
            status="memo_ready",
            recommendation="MAYBE",  # not in enum
            memo_path="/m", address="x", units=1, asking_price=1,
        )


def test_crm_row_required_field_missing_rejected() -> None:
    with pytest.raises(ValidationError):
        CRMRow(
            run_id="r",  # deal_slug missing
            ts=datetime.now(timezone.utc),
            status="memo_ready",
            recommendation="PROCEED",
            memo_path="/m", address="x", units=1, asking_price=1,
        )


def test_crm_row_jsonl_round_trip() -> None:
    """A CRMRow must serialize to one-line JSON (the JSONL contract)."""
    row = CRMRow(
        deal_slug="d", run_id="r",
        ts=datetime(2026, 5, 5, 16, 30, tzinfo=timezone.utc),
        status="memo_ready",
        recommendation="PROCEED",
        memo_path="/m", address="x", units=240, asking_price=28_000_000,
    )
    line = row.model_dump_json()
    assert "\n" not in line
    restored = CRMRow.model_validate_json(line)
    assert restored.deal_slug == "d"
    assert restored.units == 240


def test_crm_row_judgment_mode_override_only_when_nondefault() -> None:
    """Per spec §2.6: judgment_mode_override is null in the default V1 path
    and only populated when judgment_mode != 'deterministic_v1' (e.g.,
    'passthrough' for the §5.4 numerical regression path)."""
    row = CRMRow(
        deal_slug="d", run_id="r",
        ts=datetime.now(timezone.utc),
        status="memo_ready",
        recommendation="NEEDS_DATA",
        memo_path="/m", address="x", units=1, asking_price=1,
        judgment_mode_override="passthrough",
    )
    assert row.judgment_mode_override == "passthrough"
