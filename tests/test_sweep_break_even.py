"""Tests for find_break_even()."""

from unittest.mock import MagicMock

import pytest

from plat_agent.models import DealAnalysis, DealInputs, UnitMixEntry, UnitTypeResult
from plat_agent.sweep.break_even import find_break_even


# ---------------------------------------------------------------------------
# Fixtures (reuse shape from scenarios tests)
# ---------------------------------------------------------------------------

def _base_deal() -> dict:
    return {
        "metadata": {"deal_id": "D1"},
        "unit_cohorts": [{"cohort_id": "A1", "unit_count": 10, "initial_inplace_rent": 900.0, "sqft": 850}],
        "market_rent_curve": [{"cohort_id": "A1", "start_period": "2026-06", "end_period": "2027-05", "market_rent": 1100.0}],
        "exit_assumptions": {"exit_cap_rate": 0.055},
        "physical_vacancy_curve": [{"vacancy_rate": 0.05, "start_period": "2026-06"}],
        "opex_table": [{"category": "turnover", "growth_rate": 0.03}],
        "growth_assumptions": {"annual_growth_rate": 0.03},
        "renovation_programs": [
            {"program_id": "reno_A1", "target_cohort": "A1", "rent_premium_monthly": 200.0,
             "post_renovation_market_rent": 1100.0, "monthly_pace": 5},
        ],
    }


def _deal_inputs(**overrides) -> DealInputs:
    defaults = dict(
        property_id="PROP001", total_units=10,
        unit_mix=[UnitMixEntry(sqft=850, bedrooms=2, bathrooms=1, count=10,
                               current_monthly_rent=900, target_monthly_rent=1100)],
        year_built=1985, property_class="C", market="dallas",
        start_month="2026-06", monthly_pace=5,
        base_deal_inputs=_base_deal(),
    )
    defaults.update(overrides)
    return DealInputs(**defaults)


def _passing_analysis() -> DealAnalysis:
    return DealAnalysis(
        property_id="PROP001", total_units=10, ready_to_underwrite=True,
        unit_type_results=[
            UnitTypeResult(sqft=850, bedrooms=2, bathrooms=1, count=10,
                           cost_estimate_low=14000, cost_estimate_high=16800,
                           roi_pct=17.0, roi_passes=True,
                           renovation_program={"renovation_cost_per_unit": 16800},
                           roi_result={"roi_pct": 17.0, "clears_threshold": True}),
        ],
        total_renovation_cost_low=140000, total_renovation_cost_high=168000,
        exterior_capex_low=50000, exterior_capex_high=80000, risk_flags=[],
        renovation_programs=[{"program_id": "reno_A1", "target_cohort": "A1",
                              "rent_premium_monthly": 200.0,
                              "post_renovation_market_rent": 1100.0,
                              "monthly_pace": 5}],
        summary="passing",
    )


def _failing_analysis() -> DealAnalysis:
    a = _passing_analysis()
    a.ready_to_underwrite = False
    return a


def _metrics(levered_irr: float, min_dscr: float) -> dict:
    return {
        "status": "success",
        "irr": {"levered_irr": levered_irr},
        "equity_multiple": {"levered_em": 1.8},
        "dscr": {"minimum": min_dscr, "average": min_dscr + 0.1},
        "yields": {"going_in_cap_rate": 0.055},
    }


