# tests/lifecycle/test_crm_extraction_edge_cases.py
"""Edge cases for the CRM extraction map (spec §2.6).

Focuses on engine-side field name mismatches and null tolerance — the bugs
most likely to surface late in integration.
"""
from datetime import datetime, timezone
from pathlib import Path

import pytest

from plat_agent.lifecycle.crm import book_deal, crm_path
from plat_agent.lifecycle.state import LifecycleState


def _final_state(**overrides) -> LifecycleState:
    base = dict(
        deal_slug="d", run_id="r",
        status="memo_ready",
        steps_completed=["memo"],
        blockers=[],
        finished_at=datetime(2026, 5, 5, 16, 30, tzinfo=timezone.utc),
    )
    base.update(overrides)
    return LifecycleState(**base)


def _canonical(units_per_cohort: list[int] = [240], year_built: int = 2005,
               address: str = "x", price: float | None = 1_000_000,
               deal_id: str = "Demo Deal IPA OM") -> dict:
    purchase_assumptions = (
        {"purchase_price": price} if price is not None else None
    )
    return {
        "metadata": {"address": address, "year_built": year_built, "deal_id": deal_id},
        "unit_cohorts": [
            {"cohort_id": f"c{i}", "unit_count": u}
            for i, u in enumerate(units_per_cohort)
        ],
        "purchase_assumptions": purchase_assumptions,
    }


def _positioning(rec: str = "PROCEED", conf: float | None = 0.78,
                 blockers: list | None = None) -> dict:
    return {
        "positioning": {"value": "value_add", "confidence": 0.85},
        "leverage": {"ltv": 0.65, "rate": 0.0575, "amort_years": 30,
                     "io_months": 12, "source": "v1_hardcoded"},
        "engine_inputs_relative": "judgment/engine_inputs.json",
        "recommendation": rec,
        "recommendation_confidence": conf,
        "blockers": blockers or [],
    }


def _deal_summary(levered_em: float = 1.78, going_in_cap_rate: float = 0.052,
                  irr: float = 0.152, sanity_flags: list | None = None) -> dict:
    return {
        "metrics": {
            "irr": {"levered_irr": irr},
            "equity_multiple": {"levered_em": levered_em},
            "yields": {"going_in_cap_rate": going_in_cap_rate},
            "dscr": {"min_dscr": 1.32},
        },
        "sanity_flags": sanity_flags or [],
    }


def test_engine_levered_em_maps_to_crm_equity_multiple(tmp_path: Path) -> None:
    """Critical mismatch: engine emits 'levered_em', CRM column is 'equity_multiple'."""
    row = book_deal(
        crm_jsonl_path=crm_path(tmp_path),
        canonical_deal=_canonical(),
        positioning=_positioning(),
        deal_summary=_deal_summary(levered_em=2.10),
        memo_provenance={"memo_path": "/m"},
        underwriting_provenance={},
        judgment_provenance={},
        final_state=_final_state(),
    )
    assert row.equity_multiple == 2.10


def test_engine_going_in_cap_rate_maps_to_crm_going_in_cap(tmp_path: Path) -> None:
    """Engine emits 'going_in_cap_rate'; CRM column drops the '_rate' suffix."""
    row = book_deal(
        crm_jsonl_path=crm_path(tmp_path),
        canonical_deal=_canonical(),
        positioning=_positioning(),
        deal_summary=_deal_summary(going_in_cap_rate=0.045),
        memo_provenance={"memo_path": "/m"},
        underwriting_provenance={},
        judgment_provenance={},
        final_state=_final_state(),
    )
    assert row.going_in_cap == 0.045


def test_engine_year_built_maps_to_crm_vintage(tmp_path: Path) -> None:
    """canonical metadata.year_built → CRM vintage (renamed)."""
    row = book_deal(
        crm_jsonl_path=crm_path(tmp_path),
        canonical_deal=_canonical(year_built=1998),
        positioning=_positioning(),
        deal_summary=_deal_summary(),
        memo_provenance={"memo_path": "/m"},
        underwriting_provenance={},
        judgment_provenance={},
        final_state=_final_state(),
    )
    assert row.vintage == 1998


def test_units_sums_across_cohorts(tmp_path: Path) -> None:
    """units is sum(unit_cohorts[].unit_count), not a top-level field."""
    row = book_deal(
        crm_jsonl_path=crm_path(tmp_path),
        canonical_deal=_canonical(units_per_cohort=[100, 80, 60, 40]),
        positioning=_positioning(),
        deal_summary=_deal_summary(),
        memo_provenance={"memo_path": "/m"},
        underwriting_provenance={},
        judgment_provenance={},
        final_state=_final_state(),
    )
    assert row.units == 280


