# tests/lifecycle/test_memo_tables.py
"""Renderers for memo subsections (broker-claim table, returns table, risks)."""

from __future__ import annotations

import pytest

from plat_agent.lifecycle.memo import BrokerClaim, render_broker_claims


def test_render_broker_claims_pct_field() -> None:
    positioning = {
        "rent_growth": {"data_derived": 0.030, "om_claimed": 0.040, "selected": 0.030,
                        "delta_flag": "broker_optimistic"},
    }
    rows = render_broker_claims(positioning)
    assert len(rows) == 1
    r = rows[0]
    assert isinstance(r, BrokerClaim)
    assert r.label == "Rent growth"
    assert r.broker_fmt == "4.0%"
    assert r.data_fmt == "3.0%"
    assert r.selected_fmt == "3.0%"
    assert "+33%" in r.delta_fmt or "+33.3%" in r.delta_fmt  # broker over data
    assert r.flag == "broker_optimistic"


def test_render_broker_claims_currency_field() -> None:
    positioning = {
        "capex_per_unit": {"data_derived": 18000, "om_claimed": 12000, "selected": 16000,
                           "delta_flag": "broker_underestimates"},
    }
    rows = render_broker_claims(positioning)
    assert rows[0].label == "Capex per unit"
    assert rows[0].broker_fmt == "$12,000"
    assert rows[0].data_fmt == "$18,000"
    assert rows[0].selected_fmt == "$16,000"
    assert rows[0].flag == "broker_underestimates"


def test_render_broker_claims_skips_fields_without_triples() -> None:
    positioning = {
        "positioning": {"value": "value_add", "confidence": 0.85},  # no triple
        "rent_growth": {"data_derived": 0.030, "om_claimed": 0.040, "selected": 0.030,
                        "delta_flag": "broker_optimistic"},
    }
    rows = render_broker_claims(positioning)
    assert {r.label for r in rows} == {"Rent growth"}


def test_render_broker_claims_handles_missing_om_claimed() -> None:
    """If broker didn't make a claim, show '—' for broker column and no flag."""
    positioning = {
        "exit_cap": {"data_derived": 0.060, "selected": 0.060},  # no om_claimed
    }
    rows = render_broker_claims(positioning)
    assert rows[0].broker_fmt == "—"
    assert rows[0].delta_fmt == "—"


def test_render_broker_claims_zero_data_derived_avoids_div_by_zero() -> None:
    positioning = {
        "capex_per_unit": {"data_derived": 0, "om_claimed": 5000, "selected": 5000,
                           "delta_flag": "broker_optimistic"},
    }
    rows = render_broker_claims(positioning)
    assert rows[0].delta_fmt in ("—", "n/a")


def test_render_broker_claims_orders_by_field_priority() -> None:
    """Stable order: rent_growth, capex_per_unit, exit_cap, renovation_pace."""
    positioning = {
        "exit_cap": {"data_derived": 0.06, "om_claimed": 0.055, "selected": 0.058,
                     "delta_flag": "broker_optimistic"},
        "rent_growth": {"data_derived": 0.03, "om_claimed": 0.04, "selected": 0.03,
                        "delta_flag": "broker_optimistic"},
        "capex_per_unit": {"data_derived": 18000, "om_claimed": 12000, "selected": 16000,
                           "delta_flag": "broker_underestimates"},
    }
    rows = render_broker_claims(positioning)
    labels = [r.label for r in rows]
    assert labels.index("Rent growth") < labels.index("Capex per unit") \
        < labels.index("Exit cap")


from plat_agent.lifecycle.memo import render_returns


def test_render_returns_full_metrics() -> None:
    metrics = {
        "levered_irr": 0.152,
        "levered_em": 1.78,
        "min_dscr": 1.28,
        "going_in_cap": 0.052,
        "exit_cap": 0.058,
    }
    out = render_returns(metrics)
    assert out["levered_irr_fmt"] == "15.2%"
    assert out["levered_em_fmt"] == "1.78x"
    assert out["min_dscr_fmt"] == "1.28x"
    assert out["going_in_cap_fmt"] == "5.2%"
    assert out["exit_cap_fmt"] == "5.8%"


def test_render_returns_draft_mode_all_dashes() -> None:
    out = render_returns({})
    assert all(v == "—" for v in out.values())


def test_render_returns_partial_metrics() -> None:
    out = render_returns({"levered_irr": 0.10, "min_dscr": 1.30})
    assert out["levered_irr_fmt"] == "10.0%"
    assert out["min_dscr_fmt"] == "1.30x"
    assert out["levered_em_fmt"] == "—"
    assert out["going_in_cap_fmt"] == "—"


from plat_agent.lifecycle.memo import render_risks
from plat_agent.lifecycle.state import BlockerItem


def test_render_risks_aggregates_flags_and_blockers() -> None:
    out = render_risks(
        sanity_flags=["exit_cap_below_going_in", "dscr_marginal"],
        uncleared_blockers=[
            BlockerItem(step="intake", id="missing_t12", description="No T12.", cleared=False),
        ],
    )
    assert out["risks"] == ["exit_cap_below_going_in", "dscr_marginal"]
    assert len(out["blockers"]) == 1
    assert out["blockers"][0].id == "missing_t12"


def test_render_risks_empty_when_clean() -> None:
    out = render_risks(sanity_flags=[], uncleared_blockers=[])
    assert out["risks"] == []
    assert out["blockers"] == []


def test_render_risks_dedupes_sanity_flags() -> None:
    out = render_risks(
        sanity_flags=["dscr_marginal", "dscr_marginal", "exit_cap_below_going_in"],
        uncleared_blockers=[],
    )
    # Order preserved; duplicates removed
    assert out["risks"] == ["dscr_marginal", "exit_cap_below_going_in"]