class TestBreakEvenValidation:
    def test_raises_when_base_deal_inputs_missing(self):
        inputs = _deal_inputs(base_deal_inputs=None)
        with pytest.raises(ValueError, match="base_deal_inputs is required"):
            find_break_even(inputs, axis="exit_cap", bounds=(0.04, 0.08),
                            analysis=_passing_analysis(), backend=MagicMock())

    def test_raises_for_unknown_axis(self):
        inputs = _deal_inputs()
        with pytest.raises(ValueError, match="Unknown axis"):
            find_break_even(inputs, axis="bogus", bounds=(0, 1),
                            analysis=_passing_analysis(), backend=MagicMock())

    def test_raises_when_bounds_not_ordered(self):
        inputs = _deal_inputs()
        with pytest.raises(ValueError, match="bounds must be"):
            find_break_even(inputs, axis="exit_cap", bounds=(0.08, 0.04),
                            analysis=_passing_analysis(), backend=MagicMock())

    def test_target_rent_requires_cohort_id(self):
        inputs = _deal_inputs()
        with pytest.raises(ValueError, match="cohort_id is required"):
            find_break_even(inputs, axis="target_rent", bounds=(900, 1400),
                            analysis=_passing_analysis(), backend=MagicMock())

    def test_target_rent_rejects_unknown_cohort(self):
        inputs = _deal_inputs()
        with pytest.raises(ValueError, match="cohort_id 'ZZZ'"):
            find_break_even(inputs, axis="target_rent", bounds=(900, 1400),
                            cohort_id="ZZZ",
                            analysis=_passing_analysis(), backend=MagicMock())


class TestBreakEvenROIShortCircuit:
    def test_returns_roi_gate_failed(self):
        inputs = _deal_inputs()
        backend = MagicMock()
        result = find_break_even(
            inputs, axis="exit_cap", bounds=(0.04, 0.08),
            analysis=_failing_analysis(), backend=backend,
        )
        assert result.status == "roi_gate_failed"
        backend.run_deal.assert_not_called()


# ---------------------------------------------------------------------------
# Single-objective bisection
# ---------------------------------------------------------------------------

class TestBreakEvenSingleObjective:
    def test_irr_bisection_converges(self):
        """Monotonic IRR in exit_cap. Break-even at exit_cap where IRR == 0.12.

        Model: IRR(ec) = 0.20 - 4*(ec - 0.05). So IRR(0.05)=0.20, IRR(0.07)=0.12,
        IRR(0.08)=0.08. Break-even exit_cap ≈ 0.07.
        """
        inputs = _deal_inputs()

        def run_deal(deal):
            ec = deal["exit_assumptions"]["exit_cap_rate"]
            irr = 0.20 - 4 * (ec - 0.05)
            return _metrics(levered_irr=irr, min_dscr=1.4)

        backend = MagicMock()
        backend.run_deal.side_effect = lambda deal: run_deal(deal)

        result = find_break_even(
            inputs, axis="exit_cap", bounds=(0.05, 0.09),
            objective="levered_irr", irr_threshold=0.12,
            tolerance=0.0005, max_iterations=20,
            analysis=_passing_analysis(), backend=backend,
        )
        assert result.status == "success"
        assert result.irr_breakpoint is not None
        assert abs(result.irr_breakpoint - 0.07) < 0.001
        assert result.breakpoint == result.irr_breakpoint
        # For single-objective, the other constraint's fields stay zero
        assert result.dscr_iterations == 0

    def test_dscr_bisection_converges(self):
        """Monotonic min_dscr in exit_cap. DSCR(ec) = 1.5 - 10*(ec - 0.05).
        DSCR(0.05)=1.5, DSCR(0.08)=1.2, DSCR(0.09)=1.1. Break-even at 0.08.
        """
        inputs = _deal_inputs()

        def run_deal(deal):
            ec = deal["exit_assumptions"]["exit_cap_rate"]
            dscr = 1.5 - 10 * (ec - 0.05)
            return _metrics(levered_irr=0.25, min_dscr=dscr)

        backend = MagicMock()
        backend.run_deal.side_effect = lambda deal: run_deal(deal)

        result = find_break_even(
            inputs, axis="exit_cap", bounds=(0.05, 0.10),
            objective="min_dscr", dscr_threshold=1.20,
            tolerance=0.0005, max_iterations=20,
            analysis=_passing_analysis(), backend=backend,
        )
        assert result.status == "success"
        assert abs(result.dscr_breakpoint - 0.08) < 0.001


