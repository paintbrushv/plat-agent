"""TEST-001 interior capex is a labeled synthetic assumption."""

from __future__ import annotations

import importlib.util
import inspect
import sys
from decimal import Decimal
from pathlib import Path

import pytest

from plat_agent.dispatch.sibling import primary_checkout_root, underwriting_checkout
from plat_agent.lifecycle.synthetic_interior_scope import (
    SYNTHETIC_DEFERRED_ROOF,
    SYNTHETIC_INTERIOR_SCOPE,
    SYNTHETIC_INTERIOR_UNIT_COUNTS,
    YEAR_2_UNLEVERED_NOI,
    PURCHASE_PRICE,
    deferred_costmodel_root,
    interior_capex,
    interior_only_yield,
    interior_plus_synthetic_roof_yield,
    synthetic_roof_capex,
)

_MODULE = Path(inspect.getfile(interior_only_yield))
_FORBIDDEN_SOURCE = (
    "unit_type",
    "initial_inplace_rent",
    "estimate_from_deal",
    "resolve_property",
    "estimate_property",
    "DeferredMaintenance",
    "ExteriorScenario",
    "/home/",
    "plat-harness-worktrees",
)


def _minimal_deal_inputs() -> dict:
    root = underwriting_checkout()
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    spec = importlib.util.spec_from_file_location(
        "uw_conftest_for_interior_scope",
        root / "tests" / "conftest.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    grid = module.minimal_time_grid.__wrapped__()
    cohorts = module.minimal_unit_cohorts.__wrapped__()
    return module.minimal_deal_inputs.__wrapped__(grid, cohorts)


def test_test001_fixture_cannot_feed_the_interior_estimator() -> None:
    deal = _minimal_deal_inputs()
    assert deal["metadata"]["deal_id"] == "TEST-001"
    assert "property" not in deal
    assert "renovation_programs" not in deal
    assert "capex_schedule" not in deal
    assert deal["exit_assumptions"]["exit_cap_rate"] == 0.055
    for cohort in deal["unit_cohorts"]:
        assert "avg_sqft" not in cohort
        assert "avg_bedrooms" not in cohort
        assert "avg_bathrooms" not in cohort
        assert "current_avg_rent" not in cohort


def test_synthetic_scope_is_labeled_and_limited_to_estimator_fields() -> None:
    scope = SYNTHETIC_INTERIOR_SCOPE
    assert scope["assumption_kind"] == "synthetic_assumption"
    assert scope["extracted_fact"] is False
    assert set(scope) == {
        "assumption_kind",
        "extracted_fact",
        "note",
        "scope_level",
        "finish_tier",
        "rent_premium_monthly",
        "schedule",
        "cohorts",
    }
    assert scope["scope_level"] == "standard_value_add"
    assert scope["finish_tier"] == "basic"
    assert scope["rent_premium_monthly"] == 50
    assert scope["schedule"] == {
        "start_month": "2026-07",
        "monthly_pace": 5,
        "downtime_days": 21,
    }
    assert SYNTHETIC_INTERIOR_UNIT_COUNTS == (50, 50)
    for cohort in scope["cohorts"]:
        assert set(cohort) == {
            "avg_sqft",
            "avg_bedrooms",
            "avg_bathrooms",
            "current_avg_rent",
        }


def test_scope_module_does_not_alias_fixture_fields_or_other_estimators() -> None:
    source = _MODULE.read_text(encoding="utf-8")
    for token in _FORBIDDEN_SOURCE:
        assert token not in source


def test_missing_interior_field_is_rejected() -> None:
    scope = {
        **SYNTHETIC_INTERIOR_SCOPE,
        "cohorts": [
            {key: value for key, value in cohort.items() if key != "current_avg_rent"}
            for cohort in SYNTHETIC_INTERIOR_SCOPE["cohorts"]
        ],
    }
    with pytest.raises(KeyError, match="current_avg_rent"):
        interior_capex(scope, unit_counts=SYNTHETIC_INTERIOR_UNIT_COUNTS)


def test_interior_only_yield_uses_explicit_capex_and_presents_no_bid() -> None:
    issued = interior_only_yield()
    capex, per_unit_high = interior_capex(
        SYNTHETIC_INTERIOR_SCOPE,
        unit_counts=SYNTHETIC_INTERIOR_UNIT_COUNTS,
    )

    assert issued.capex == capex
    assert issued.capex > 0
    assert issued.per_unit_high == per_unit_high
    assert all(amount > 0 for amount in issued.per_unit_high)
    assert issued.label == "interior_only"
    assert issued.bid is None
    assert issued.withheld is True
    assert issued.withhold_reason == "The 5.5% exit cap still withholds this deal."
    assert YEAR_2_UNLEVERED_NOI == Decimal("1200004.80")
    assert PURCHASE_PRICE == Decimal("13500000")

    try:
        from plat_harness.underwriting_direction import year_2_unlevered_yield_on_cost
    except ImportError:
        assert issued.year_2_unlevered_yield_on_cost is None
        return

    assert issued.year_2_unlevered_yield_on_cost == year_2_unlevered_yield_on_cost(
        YEAR_2_UNLEVERED_NOI,
        PURCHASE_PRICE,
        issued.capex,
    )
    direct = YEAR_2_UNLEVERED_NOI / (PURCHASE_PRICE + issued.capex)
    assert issued.year_2_unlevered_yield_on_cost == direct


def test_synthetic_roof_is_an_explicit_assumption() -> None:
    roof = SYNTHETIC_DEFERRED_ROOF
    assert roof["assumption_kind"] == "synthetic_assumption"
    assert roof["extracted_fact"] is False
    assert roof["item"] == "roof_full_replacement"
    assert roof["quantity"] == 100
    assert "property" not in roof
    source = inspect.getsource(synthetic_roof_capex)
    assert "unit_count" not in source
    assert "SYNTHETIC_INTERIOR_UNIT_COUNTS" not in source
    loaded = deferred_costmodel_root() / "src" / "plat_costmodel" / "deferred_estimator.py"
    primary = (primary_checkout_root().parent / "plat-costmodel").resolve()
    assert loaded.is_file()
    assert primary not in loaded.resolve().parents


def test_interior_plus_synthetic_roof_adds_roof_capex_and_presents_no_bid() -> None:
    issued = interior_plus_synthetic_roof_yield()
    interior_total, _per_unit = interior_capex(
        SYNTHETIC_INTERIOR_SCOPE,
        unit_counts=SYNTHETIC_INTERIOR_UNIT_COUNTS,
    )
    roof = synthetic_roof_capex()

    assert roof == Decimal("400000.0")
    assert issued.roof_capex == roof
    assert issued.interior_capex == interior_total
    assert issued.capex == interior_total + roof
    assert issued.label == "interior plus this synthetic roof"
    assert issued.bid is None
    assert issued.withheld is True

    try:
        from plat_harness.underwriting_direction import year_2_unlevered_yield_on_cost
    except ImportError:
        assert issued.year_2_unlevered_yield_on_cost is None
        return

    assert issued.year_2_unlevered_yield_on_cost == year_2_unlevered_yield_on_cost(
        YEAR_2_UNLEVERED_NOI,
        PURCHASE_PRICE,
        issued.capex,
    )
