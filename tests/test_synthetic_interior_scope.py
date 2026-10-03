"""TEST-001 interior capex is a labeled synthetic assumption."""

from __future__ import annotations

import hashlib
import importlib.metadata
import inspect
import json
from dataclasses import replace
from decimal import Decimal
from importlib.resources import files
from pathlib import Path

import pytest

from plat_agent.lifecycle.versioned_adapters import COSTMODEL_V1, UNDERWRITING_V4
from plat_agent.lifecycle.synthetic_interior_scope import (
    SYNTHETIC_DEFERRED_ROOF,
    SYNTHETIC_INTERIOR_SCOPE,
    SYNTHETIC_INTERIOR_UNIT_COUNTS,
    PURCHASE_PRICE,
    UNDERWRITING_SHA,
    interior_capex,
    interior_only_yield,
    interior_plus_synthetic_roof_yield,
    record_test001_thesis,
    synthetic_roof_capex,
    load_test001_underwriting_metrics,
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


def _require_reviewed_packages() -> None:
    for contract in (COSTMODEL_V1, UNDERWRITING_V4):
        try:
            importlib.metadata.version(contract.distribution)
        except importlib.metadata.PackageNotFoundError:
            pytest.skip(f"{contract.distribution} is not installed")
        contract.verify()


def test_test001_fixture_cannot_feed_the_interior_estimator() -> None:
    fixture = files("plat_agent.lifecycle").joinpath("fixtures/test001_underwriting_inputs.json")
    deal = json.loads(fixture.read_text(encoding="utf-8"))
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


def test_saved_inputs_match_pinned_public_underwriting_fixture() -> None:
    _require_reviewed_packages()
    fixture = files("plat_agent.lifecycle").joinpath("fixtures/test001_underwriting_inputs.json")
    inputs = json.loads(fixture.read_text(encoding="utf-8"))
    assert inputs["metadata"]["deal_id"] == "TEST-001"
    assert UNDERWRITING_SHA == "b42764be7d0bf278272ff29ce36e0c98e2c10f5f"
    snapshot = json.loads(
        (Path(__file__).parent / "fixtures/test001_public_thesis_v5.json").read_text(encoding="utf-8")
    )
    assert hashlib.sha256(fixture.read_bytes()).hexdigest() == snapshot["source"]["saved_input_sha256"]


def test_stale_underwriting_package_is_refused() -> None:
    _require_reviewed_packages()
    stale = replace(UNDERWRITING_V4, content_sha256="0" * 64)
    with pytest.raises(RuntimeError, match="contents differ"):
        stale.verify()


def test_missing_underwriting_package_is_refused(monkeypatch) -> None:
    def missing(_name):
        raise importlib.metadata.PackageNotFoundError("absent")

    monkeypatch.setattr(importlib.metadata, "version", missing)
    with pytest.raises(RuntimeError, match="requires an installed package"):
        load_test001_underwriting_metrics()


def test_wrong_costmodel_version_is_refused(monkeypatch) -> None:
    original = importlib.metadata.version
    monkeypatch.setattr(
        importlib.metadata, "version",
        lambda name: "0.0.1" if name == COSTMODEL_V1.distribution else original(name),
    )
    with pytest.raises(RuntimeError, match="requires version 0.1.0"):
        interior_capex(SYNTHETIC_INTERIOR_SCOPE, unit_counts=SYNTHETIC_INTERIOR_UNIT_COUNTS)


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
    _require_reviewed_packages()
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
    assert issued.year_2_unlevered_noi == Decimal("1039354.8")
    assert PURCHASE_PRICE == Decimal("13500000")

    try:
        from plat_harness.underwriting_direction import year_2_unlevered_yield_on_cost
    except ImportError:
        assert issued.year_2_unlevered_yield_on_cost is None
        return

    assert issued.year_2_unlevered_yield_on_cost == year_2_unlevered_yield_on_cost(
        issued.year_2_unlevered_noi,
        PURCHASE_PRICE,
        issued.capex,
    )
    direct = issued.year_2_unlevered_noi / (PURCHASE_PRICE + issued.capex)
    assert issued.year_2_unlevered_yield_on_cost == direct


def test_synthetic_roof_is_an_explicit_assumption() -> None:
    _require_reviewed_packages()
    roof = SYNTHETIC_DEFERRED_ROOF
    assert roof["assumption_kind"] == "synthetic_assumption"
    assert roof["extracted_fact"] is False
    assert roof["item"] == "roof_full_replacement"
    assert roof["quantity"] == 100
    assert "property" not in roof
    source = inspect.getsource(synthetic_roof_capex)
    assert "unit_count" not in source
    assert "SYNTHETIC_INTERIOR_UNIT_COUNTS" not in source
    loaded = COSTMODEL_V1.verify() / "deferred_estimator.py"
    assert loaded.is_file()
    assert COSTMODEL_V1.source_sha == "518142ecb8771e52fcc9985237fe1a6f97a76168"


def test_interior_plus_synthetic_roof_adds_roof_capex_and_presents_no_bid() -> None:
    _require_reviewed_packages()
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
        issued.year_2_unlevered_noi,
        PURCHASE_PRICE,
        issued.capex,
    )


def test_engine_metric_records_a_new_withheld_thesis() -> None:
    _require_reviewed_packages()
    metrics = load_test001_underwriting_metrics()
    assert metrics["noi"]["year_2_unlevered_noi"] == 1039354.8
    assert metrics["noi"]["year_1_noi"] == 1050154.8
    assert metrics["yields"]["going_in_cap_rate"] == 0.0778
    issued, record = record_test001_thesis()
    assert issued["present_as_bid"] is False
    assert issued["bid"] is None
    assert issued["breaches"] == [
        {"field": "exit_cap", "value": "0.055", "band": "[0.06, 0.12]"}
    ]
    assert record.thesis.year_2_unlevered_noi == Decimal("1039354.8")
    assert record.thesis.capex == Decimal("1758150")
    assert record.thesis.year_2_unlevered_yield_on_cost == (
        Decimal("1039354.8") / Decimal("15258150")
    )
    assert record.thesis.present_as_bid is False
    assert record.operations_actual_noi is None
    snapshot = json.loads(
        (Path(__file__).parent / "fixtures/test001_public_thesis_v5.json").read_text(encoding="utf-8")
    )
    assert snapshot["source"]["underwriting_sha"] == UNDERWRITING_SHA
    assert snapshot["original_thesis"] == {
        "purchase_price": format(record.thesis.purchase_price, "f"),
        "year_2_unlevered_noi": format(record.thesis.year_2_unlevered_noi, "f"),
        "capex": format(record.thesis.capex, "f"),
        "year_2_unlevered_yield_on_cost": format(record.thesis.year_2_unlevered_yield_on_cost, "f"),
        "present_as_bid": record.thesis.present_as_bid,
        "operations_actual_noi": record.operations_actual_noi,
    }
    assert snapshot["numeric_reasonability"] == {
        "present_as_bid": issued["present_as_bid"],
        "bid": issued["bid"],
        "breaches": issued["breaches"],
    }
