"""Tests for schema_mapper — canonical renovation_programs generation."""

import pytest

from plat_agent.models import DealInputs, UnitMixEntry, UnitTypeResult
from plat_agent.schema_mapper import (
    build_renovation_programs,
    merge_renovation_programs,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _inputs(**kwargs) -> DealInputs:
    defaults = dict(
        property_id="PROP001",
        total_units=30,
        start_month="2026-06",
        monthly_pace=5,
        unit_mix=[
            UnitMixEntry(sqft=850, bedrooms=2, bathrooms=1, count=20,
                         current_monthly_rent=900, target_monthly_rent=1100),
            UnitMixEntry(sqft=600, bedrooms=1, bathrooms=1, count=10,
                         current_monthly_rent=750, target_monthly_rent=950),
        ],
    )
    defaults.update(kwargs)
    return DealInputs(**defaults)


def _passing_result(sqft=850, bedrooms=2, bathrooms=1, count=20) -> UnitTypeResult:
    return UnitTypeResult(
        sqft=sqft, bedrooms=bedrooms, bathrooms=bathrooms, count=count,
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


def _failing_result(sqft=600, bedrooms=1, bathrooms=1, count=10) -> UnitTypeResult:
    return UnitTypeResult(
        sqft=sqft, bedrooms=bedrooms, bathrooms=bathrooms, count=count,
        cost_estimate_low=10000, cost_estimate_high=14000,
        roi_pct=8.5, roi_passes=False,
        renovation_program=None,
        roi_result={"roi_pct": 8.5, "clears_threshold": False},
    )


# ---------------------------------------------------------------------------
# build_renovation_programs
# ---------------------------------------------------------------------------

class TestBuildRenovationPrograms:
    def test_one_passing_type(self):
        inputs = _inputs()
        results = [_passing_result(), _failing_result()]
        programs = build_renovation_programs(inputs, results)
        assert len(programs) == 1

    def test_all_passing(self):
        inputs = _inputs()
        results = [_passing_result(), _passing_result(sqft=600, bedrooms=1, count=10)]
        programs = build_renovation_programs(inputs, results)
        assert len(programs) == 2

    def test_none_passing(self):
        inputs = _inputs()
        results = [_failing_result(sqft=850, count=20), _failing_result()]
        programs = build_renovation_programs(inputs, results)
        assert len(programs) == 0

    def test_canonical_fields_present(self):
        """Every program must have all required canonical schema fields."""
        inputs = _inputs()
        results = [_passing_result(), _failing_result()]
        programs = build_renovation_programs(inputs, results)
        required = {
            "program_id", "program_name", "target_cohort", "output_cohort",
            "renovation_cost_per_unit", "rent_premium_monthly",
            "downtime_days", "strategy", "start_month", "monthly_pace",
        }
        for prog in programs:
            assert required <= prog.keys(), f"Missing fields: {required - prog.keys()}"

    def test_strategy_on_turnover(self):
        inputs = _inputs()
        results = [_passing_result(), _failing_result()]
        programs = build_renovation_programs(inputs, results, strategy="on_turnover")
        assert all(p["strategy"] == "on_turnover" for p in programs)

    def test_strategy_proactive(self):
        inputs = _inputs()
        results = [_passing_result(), _failing_result()]
        programs = build_renovation_programs(inputs, results, strategy="proactive")
        assert all(p["strategy"] == "proactive" for p in programs)

    def test_invalid_strategy_raises(self):
        inputs = _inputs()
        results = [_passing_result()]
        with pytest.raises(ValueError, match="strategy"):
            build_renovation_programs(inputs, results, strategy="invalid")

    def test_cost_and_rent_from_bridge(self):
        inputs = _inputs()
        results = [_passing_result(), _failing_result()]
        programs = build_renovation_programs(inputs, results)
        assert programs[0]["renovation_cost_per_unit"] == 16800
        assert programs[0]["rent_premium_monthly"] == 200

    def test_max_units_set_to_count(self):
        inputs = _inputs()
        results = [_passing_result(count=20), _failing_result()]
        programs = build_renovation_programs(inputs, results)
        assert programs[0]["max_units"] == 20

    def test_output_cohort_id_suffixed(self):
        inputs = _inputs()
        results = [_passing_result(), _failing_result()]
        programs = build_renovation_programs(inputs, results)
        assert programs[0]["output_cohort"].endswith("_postreno")

    def test_program_id_unique_per_type(self):
        inputs = _inputs()
        results = [_passing_result(), _passing_result(sqft=600, bedrooms=1, count=10)]
        programs = build_renovation_programs(inputs, results)
        ids = [p["program_id"] for p in programs]
        assert len(set(ids)) == len(ids)

    def test_program_name_descriptive(self):
        inputs = _inputs()
        results = [_passing_result(), _failing_result()]
        programs = build_renovation_programs(inputs, results)
        assert "2BR" in programs[0]["program_name"]
        assert "850" in programs[0]["program_name"]


# ---------------------------------------------------------------------------
# merge_renovation_programs
# ---------------------------------------------------------------------------

class TestMergeRenovationPrograms:
    def test_merge_into_empty(self):
        base = {"unit_cohorts": []}
        programs = [{"program_id": "reno_1", "target_cohort": "c1"}]
        result = merge_renovation_programs(base, programs)
        assert len(result["renovation_programs"]) == 1

    def test_merge_appends_to_existing(self):
        base = {
            "renovation_programs": [
                {"program_id": "existing_1", "target_cohort": "c0"},
            ],
        }
        programs = [{"program_id": "reno_1", "target_cohort": "c1"}]
        result = merge_renovation_programs(base, programs)
        assert len(result["renovation_programs"]) == 2

    def test_no_duplicate_ids(self):
        base = {
            "renovation_programs": [
                {"program_id": "reno_1", "target_cohort": "c1"},
            ],
        }
        programs = [{"program_id": "reno_1", "target_cohort": "c1"}]
        result = merge_renovation_programs(base, programs)
        assert len(result["renovation_programs"]) == 1

    def test_base_returned(self):
        base = {"metadata": {"deal_id": "D1"}}
        result = merge_renovation_programs(base, [])
        assert result["metadata"]["deal_id"] == "D1"
