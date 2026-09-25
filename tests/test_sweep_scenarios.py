"""Tests for run_scenarios()."""

from unittest.mock import MagicMock

import pytest

from plat_agent.models import DealAnalysis, DealInputs, UnitMixEntry, UnitTypeResult
from plat_agent.sweep.scenarios import run_scenarios


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _deal_inputs(**overrides) -> DealInputs:
    base = _merged_base_deal()
    defaults = dict(
        property_id="PROP001",
        total_units=10,
        unit_mix=[
            UnitMixEntry(sqft=850, bedrooms=2, bathrooms=1, count=10,
                         current_monthly_rent=900, target_monthly_rent=1100),
        ],
        year_built=1985,
        property_class="C",
        market="dallas",
        start_month="2026-06",
        monthly_pace=5,
        base_deal_inputs=base,
    )
    defaults.update(overrides)
    return DealInputs(**defaults)


def _merged_base_deal() -> dict:
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
        exterior_capex_low=50000, exterior_capex_high=80000,
        risk_flags=[],
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


def _metrics_feasible(levered_irr: float = 0.15, min_dscr: float = 1.25) -> dict:
    return {
        "status": "success",
        "irr": {"levered_irr": levered_irr},
        "equity_multiple": {"levered_em": 1.85},
        "dscr": {"minimum": min_dscr, "average": min_dscr + 0.2},
        "yields": {"going_in_cap_rate": 0.055},
    }


def _mock_backend(results_per_call: list[dict]) -> MagicMock:
    backend = MagicMock()
    backend.run_deal.side_effect = iter(results_per_call)
    return backend


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------

class TestRunScenariosInputValidation:
    def test_raises_when_base_deal_inputs_missing(self):
        inputs = _deal_inputs(base_deal_inputs=None)
        with pytest.raises(ValueError, match="base_deal_inputs is required"):
            run_scenarios(inputs, analysis=_passing_analysis(), backend=MagicMock())

    def test_raises_for_unknown_preset(self):
        inputs = _deal_inputs()
        with pytest.raises(ValueError, match="Unknown preset family"):
            run_scenarios(inputs, preset="bogus", analysis=_passing_analysis(), backend=MagicMock())


# ---------------------------------------------------------------------------
# ROI gate short-circuit
# ---------------------------------------------------------------------------

class TestRunScenariosROIShortCircuit:
    def test_returns_roi_gate_failed_status(self):
        inputs = _deal_inputs()
        backend = MagicMock()
        result = run_scenarios(inputs, analysis=_failing_analysis(), backend=backend)
        assert result.status == "roi_gate_failed"

    def test_no_backend_calls_when_roi_fails(self):
        inputs = _deal_inputs()
        backend = MagicMock()
        run_scenarios(inputs, analysis=_failing_analysis(), backend=backend)
        backend.run_deal.assert_not_called()

    def test_empty_scenarios_list_when_roi_fails(self):
        inputs = _deal_inputs()
        backend = MagicMock()
        result = run_scenarios(inputs, analysis=_failing_analysis(), backend=backend)
        assert result.scenarios == []


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

class TestRunScenariosHappyPath:
    def test_three_scenarios_base_bull_bear(self):
        inputs = _deal_inputs()
        backend = _mock_backend([_metrics_feasible(), _metrics_feasible(0.18), _metrics_feasible(0.08, 1.05)])
        result = run_scenarios(inputs, analysis=_passing_analysis(), backend=backend)
        names = [s.name for s in result.scenarios]
        assert names == ["Base", "Bull", "Bear"]

    def test_one_backend_call_per_scenario(self):
        inputs = _deal_inputs()
        backend = _mock_backend([_metrics_feasible()] * 3)
        run_scenarios(inputs, analysis=_passing_analysis(), backend=backend)
        assert backend.run_deal.call_count == 3

    def test_feasible_flag_reflects_compound_gate(self):
        inputs = _deal_inputs()
        backend = _mock_backend([
            _metrics_feasible(0.15, 1.25),  # Base: pass
            _metrics_feasible(0.18, 1.35),  # Bull: pass
            _metrics_feasible(0.08, 1.05),  # Bear: fail both
        ])
        result = run_scenarios(inputs, analysis=_passing_analysis(), backend=backend)
        assert result.scenarios[0].feasible is True   # Base
        assert result.scenarios[1].feasible is True   # Bull
        assert result.scenarios[2].feasible is False  # Bear

    def test_base_scenario_has_empty_overrides(self):
        inputs = _deal_inputs()
        backend = _mock_backend([_metrics_feasible()] * 3)
        result = run_scenarios(inputs, analysis=_passing_analysis(), backend=backend)
        assert result.scenarios[0].name == "Base"
        assert result.scenarios[0].overrides == {}

    def test_bull_bear_have_populated_overrides(self):
        inputs = _deal_inputs()
        backend = _mock_backend([_metrics_feasible()] * 3)
        result = run_scenarios(inputs, analysis=_passing_analysis(), backend=backend)
        assert result.scenarios[1].overrides  # non-empty
        assert result.scenarios[2].overrides

    def test_status_success(self):
        inputs = _deal_inputs()
        backend = _mock_backend([_metrics_feasible()] * 3)
        result = run_scenarios(inputs, analysis=_passing_analysis(), backend=backend)
        assert result.status == "success"

    def test_summary_contains_all_scenario_names(self):
        inputs = _deal_inputs()
        backend = _mock_backend([_metrics_feasible()] * 3)
        result = run_scenarios(inputs, analysis=_passing_analysis(), backend=backend)
        for name in ("Base", "Bull", "Bear"):
            assert name in result.summary