def test_ppu_computed_from_price_and_units(tmp_path: Path) -> None:
    row = book_deal(
        crm_jsonl_path=crm_path(tmp_path),
        canonical_deal=_canonical(units_per_cohort=[200], price=20_000_000),
        positioning=_positioning(),
        deal_summary=_deal_summary(),
        memo_provenance={"memo_path": "/m"},
        underwriting_provenance={},
        judgment_provenance={},
        final_state=_final_state(),
    )
    assert row.ppu == pytest.approx(100_000)


def test_crm_prefers_priced_engine_inputs_over_intake_canonical(tmp_path: Path) -> None:
    """Intake canonical can be pre-pricing; CRM should use final engine inputs."""
    row = book_deal(
        crm_jsonl_path=crm_path(tmp_path),
        canonical_deal=_canonical(units_per_cohort=[10], price=None, deal_id="Old OM"),
        engine_inputs=_canonical(
            units_per_cohort=[20],
            price=2_000_000,
            deal_id="Casa Nube IPA OM",
        ),
        positioning=_positioning(),
        deal_summary=_deal_summary(),
        memo_provenance={"memo_path": "/m"},
        underwriting_provenance={},
        judgment_provenance={},
        final_state=_final_state(),
    )
    assert row.asking_price == 2_000_000
    assert row.units == 20
    assert row.ppu == pytest.approx(100_000)
    assert row.property_name == "Casa Nube"


def test_sanity_flag_count_from_array_length(tmp_path: Path) -> None:
    row = book_deal(
        crm_jsonl_path=crm_path(tmp_path),
        canonical_deal=_canonical(),
        positioning=_positioning(),
        deal_summary=_deal_summary(sanity_flags=["a", "b", "c"]),
        memo_provenance={"memo_path": "/m"},
        underwriting_provenance={},
        judgment_provenance={},
        final_state=_final_state(),
    )
    assert row.sanity_flag_count == 3


def test_min_dscr_is_not_a_crm_column(tmp_path: Path) -> None:
    """Sanity check: min_dscr is in deal_summary but NOT a CRM column.
    It feeds the recommendation derivation only."""
    row = book_deal(
        crm_jsonl_path=crm_path(tmp_path),
        canonical_deal=_canonical(),
        positioning=_positioning(),
        deal_summary=_deal_summary(),
        memo_provenance={"memo_path": "/m"},
        underwriting_provenance={},
        judgment_provenance={},
        final_state=_final_state(),
    )
    assert not hasattr(row, "min_dscr")


def test_null_recommendation_confidence_round_trips(tmp_path: Path) -> None:
    """Per §2.6: recommendation_confidence may be null."""
    row = book_deal(
        crm_jsonl_path=crm_path(tmp_path),
        canonical_deal=_canonical(),
        positioning=_positioning(conf=None),
        deal_summary=_deal_summary(),
        memo_provenance={"memo_path": "/m"},
        underwriting_provenance={},
        judgment_provenance={},
        final_state=_final_state(),
    )
    assert row.recommendation_confidence is None


def test_zero_units_does_not_divide_by_zero(tmp_path: Path) -> None:
    """Edge: empty unit_cohorts → units==0 → ppu must be None (not crash)."""
    row = book_deal(
        crm_jsonl_path=crm_path(tmp_path),
        canonical_deal={
            "metadata": {"address": "x", "year_built": 2000},
            "unit_cohorts": [],
            "purchase_assumptions": {"purchase_price": 1_000_000},
        },
        positioning=_positioning(),
        deal_summary=_deal_summary(),
        memo_provenance={"memo_path": "/m"},
        underwriting_provenance={},
        judgment_provenance={},
        final_state=_final_state(),
    )
    assert row.units == 0
    assert row.ppu is None


def test_memo_path_missing_defaults_to_empty_string(tmp_path: Path) -> None:
    """V1.1 graceful degradation (Demo Annex): memo provenance may be absent or
    missing memo_path when MemoStep itself failed. Per spec §2.6, the row
    is still written — memo_path falls back to "" (the schema's required-
    field contract is satisfied without raising)."""
    row = book_deal(
        crm_jsonl_path=crm_path(tmp_path),
        canonical_deal=_canonical(),
        positioning=_positioning(),
        deal_summary=_deal_summary(),
        memo_provenance={},  # no memo_path
        underwriting_provenance={},
        judgment_provenance={},
        final_state=_final_state(),
    )
    assert row.memo_path == ""
