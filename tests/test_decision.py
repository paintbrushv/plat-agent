"""Tests for plat_agent.decision.build_decision_summary."""

import pytest

from plat_agent.decision import build_decision_summary
from plat_agent.models import UnitTypeResult


def _passing_unit(**overrides) -> UnitTypeResult:
    base = dict(
        sqft=850, bedrooms=2, bathrooms=1, count=30,
        cost_estimate_low=14000, cost_estimate_high=16800,
        roi_pct=17.1, roi_passes=True,
        renovation_program={"x": 1},
        roi_result={},
        monthly_rent_lift=200, annual_rent_lift=2400,
        roi_gap_pp=2.1, simple_payback_months=84.0,
        cost_reduction_needed_to_pass=None, rent_increase_needed_to_pass=None,
    )
    base.update(overrides)
    return UnitTypeResult(**base)


def _failing_unit(**overrides) -> UnitTypeResult:
    base = dict(
        sqft=650, bedrooms=1, bathrooms=1, count=20,
        cost_estimate_low=14000, cost_estimate_high=16800,
        roi_pct=10.7, roi_passes=False,
        renovation_program=None,
        roi_result={},
        monthly_rent_lift=150, annual_rent_lift=1800,
        roi_gap_pp=-4.3, simple_payback_months=112.0,
        cost_reduction_needed_to_pass=4800,
        rent_increase_needed_to_pass=60,
    )
    base.update(overrides)
    return UnitTypeResult(**base)


class TestHeadline:
    def test_roi_failure_headline(self):
        s = build_decision_summary(
            ready_to_underwrite=False,
            underwriting_feasible=None,
            unit_type_results=[_passing_unit(), _failing_unit()],
            risk_flags=[], sanity_flags=[],
        )
        assert "Not ready" in s["headline"]
        assert "1 of 2" in s["headline"]

    def test_clean_pass_headline(self):
        s = build_decision_summary(
            ready_to_underwrite=True,
            underwriting_feasible=True,
            unit_type_results=[_passing_unit()],
            risk_flags=[], sanity_flags=[],
        )
        assert "ready to advance" in s["headline"].lower()

    def test_underwriting_feasibility_failure_headline(self):
        s = build_decision_summary(
            ready_to_underwrite=True,
            underwriting_feasible=False,
            unit_type_results=[_passing_unit()],
            risk_flags=[], sanity_flags=[],
        )
        assert "feasibility" in s["headline"].lower()

    def test_sanity_error_headline(self):
        s = build_decision_summary(
            ready_to_underwrite=True,
            underwriting_feasible=True,
            unit_type_results=[_passing_unit()],
            risk_flags=[],
            sanity_flags=[{"severity": "error", "metric": "min_dscr", "message": "low DSCR"}],
        )
        assert "sanity error" in s["headline"].lower()

    def test_sanity_warning_only_headline(self):
        s = build_decision_summary(
            ready_to_underwrite=True,
            underwriting_feasible=True,
            unit_type_results=[_passing_unit()],
            risk_flags=[],
            sanity_flags=[{"severity": "warning", "metric": "going_in_cap_rate", "message": "low cap"}],
        )
        assert "warning" in s["headline"].lower()


class TestBindingConstraints:
    def test_lists_each_failing_unit_type(self):
        s = build_decision_summary(
            ready_to_underwrite=False,
            underwriting_feasible=None,
            unit_type_results=[_passing_unit(), _failing_unit(bedrooms=1)],
            risk_flags=[], sanity_flags=[],
        )
        # Only the failing 1BR is a constraint; 2BR passes
        constraints = " | ".join(s["binding_constraints"])
        assert "1BR" in constraints
        assert "2BR" not in constraints

    def test_includes_sanity_flags(self):
        s = build_decision_summary(
            ready_to_underwrite=True,
            underwriting_feasible=True,
            unit_type_results=[_passing_unit()],
            risk_flags=[],
            sanity_flags=[
                {"severity": "error", "metric": "min_dscr", "message": "DSCR 0.58x is below 1.20x"},
            ],
        )
        joined = " | ".join(s["binding_constraints"])
        assert "[ERROR]" in joined
        assert "DSCR" in joined

    def test_includes_risk_flags(self):
        s = build_decision_summary(
            ready_to_underwrite=True,
            underwriting_feasible=True,
            unit_type_results=[_passing_unit()],
            risk_flags=[{"flag_type": "galvanized_pipe", "message": "Pre-1990 plumbing risk"}],
            sanity_flags=[],
        )
        joined = " | ".join(s["binding_constraints"])
        assert "Pre-1990" in joined

    def test_empty_when_clean_deal(self):
        s = build_decision_summary(
            ready_to_underwrite=True,
            underwriting_feasible=True,
            unit_type_results=[_passing_unit()],
            risk_flags=[], sanity_flags=[],
        )
        assert s["binding_constraints"] == []


class TestRecommendedChanges:
    def test_path_to_pass_emitted_when_roi_fails(self):
        s = build_decision_summary(
            ready_to_underwrite=False,
            underwriting_feasible=None,
            unit_type_results=[_failing_unit(rent_increase_needed_to_pass=85, cost_reduction_needed_to_pass=4800)],
            risk_flags=[], sanity_flags=[],
        )
        joined = " | ".join(s["recommended_changes"])
        assert "raise target rent by $85" in joined
        assert "cut renovation cost by $4,800" in joined

    def test_no_changes_when_clean_deal(self):
        s = build_decision_summary(
            ready_to_underwrite=True,
            underwriting_feasible=True,
            unit_type_results=[_passing_unit()],
            risk_flags=[], sanity_flags=[],
        )
        assert s["recommended_changes"] == []

    def test_sanity_remediation_per_metric(self):
        s = build_decision_summary(
            ready_to_underwrite=True,
            underwriting_feasible=True,
            unit_type_results=[_passing_unit()],
            risk_flags=[],
            sanity_flags=[
                {"severity": "error", "metric": "going_in_cap_rate", "message": "low cap"},
                {"severity": "error", "metric": "min_dscr", "message": "low DSCR"},
            ],
        )
        joined = " | ".join(s["recommended_changes"])
        assert "NOI" in joined  # cap rate remediation mentions NOI
        assert "DSCR" in joined

    def test_skips_path_when_deltas_missing(self):
        # If both rent_increase_needed and cost_reduction_needed are None,
        # no path-to-pass entries should be emitted (no recommendation we
        # can make confidently).
        u = _failing_unit(rent_increase_needed_to_pass=None, cost_reduction_needed_to_pass=None)
        s = build_decision_summary(
            ready_to_underwrite=False,
            underwriting_feasible=None,
            unit_type_results=[u],
            risk_flags=[], sanity_flags=[],
        )
        assert s["recommended_changes"] == []


class TestSummaryShape:
    def test_returns_three_keys(self):
        s = build_decision_summary(
            ready_to_underwrite=True,
            underwriting_feasible=True,
            unit_type_results=[_passing_unit()],
            risk_flags=[], sanity_flags=[],
        )
        assert set(s) == {"headline", "binding_constraints", "recommended_changes"}
