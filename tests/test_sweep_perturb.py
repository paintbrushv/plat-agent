"""Tests for sweep/perturb.py — deal mutation helpers."""

import copy

import pytest

from plat_agent.sweep.perturb import (
    AXIS_MUTATORS,
    apply_exit_cap,
    apply_monthly_pace,
    apply_preset,
    apply_target_rent,
    get_preset_family,
)


def _base_deal() -> dict:
    """Minimal merged deal with the fields perturbers touch."""
    return {
        "metadata": {"deal_id": "D1"},
        "unit_cohorts": [
            {"cohort_id": "A1", "unit_count": 10, "initial_inplace_rent": 800.0, "sqft": 650},
            {"cohort_id": "B1", "unit_count": 5, "initial_inplace_rent": 950.0, "sqft": 850},
        ],
        "market_rent_curve": [
            {"cohort_id": "A1", "start_period": "2026-05", "end_period": "2027-04", "market_rent": 900.0},
            {"cohort_id": "A1", "start_period": "2027-05", "end_period": "2028-04", "market_rent": 945.0},
            {"cohort_id": "B1", "start_period": "2026-05", "end_period": "2027-04", "market_rent": 1050.0},
        ],
        "exit_assumptions": {"exit_cap_rate": 0.055},
        "physical_vacancy_curve": [{"vacancy_rate": 0.05, "start_period": "2026-05"}],
        "opex_table": [{"category": "turnover", "growth_rate": 0.03}],
        "growth_assumptions": {"annual_growth_rate": 0.03},
        "renovation_programs": [
            {
                "program_id": "reno_A1_yr1",
                "target_cohort": "A1",
                "rent_premium_monthly": 200.0,
                "post_renovation_market_rent": 1000.0,
                "monthly_pace": 3,
            },
            {
                "program_id": "reno_A1_yr2",
                "target_cohort": "A1",
                "rent_premium_monthly": 200.0,
                "post_renovation_market_rent": 1000.0,
                "monthly_pace": 3,
            },
            {
                "program_id": "reno_B1_yr1",
                "target_cohort": "B1",
                "rent_premium_monthly": 250.0,
                "post_renovation_market_rent": 1200.0,
                "monthly_pace": 2,
            },
        ],
    }


class TestGetPresetFamily:
    def test_value_add(self):
        family = get_preset_family("value_add")
        assert "bull" in family
        assert "bear" in family

    def test_stabilized(self):
        family = get_preset_family("stabilized")
        assert "bull" in family
        assert "bear" in family

    def test_invalid_raises(self):
        with pytest.raises(ValueError, match="Unknown preset family"):
            get_preset_family("not_a_family")


class TestApplyPreset:
    def test_returns_new_dict_does_not_mutate_original(self):
        deal = _base_deal()
        original = copy.deepcopy(deal)
        preset_family = get_preset_family("value_add")
        result = apply_preset(deal, preset_family["bull"])
        assert deal == original  # original untouched
        assert result is not deal

    def test_bull_increases_rent_growth(self):
        deal = _base_deal()
        preset = get_preset_family("value_add")["bull"]
        result = apply_preset(deal, preset)
        assert result["growth_assumptions"]["annual_growth_rate"] > deal["growth_assumptions"]["annual_growth_rate"]

    def test_bear_increases_exit_cap(self):
        deal = _base_deal()
        preset = get_preset_family("value_add")["bear"]
        result = apply_preset(deal, preset)
        assert result["exit_assumptions"]["exit_cap_rate"] > deal["exit_assumptions"]["exit_cap_rate"]


class TestApplyExitCap:
    def test_sets_exit_cap_rate(self):
        deal = _base_deal()
        result = apply_exit_cap(deal, 0.07)
        assert result["exit_assumptions"]["exit_cap_rate"] == 0.07

    def test_does_not_mutate_original(self):
        deal = _base_deal()
        apply_exit_cap(deal, 0.07)
        assert deal["exit_assumptions"]["exit_cap_rate"] == 0.055


class TestApplyMonthlyPace:
    def test_sets_pace_on_every_renovation_program(self):
        deal = _base_deal()
        result = apply_monthly_pace(deal, 5)
        for prog in result["renovation_programs"]:
            assert prog["monthly_pace"] == 5

    def test_does_not_mutate_original(self):
        deal = _base_deal()
        apply_monthly_pace(deal, 5)
        for prog in deal["renovation_programs"]:
            assert prog["monthly_pace"] in (2, 3)  # originals


class TestApplyTargetRent:
    def test_updates_single_matching_program(self):
        # Cardinality contract: at most one renovation_program may target a
        # given cohort. Use B1 (single program) for the success path; A1 has
        # two programs targeting it and now correctly raises (see
        # test_apply_target_rent_multiple_matches_raises in
        # test_perturb_cardinality.py).
        deal = _base_deal()
        result = apply_target_rent(deal, cohort_id="B1", target_rent=1300.0)
        b1_programs = [p for p in result["renovation_programs"] if p["target_cohort"] == "B1"]
        assert len(b1_programs) == 1
        assert b1_programs[0]["rent_premium_monthly"] == 350.0  # 1300 - 950
        assert b1_programs[0]["post_renovation_market_rent"] == 1300.0

    def test_leaves_other_cohorts_untouched(self):
        deal = _base_deal()
        result = apply_target_rent(deal, cohort_id="B1", target_rent=1300.0)
        a1_programs = [p for p in result["renovation_programs"] if p["target_cohort"] == "A1"]
        assert len(a1_programs) == 2
        for prog in a1_programs:
            assert prog["rent_premium_monthly"] == 200.0  # unchanged
            assert prog["post_renovation_market_rent"] == 1000.0  # unchanged

    def test_raises_for_unknown_cohort(self):
        deal = _base_deal()
        with pytest.raises(ValueError, match="cohort_id 'XYZ' not found"):
            apply_target_rent(deal, cohort_id="XYZ", target_rent=1050.0)

    def test_does_not_mutate_original(self):
        deal = _base_deal()
        apply_target_rent(deal, cohort_id="B1", target_rent=1300.0)
        b1_programs = [p for p in deal["renovation_programs"] if p["target_cohort"] == "B1"]
        for prog in b1_programs:
            assert prog["rent_premium_monthly"] == 250.0  # original


class TestAxisMutatorsDispatch:
    def test_target_rent_in_dispatch(self):
        assert "target_rent" in AXIS_MUTATORS

    def test_exit_cap_in_dispatch(self):
        assert "exit_cap" in AXIS_MUTATORS

    def test_monthly_pace_in_dispatch(self):
        assert "monthly_pace" in AXIS_MUTATORS

    def test_exit_cap_dispatch_callable(self):
        deal = _base_deal()
        result = AXIS_MUTATORS["exit_cap"](deal, value=0.07)
        assert result["exit_assumptions"]["exit_cap_rate"] == 0.07