# ---------------------------------------------------------------------------
# Per-scenario error handling
# ---------------------------------------------------------------------------

class TestRunScenariosPartialFailure:
    def test_one_scenario_error_does_not_abort(self):
        inputs = _deal_inputs()
        backend = _mock_backend([
            _metrics_feasible(),
            {"status": "error", "error": "engine crashed"},
            _metrics_feasible(0.08),
        ])
        result = run_scenarios(inputs, analysis=_passing_analysis(), backend=backend)
        assert len(result.scenarios) == 3
        assert result.scenarios[0].status == "success"
        assert result.scenarios[1].status == "error"
        assert result.scenarios[1].error == "engine crashed"
        assert result.scenarios[1].feasible is False
        assert result.scenarios[2].status == "success"


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------

class _SleepyBackend:
    """Backend whose run_deal sleeps for `delay` seconds and tags the metrics
    with the deal's market_rent_curve[0].market_rent so we can assert each
    job got its own perturbed deal in the expected position."""

    def __init__(self, delay: float = 0.0):
        self.delay = delay
        self.call_count = 0

    def run_deal(self, deal_inputs: dict) -> dict:
        import time
        self.call_count += 1
        if self.delay:
            time.sleep(self.delay)
        rent = deal_inputs["market_rent_curve"][0]["market_rent"]
        return {
            "status": "success",
            "irr": {"levered_irr": 0.15},
            "equity_multiple": {"levered_em": 1.85},
            "dscr": {"minimum": 1.25, "average": 1.45},
            "yields": {"going_in_cap_rate": 0.055},
            "_marker_rent": rent,
        }


class TestRunScenariosConcurrency:
    def test_concurrency_lt_one_raises(self):
        inputs = _deal_inputs()
        with pytest.raises(ValueError, match="concurrency must be >=1"):
            run_scenarios(inputs, analysis=_passing_analysis(),
                          backend=_SleepyBackend(), concurrency=0)

    def test_parallel_produces_same_count(self):
        inputs = _deal_inputs()
        backend = _SleepyBackend()
        result = run_scenarios(inputs, analysis=_passing_analysis(),
                               backend=backend, concurrency=3)
        assert len(result.scenarios) == 3
        assert backend.call_count == 3

    def test_parallel_preserves_scenario_order(self):
        """Result order must equal input order (Base, Bull, Bear) regardless
        of which thread completes first."""
        inputs = _deal_inputs()
        sequential = run_scenarios(inputs, analysis=_passing_analysis(),
                                   backend=_SleepyBackend(), concurrency=1)
        parallel = run_scenarios(inputs, analysis=_passing_analysis(),
                                 backend=_SleepyBackend(), concurrency=3)
        assert [s.name for s in sequential.scenarios] == [s.name for s in parallel.scenarios]

    def test_parallel_reduces_wall_time(self):
        """Concurrency=3 with three 0.2s scenarios should finish closer to
        0.2s than 0.6s. Loose threshold (0.4s) avoids CI flakiness."""
        import time
        inputs = _deal_inputs()
        backend = _SleepyBackend(delay=0.2)
        t0 = time.monotonic()
        result = run_scenarios(inputs, analysis=_passing_analysis(),
                               backend=backend, concurrency=3)
        elapsed = time.monotonic() - t0
        assert len(result.scenarios) == 3
        assert elapsed < 0.4, f"parallel sweep took {elapsed:.2f}s, expected <0.4s"

    def test_concurrency_one_is_sequential(self):
        """Regression: concurrency=1 must use the sequential code path."""
        import time
        inputs = _deal_inputs()
        backend = _SleepyBackend(delay=0.1)
        t0 = time.monotonic()
        run_scenarios(inputs, analysis=_passing_analysis(),
                      backend=backend, concurrency=1)
        elapsed = time.monotonic() - t0
        # 3 scenarios * 0.1s sleep = ~0.3s; allow generous slack
        assert elapsed >= 0.25, f"sequential sweep took {elapsed:.2f}s, expected ~0.3s"