# ---------------------------------------------------------------------------
# Compound objective (max of IRR and DSCR break-evens)
# ---------------------------------------------------------------------------

class TestBreakEvenCompound:
    def test_exit_cap_compound_binds_lower_breakpoint_when_axis_worsens_metrics(self):
        """Higher exit_cap worsens metrics, so the lower individual threshold is stricter.

        IRR breaks even at 0.07 and DSCR breaks even at 0.08. Because feasible
        exit caps are <= each threshold, the compound AND gate binds at 0.07.
        """
        inputs = _deal_inputs()

        def run_deal(deal):
            ec = deal["exit_assumptions"]["exit_cap_rate"]
            irr = 0.20 - 4 * (ec - 0.05)       # IRR=0.12 at ec=0.07
            dscr = 1.5 - 10 * (ec - 0.05)       # DSCR=1.20 at ec=0.08
            return _metrics(levered_irr=irr, min_dscr=dscr)

        backend = MagicMock()
        backend.run_deal.side_effect = lambda deal: run_deal(deal)

        result = find_break_even(
            inputs, axis="exit_cap", bounds=(0.05, 0.09),
            objective="compound", irr_threshold=0.12, dscr_threshold=1.20,
            tolerance=0.0005, max_iterations=20,
            analysis=_passing_analysis(), backend=backend,
        )
        assert result.status == "success"
        assert result.binding_constraint == "irr"
        assert abs(result.breakpoint - 0.07) < 0.001
        assert abs(result.irr_breakpoint - 0.07) < 0.001
        assert abs(result.dscr_breakpoint - 0.08) < 0.001
        assert result.breakpoint_metrics["dscr"]["minimum"] > 1.20

    def test_exit_cap_compound_binds_dscr_when_dscr_breakpoint_is_lower(self):
        """For exit_cap, DSCR binds when its threshold is the lower/more conservative one."""
        inputs = _deal_inputs()

        def run_deal(deal):
            ec = deal["exit_assumptions"]["exit_cap_rate"]
            irr = 0.20 - 2.67 * (ec - 0.05)    # IRR=0.12 at ec≈0.08
            dscr = 1.5 - 15 * (ec - 0.05)       # DSCR=1.20 at ec=0.07
            return _metrics(levered_irr=irr, min_dscr=dscr)

        backend = MagicMock()
        backend.run_deal.side_effect = lambda deal: run_deal(deal)

        result = find_break_even(
            inputs, axis="exit_cap", bounds=(0.05, 0.09),
            objective="compound", irr_threshold=0.12, dscr_threshold=1.20,
            tolerance=0.0005, max_iterations=20,
            analysis=_passing_analysis(), backend=backend,
        )
        assert result.status == "success"
        assert result.binding_constraint == "dscr"
        assert abs(result.breakpoint - 0.07) < 0.002
        assert abs(result.irr_breakpoint - 0.08) < 0.002
        assert abs(result.dscr_breakpoint - 0.07) < 0.001

    def test_target_rent_compound_binds_higher_breakpoint_when_axis_improves_metrics(self):
        """Higher target_rent improves metrics, so the higher individual threshold is stricter."""
        inputs = _deal_inputs()

        def run_deal(deal):
            target_rent = deal["renovation_programs"][0]["post_renovation_market_rent"]
            irr = 0.08 + 0.0002 * (target_rent - 900)     # IRR=0.12 at rent=1100
            dscr = 1.0 + 0.001 * (target_rent - 1000)     # DSCR=1.20 at rent=1200
            return _metrics(levered_irr=irr, min_dscr=dscr)

        backend = MagicMock()
        backend.run_deal.side_effect = lambda deal: run_deal(deal)

        result = find_break_even(
            inputs, axis="target_rent", bounds=(1000, 1300), cohort_id="A1",
            objective="compound", irr_threshold=0.12, dscr_threshold=1.20,
            tolerance=0.5, max_iterations=20,
            analysis=_passing_analysis(), backend=backend,
        )
        assert result.status == "success"
        assert result.binding_constraint == "dscr"
        assert abs(result.breakpoint - 1200) < 1
        assert abs(result.irr_breakpoint - 1100) < 1
        assert abs(result.dscr_breakpoint - 1200) < 1


