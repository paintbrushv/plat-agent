"""Smoke tests for splice_renovation_programs_into_canonical.

The splice helper is a thin compose around ``build_renovation_programs``
and ``merge_renovation_programs``. These tests assert (1) the unified
function actually calls both underlying functions and produces the
expected canonical shape, and (2) it raises on cohort namespace
collisions before any mutation occurs.

Wave 2 Task 2.4 — federated splicer contract.
"""

from __future__ import annotations

import pytest

from plat_agent.models import DealInputs, UnitMixEntry, UnitTypeResult
from plat_agent.schema_mapper import splice_renovation_programs_into_canonical


def _inputs() -> DealInputs:
    return DealInputs(
        property_id="PROP001",
        total_units=20,
        start_month="2026-06",
        monthly_pace=5,
        unit_mix=[
            UnitMixEntry(
                sqft=850, bedrooms=2, bathrooms=1, count=20,
                current_monthly_rent=900, target_monthly_rent=1100,
            ),
        ],
    )


def _passing_result() -> UnitTypeResult:
    return UnitTypeResult(
        sqft=850, bedrooms=2, bathrooms=1, count=20,
        cost_estimate_low=12000, cost_estimate_high=16800,
        roi_pct=17.1, roi_passes=True,
        renovation_program={
            "renovation_cost_per_unit": 16800,
            "rent_premium_monthly": 200,
            "downtime_days": 21,
            "strategy": "renovation",
            "start_month": "2026-06",
            "monthly_pace": 5,
        },
        roi_result={"roi_pct": 17.1, "clears_threshold": True},
    )


def test_splice_calls_build_plus_merge_and_returns_canonical_shape():
    canonical = {
        "unit_cohorts": [
            {"cohort_id": "2br_inplace", "bedrooms": 2, "bathrooms": 1,
             "sqft": 850, "unit_count": 20},
        ],
        "renovation_programs": [],
    }
    inputs = _inputs()
    results = [_passing_result()]

    out = splice_renovation_programs_into_canonical(canonical, inputs, results)

    # Composes build + merge: result has a renovation_programs list with
    # one program for the passing cohort.
    assert "renovation_programs" in out
    assert len(out["renovation_programs"]) == 1
    program = out["renovation_programs"][0]
    assert program["target_cohort"] == "2br_inplace"
    assert program["output_cohort"].startswith("2br_inplace_postreno")
    assert program["strategy"] == "on_turnover"
    # Original canonical NOT mutated.
    assert canonical["renovation_programs"] == []


def test_splice_raises_on_cohort_id_collision():
    """If a federation-emitted output_cohort would collide with an
    existing unit_cohort.cohort_id, the splice MUST raise before any
    state changes."""
    canonical = {
        "unit_cohorts": [
            {"cohort_id": "2br_inplace", "bedrooms": 2, "bathrooms": 1,
             "sqft": 850, "unit_count": 20},
            # Pre-existing rent-roll-derived "renovated" subtotal AND
            # all four ladder candidates from _make_output_cohort, so
            # the build step is forced to raise.
            {"cohort_id": "2br_inplace_postreno", "bedrooms": 2,
             "bathrooms": 1, "sqft": 850, "unit_count": 0},
            {"cohort_id": "2br_inplace_postreno_inplace", "bedrooms": 2,
             "bathrooms": 1, "sqft": 850, "unit_count": 0},
            {"cohort_id": "2br_inplace_postreno_v2", "bedrooms": 2,
             "bathrooms": 1, "sqft": 850, "unit_count": 0},
            {"cohort_id": "2br_inplace_postreno_v3", "bedrooms": 2,
             "bathrooms": 1, "sqft": 850, "unit_count": 0},
        ],
        "renovation_programs": [],
    }
    inputs = _inputs()
    results = [_passing_result()]

    with pytest.raises(ValueError):
        splice_renovation_programs_into_canonical(canonical, inputs, results)