# ---------------------------------------------------------------------------
# Not-bracketed and error paths
# ---------------------------------------------------------------------------

class TestBreakEvenNotBracketed:
    def test_returns_not_bracketed_when_threshold_above_both_endpoints(self):
        """IRR is 0.05 everywhere — can never reach 0.12 threshold."""
        inputs = _deal_inputs()
        backend = MagicMock()
        backend.run_deal.side_effect = lambda deal: _metrics(levered_irr=0.05, min_dscr=1.4)

        result = find_break_even(
            inputs, axis="exit_cap", bounds=(0.05, 0.09),
            objective="levered_irr", irr_threshold=0.12,
            analysis=_passing_analysis(), backend=backend,
        )
        assert result.status == "not_bracketed"

    def test_endpoint_metrics_recorded_on_not_bracketed(self):
        inputs = _deal_inputs()
        backend = MagicMock()
        backend.run_deal.side_effect = lambda deal: _metrics(levered_irr=0.05, min_dscr=1.4)
        result = find_break_even(
            inputs, axis="exit_cap", bounds=(0.05, 0.09),
            objective="levered_irr", irr_threshold=0.12,
            analysis=_passing_analysis(), backend=backend,
        )
        assert result.bracket_low.get("axis_value") == 0.05
        assert result.bracket_high.get("axis_value") == 0.09


class TestBreakEvenBracketError:
    def test_returns_bracket_error_when_low_endpoint_errors(self):
        inputs = _deal_inputs()
        backend = MagicMock()
        backend.run_deal.side_effect = [
            {"status": "error", "error": "engine blew up"},
            _metrics(0.20, 1.5),
        ]
        result = find_break_even(
            inputs, axis="exit_cap", bounds=(0.05, 0.09),
            objective="levered_irr",
            analysis=_passing_analysis(), backend=backend,
        )
        assert result.status == "bracket_error"
        assert "engine blew up" in result.summary

    def test_returns_bracket_error_when_mid_bisection_errors(self):
        """First two calls bracket OK, third (mid) errors."""
        inputs = _deal_inputs()
        backend = MagicMock()
        backend.run_deal.side_effect = [
            _metrics(0.20, 1.5),   # low endpoint
            _metrics(0.08, 1.1),   # high endpoint — brackets IRR threshold
            {"status": "error", "error": "mid crash"},
        ]
        result = find_break_even(
            inputs, axis="exit_cap", bounds=(0.05, 0.09),
            objective="levered_irr",
            analysis=_passing_analysis(), backend=backend,
        )
        assert result.status == "bracket_error"
        assert "mid crash" in result.summary


class TestBreakEvenMaxIterations:
    def test_stops_at_max_iterations(self):
        """Tolerance too tight to hit — ensure we bail at max_iterations."""
        inputs = _deal_inputs()

        def run_deal(deal):
            ec = deal["exit_assumptions"]["exit_cap_rate"]
            # Produces a clean bracket so bisection engages, but never converges within 1e-30
            irr = 0.20 - 4 * (ec - 0.05)
            return _metrics(levered_irr=irr, min_dscr=1.4)

        backend = MagicMock()
        backend.run_deal.side_effect = lambda deal: run_deal(deal)

        result = find_break_even(
            inputs, axis="exit_cap", bounds=(0.05, 0.09),
            objective="levered_irr", tolerance=1e-30, max_iterations=5,
            analysis=_passing_analysis(), backend=backend,
        )
        assert result.status == "success"
        assert result.irr_iterations == 5
