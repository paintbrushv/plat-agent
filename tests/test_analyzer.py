"""Tests for analyze_deal() orchestration with mocked clients."""

import pytest
from unittest.mock import MagicMock

from plat_agent.analyzer import analyze_deal, _slice_for_unit_type
from plat_agent.models import DealAnalysis, DealInputs, UnitMixEntry


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _deal(unit_mix=None, **kwargs) -> DealInputs:
    defaults = dict(
        property_id="PROP001",
        total_units=10,
        year_built=1985,
        property_class="C",
        market="dallas",
        start_month="2026-06",
        monthly_pace=5,
    )
    defaults.update(kwargs)
    if unit_mix is None:
        unit_mix = [
            UnitMixEntry(sqft=850, bedrooms=2, bathrooms=1, count=10,
                         current_monthly_rent=900, target_monthly_rent=1100),
        ]
    return DealInputs(unit_mix=unit_mix, **defaults)


def _mock_client(prop_estimate: dict, bridge_results: list[dict]) -> MagicMock:
    """Build a client mock that returns canned responses."""
    client = MagicMock()
    call_seq = iter(bridge_results)

    def side_effect(tool_name, arguments):
        if tool_name == "estimate_property_from_model":
            return prop_estimate
        if tool_name == "prepare_renovation_program_tool":
            return next(call_seq)
        raise ValueError(f"Unexpected tool call: {tool_name}")

    client.call_tool.side_effect = side_effect
    return client


def _prop_estimate(n_units: int = 10, sqft: float = 850) -> dict:
    unit_est = {
        "unit_id": "",
        "unit_sqft": sqft,
        "bedrooms": 2,
        "bathrooms": 1,
        "scope_level": "standard_value_add",
        "finish_tier": "basic",
        "size_category": "medium",
        "line_items": [],
        "subtotal_low": 12500,
        "subtotal_high": 15000,
        "contingency_pct": 12,
        "contingency_low": 0,
        "contingency_high": 0,
        "total_low": 14000,
        "total_high": 16800,
        "risk_flags": [],
        "year_built": 1985,
        "property_class": "C",
        "market": "dallas",
    }
    return {
        "property_id": "PROP001",
        "total_units": n_units,
        "units_needing_work": n_units,
        "unit_estimates": [unit_est] * n_units,
        "exterior_capex_low": 50000,
        "exterior_capex_high": 80000,
        "total_renovation_low": 14000 * n_units + 50000,
        "total_renovation_high": 16800 * n_units + 80000,
        "per_unit_average_low": 14000,
        "per_unit_average_high": 16800,
        "risk_flags": [{"message": "Pre-1990: check galvanized pipe", "flag_type": "galvanized_pipe"}],
    }


def _passing_bridge() -> dict:
    return {
        "ready_to_underwrite": True,
        "renovation_program": {
            "renovation_cost_per_unit": 16800,
            "rent_premium_monthly": 200,
            "downtime_days": 21,
            "strategy": "renovation",
            "start_month": "2026-06",
            "monthly_pace": 5,
        },
        "roi_result": {
            "roi_pct": 17.1,
            "clears_threshold": True,
            "total_cost_high": 16800,
            "current_monthly_rent": 900,
            "target_monthly_rent": 1100,
            "monthly_rent_lift": 200,
            "annual_rent_lift": 2400,
            "threshold_pct": 15.0,
        },
    }


def _failing_bridge(current_rent: float = 900, target_rent: float = 950) -> dict:
    cost = 16800
    lift = (target_rent - current_rent) * 12
    roi = (lift / cost) * 100
    return {
        "ready_to_underwrite": False,
        "renovation_program": {
            "renovation_cost_per_unit": None,
            "rent_premium_monthly": target_rent - current_rent,
            "downtime_days": 21,
            "strategy": "renovation",
            "start_month": "2026-06",
            "monthly_pace": 5,
        },
        "roi_result": {
            "roi_pct": roi,
            "clears_threshold": False,
            "total_cost_high": cost,
            "current_monthly_rent": current_rent,
            "target_monthly_rent": target_rent,
            "monthly_rent_lift": target_rent - current_rent,
            "annual_rent_lift": lift,
            "threshold_pct": 15.0,
            "path_to_pass": ["Increase target rent by $X or reduce scope"],
        },
    }


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

class TestAnalyzeDealHappyPath:
    def test_returns_deal_analysis(self):
        inputs = _deal()
        client = _mock_client(_prop_estimate(), [_passing_bridge()])
        result = analyze_deal(inputs, client=client)
        assert isinstance(result, DealAnalysis)

    def test_ready_to_underwrite_when_all_pass(self):
        inputs = _deal()
        client = _mock_client(_prop_estimate(), [_passing_bridge()])
        result = analyze_deal(inputs, client=client)
        assert result.ready_to_underwrite is True

    def test_property_id_carried_through(self):
        inputs = _deal(property_id="PROP_DALLAS_001")
        client = _mock_client(_prop_estimate(), [_passing_bridge()])
        result = analyze_deal(inputs, client=client)
        assert result.property_id == "PROP_DALLAS_001"

    def test_renovation_programs_populated(self):
        inputs = _deal()
        client = _mock_client(_prop_estimate(), [_passing_bridge()])
        result = analyze_deal(inputs, client=client)
        assert len(result.renovation_programs) == 1
        prog = result.renovation_programs[0]
        assert prog["renovation_cost_per_unit"] == 16800
        assert prog["strategy"] == "on_turnover"  # canonical schema enum
        assert prog["start_month"] == "2026-06"

    def test_renovation_program_schema_fields(self):
        """All required underwriting schema fields must be present."""
        inputs = _deal()
        client = _mock_client(_prop_estimate(), [_passing_bridge()])
        result = analyze_deal(inputs, client=client)
        required_fields = {
            "renovation_cost_per_unit", "rent_premium_monthly",
            "downtime_days", "strategy", "start_month", "monthly_pace",
        }
        for prog in result.renovation_programs:
            assert required_fields <= prog.keys()

    def test_exterior_capex_carried_through(self):
        inputs = _deal()
        client = _mock_client(_prop_estimate(), [_passing_bridge()])
        result = analyze_deal(inputs, client=client)
        assert result.exterior_capex_high == 80000

    def test_risk_flags_carried_through(self):
        inputs = _deal()
        client = _mock_client(_prop_estimate(), [_passing_bridge()])
        result = analyze_deal(inputs, client=client)
        assert len(result.risk_flags) == 1
        assert result.risk_flags[0]["flag_type"] == "galvanized_pipe"

    def test_unit_type_result_populated(self):
        inputs = _deal()
        client = _mock_client(_prop_estimate(), [_passing_bridge()])
        result = analyze_deal(inputs, client=client)
        assert len(result.unit_type_results) == 1
        utr = result.unit_type_results[0]
        assert utr.sqft == 850
        assert utr.roi_pct == 17.1
        assert utr.roi_passes is True

    def test_summary_contains_property_id(self):
        inputs = _deal()
        client = _mock_client(_prop_estimate(), [_passing_bridge()])
        result = analyze_deal(inputs, client=client)
        assert "PROP001" in result.summary


# ---------------------------------------------------------------------------
# ROI gate failure
# ---------------------------------------------------------------------------

class TestAnalyzeDealROIFailure:
    def test_not_ready_when_any_type_fails(self):
        inputs = _deal()
        client = _mock_client(_prop_estimate(), [_failing_bridge()])
        result = analyze_deal(inputs, client=client)
        assert result.ready_to_underwrite is False

    def test_no_renovation_program_on_failure(self):
        inputs = _deal()
        client = _mock_client(_prop_estimate(), [_failing_bridge()])
        result = analyze_deal(inputs, client=client)
        assert len(result.renovation_programs) == 0
        assert result.unit_type_results[0].renovation_program is None

    def test_roi_result_attached_on_failure(self):
        inputs = _deal()
        client = _mock_client(_prop_estimate(), [_failing_bridge()])
        result = analyze_deal(inputs, client=client)
        roi = result.unit_type_results[0].roi_result
        assert roi["clears_threshold"] is False
        assert "path_to_pass" in roi

    def test_partial_pass_partial_fail(self):
        """Two unit types: one passes, one fails."""
        inputs = _deal(
            total_units=30,
            unit_mix=[
                UnitMixEntry(sqft=600, bedrooms=1, bathrooms=1, count=20,
                             current_monthly_rent=750, target_monthly_rent=950),
                UnitMixEntry(sqft=850, bedrooms=2, bathrooms=1, count=10,
                             current_monthly_rent=900, target_monthly_rent=950),  # fails
            ],
        )
        prop_est = _prop_estimate(n_units=30)
        client = _mock_client(prop_est, [_passing_bridge(), _failing_bridge()])
        result = analyze_deal(inputs, client=client)
        assert result.ready_to_underwrite is False
        assert result.unit_type_results[0].roi_passes is True
        assert result.unit_type_results[1].roi_passes is False
        assert len(result.renovation_programs) == 1  # only passing type


# ---------------------------------------------------------------------------
# Multi-unit-type happy path
# ---------------------------------------------------------------------------

class TestMultiUnitType:
    def test_two_passing_types(self):
        inputs = _deal(
            total_units=30,
            unit_mix=[
                UnitMixEntry(sqft=600, bedrooms=1, bathrooms=1, count=20,
                             current_monthly_rent=750, target_monthly_rent=950),
                UnitMixEntry(sqft=850, bedrooms=2, bathrooms=1, count=10,
                             current_monthly_rent=900, target_monthly_rent=1100),
            ],
        )
        prop_est = _prop_estimate(n_units=30)
        client = _mock_client(prop_est, [_passing_bridge(), _passing_bridge()])
        result = analyze_deal(inputs, client=client)
        assert result.ready_to_underwrite is True
        assert len(result.unit_type_results) == 2
        assert len(result.renovation_programs) == 2


# ---------------------------------------------------------------------------
# _slice_for_unit_type helper
# ---------------------------------------------------------------------------

class TestSliceForUnitType:
    def test_single_type(self):
        unit_estimates = [{"id": i} for i in range(5)]
        mix = [UnitMixEntry(sqft=850, bedrooms=2, bathrooms=1, count=5,
                            current_monthly_rent=900, target_monthly_rent=1050)]
        sliced = _slice_for_unit_type(unit_estimates, mix, 0)
        assert len(sliced) == 5
        assert sliced[0]["id"] == 0

    def test_second_type_offset(self):
        unit_estimates = [{"id": i} for i in range(15)]
        mix = [
            UnitMixEntry(sqft=600, bedrooms=1, bathrooms=1, count=10,
                         current_monthly_rent=750, target_monthly_rent=900),
            UnitMixEntry(sqft=850, bedrooms=2, bathrooms=1, count=5,
                         current_monthly_rent=900, target_monthly_rent=1050),
        ]
        sliced = _slice_for_unit_type(unit_estimates, mix, 1)
        assert len(sliced) == 5
        assert sliced[0]["id"] == 10  # offset by first type's count


# ---------------------------------------------------------------------------
# Underwriting integration (with mocked underwriting client)
# ---------------------------------------------------------------------------

def _mock_uw_client(summary_result: dict) -> MagicMock:
    client = MagicMock()
    client.run_summary.return_value = summary_result
    return client


def _uw_success() -> dict:
    return {
        "status": "success",
        "irr": {"levered_irr": 0.145, "unlevered_irr": 0.095},
        "equity_multiple": {"levered_em": 1.85, "unlevered_em": 1.55},
        "dscr": {"minimum": 1.25, "average": 1.45},
        "yields": {"going_in_cap_rate": 0.055, "exit_cap_rate": 0.06},
        "noi_summary": {"total_noi": 2500000, "average_noi_margin": 0.52},
        "total_capex": 1500000,
    }


def _uw_marginal() -> dict:
    return {
        "status": "success",
        "irr": {"levered_irr": 0.10, "unlevered_irr": 0.07},
        "equity_multiple": {"levered_em": 1.35, "unlevered_em": 1.2},
        "dscr": {"minimum": 1.10, "average": 1.25},
        "yields": {"going_in_cap_rate": 0.045, "exit_cap_rate": 0.055},
        "noi_summary": {},
        "total_capex": 0,
    }


def _base_deal() -> dict:
    """Minimal base deal inputs for underwriting engine."""
    return {
        "metadata": {"deal_id": "D1", "schema_version": "0.1"},
        "unit_cohorts": [{"cohort_id": "2BR1BA_0", "unit_count": 10}],
        "time_grid": {"analysis_start_date": "2026-06-01", "analysis_end_date": "2033-05-01"},
    }


class TestAnalyzeDealWithUnderwriting:
    def test_underwriting_metrics_populated(self):
        inputs = _deal(base_deal_inputs=_base_deal())
        cm_client = _mock_client(_prop_estimate(), [_passing_bridge()])
        uw_client = _mock_uw_client(_uw_success())
        result = analyze_deal(inputs, client=cm_client, underwriting_client=uw_client)
        assert result.underwriting_metrics is not None
        assert result.underwriting_metrics["irr"]["levered_irr"] == 0.145

    def test_underwriting_feasible_when_metrics_pass(self):
        inputs = _deal(base_deal_inputs=_base_deal())
        cm_client = _mock_client(_prop_estimate(), [_passing_bridge()])
        uw_client = _mock_uw_client(_uw_success())
        result = analyze_deal(inputs, client=cm_client, underwriting_client=uw_client)
        assert result.underwriting_feasible is True

    def test_underwriting_not_feasible_when_irr_low(self):
        inputs = _deal(base_deal_inputs=_base_deal())
        cm_client = _mock_client(_prop_estimate(), [_passing_bridge()])
        uw_client = _mock_uw_client(_uw_marginal())
        result = analyze_deal(inputs, client=cm_client, underwriting_client=uw_client)
        assert result.underwriting_feasible is False

    def test_no_underwriting_without_base_inputs(self):
        inputs = _deal()  # no base_deal_inputs
        cm_client = _mock_client(_prop_estimate(), [_passing_bridge()])
        result = analyze_deal(inputs, client=cm_client)
        assert result.underwriting_metrics is None
        assert result.underwriting_feasible is None

    def test_no_underwriting_when_roi_fails(self):
        inputs = _deal(base_deal_inputs=_base_deal())
        cm_client = _mock_client(_prop_estimate(), [_failing_bridge()])
        uw_client = _mock_uw_client(_uw_success())
        result = analyze_deal(inputs, client=cm_client, underwriting_client=uw_client)
        # ROI gate failed → underwriting should NOT run
        assert result.underwriting_metrics is None
        uw_client.run_summary.assert_not_called()

    def test_summary_includes_underwriting(self):
        inputs = _deal(base_deal_inputs=_base_deal())
        cm_client = _mock_client(_prop_estimate(), [_passing_bridge()])
        uw_client = _mock_uw_client(_uw_success())
        result = analyze_deal(inputs, client=cm_client, underwriting_client=uw_client)
        assert "Levered IRR" in result.summary
        assert "DSCR" in result.summary

    def test_canonical_programs_have_schema_fields(self):
        inputs = _deal(base_deal_inputs=_base_deal())
        cm_client = _mock_client(_prop_estimate(), [_passing_bridge()])
        uw_client = _mock_uw_client(_uw_success())
        result = analyze_deal(inputs, client=cm_client, underwriting_client=uw_client)
        required = {"program_id", "program_name", "target_cohort", "output_cohort",
                     "renovation_cost_per_unit", "rent_premium_monthly",
                     "downtime_days", "strategy", "start_month", "monthly_pace"}
        for prog in result.renovation_programs:
            assert required <= prog.keys()


# ---------------------------------------------------------------------------
# Full-cashflow mode (direct-import path)
# ---------------------------------------------------------------------------

def _uw_full_success() -> dict:
    """Shape returned by engine.api.handle_run_deal — flat metrics dict."""
    return {
        "status": "success",
        "deal_id": "D1",
        "metrics": {
            "levered_irr": 0.145,
            "unlevered_irr": 0.095,
            "levered_em": 1.85,
            "unlevered_em": 1.55,
            "partnership_irr": 0.17,
            "partnership_em": 1.9,
            "average_dscr": 1.45,
            "minimum_dscr": 1.25,
            "going_in_cap": 0.055,
        },
        "cashflow_summary": {"years": 7, "noi_year_1": 1_800_000, "noi_exit": 2_600_000},
        "cashflow": {"by_year": [{"net_operating_income": 1_800_000}]},
        "elapsed_seconds": 0.42,
    }


def _uw_full_marginal() -> dict:
    return {
        "status": "success",
        "deal_id": "D1",
        "metrics": {
            "levered_irr": 0.10,
            "unlevered_irr": 0.07,
            "levered_em": 1.35,
            "unlevered_em": 1.2,
            "average_dscr": 1.25,
            "minimum_dscr": 1.10,
            "going_in_cap": 0.045,
        },
        "cashflow_summary": {"years": 7},
        "cashflow": {},
    }


def _mock_uw_client_full(full_result: dict) -> MagicMock:
    client = MagicMock()
    client.run_full.return_value = full_result
    return client


class TestFullCashflowMode:
    def test_run_full_called_when_full_cashflow_true(self):
        inputs = _deal(base_deal_inputs=_base_deal(), full_cashflow=True)
        cm_client = _mock_client(_prop_estimate(), [_passing_bridge()])
        uw_client = _mock_uw_client_full(_uw_full_success())
        analyze_deal(inputs, client=cm_client, underwriting_client=uw_client)
        uw_client.run_full.assert_called_once()
        uw_client.run_summary.assert_not_called()

    def test_run_summary_called_by_default(self):
        inputs = _deal(base_deal_inputs=_base_deal())  # full_cashflow defaults False
        cm_client = _mock_client(_prop_estimate(), [_passing_bridge()])
        uw_client = _mock_uw_client(_uw_success())
        analyze_deal(inputs, client=cm_client, underwriting_client=uw_client)
        uw_client.run_summary.assert_called_once()
        uw_client.run_full.assert_not_called()

    def test_full_result_stored_on_underwriting_full(self):
        inputs = _deal(base_deal_inputs=_base_deal(), full_cashflow=True)
        cm_client = _mock_client(_prop_estimate(), [_passing_bridge()])
        uw_client = _mock_uw_client_full(_uw_full_success())
        result = analyze_deal(inputs, client=cm_client, underwriting_client=uw_client)
        assert result.underwriting_full is not None
        assert result.underwriting_full["cashflow_summary"]["years"] == 7
        assert "by_year" in result.underwriting_full["cashflow"]

    def test_underwriting_full_none_in_summary_mode(self):
        inputs = _deal(base_deal_inputs=_base_deal())
        cm_client = _mock_client(_prop_estimate(), [_passing_bridge()])
        uw_client = _mock_uw_client(_uw_success())
        result = analyze_deal(inputs, client=cm_client, underwriting_client=uw_client)
        assert result.underwriting_full is None

    def test_flat_metrics_normalized_to_nested(self):
        """Full-mode metrics must land in the same nested shape as summary-mode."""
        inputs = _deal(base_deal_inputs=_base_deal(), full_cashflow=True)
        cm_client = _mock_client(_prop_estimate(), [_passing_bridge()])
        uw_client = _mock_uw_client_full(_uw_full_success())
        result = analyze_deal(inputs, client=cm_client, underwriting_client=uw_client)
        m = result.underwriting_metrics
        assert m["irr"]["levered_irr"] == 0.145
        assert m["equity_multiple"]["levered_em"] == 1.85
        assert m["dscr"]["minimum"] == 1.25
        assert m["dscr"]["average"] == 1.45
        assert m["yields"]["going_in_cap_rate"] == 0.055

    def test_feasibility_gate_works_in_full_mode(self):
        inputs = _deal(base_deal_inputs=_base_deal(), full_cashflow=True)
        cm_client = _mock_client(_prop_estimate(), [_passing_bridge()])
        uw_client = _mock_uw_client_full(_uw_full_success())
        result = analyze_deal(inputs, client=cm_client, underwriting_client=uw_client)
        assert result.underwriting_feasible is True

    def test_feasibility_fails_in_full_mode_when_marginal(self):
        inputs = _deal(base_deal_inputs=_base_deal(), full_cashflow=True)
        cm_client = _mock_client(_prop_estimate(), [_passing_bridge()])
        uw_client = _mock_uw_client_full(_uw_full_marginal())
        result = analyze_deal(inputs, client=cm_client, underwriting_client=uw_client)
        assert result.underwriting_feasible is False

    def test_summary_includes_normalized_metrics_in_full_mode(self):
        """_build_summary uses the nested shape — must render in full mode too."""
        inputs = _deal(base_deal_inputs=_base_deal(), full_cashflow=True)
        cm_client = _mock_client(_prop_estimate(), [_passing_bridge()])
        uw_client = _mock_uw_client_full(_uw_full_success())
        result = analyze_deal(inputs, client=cm_client, underwriting_client=uw_client)
        assert "Levered IRR" in result.summary
        assert "DSCR" in result.summary

    def test_full_cashflow_ignored_when_roi_fails(self):
        inputs = _deal(base_deal_inputs=_base_deal(), full_cashflow=True)
        cm_client = _mock_client(_prop_estimate(), [_failing_bridge()])
        uw_client = _mock_uw_client_full(_uw_full_success())
        result = analyze_deal(inputs, client=cm_client, underwriting_client=uw_client)
        assert result.underwriting_full is None
        assert result.underwriting_metrics is None
        uw_client.run_full.assert_not_called()


# ---------------------------------------------------------------------------
# ROI diagnostic promotion + decision_summary wiring
# ---------------------------------------------------------------------------

def _failing_bridge_with_path(current=900, target=950) -> dict:
    """Like _failing_bridge but includes the path-to-pass deltas costmodel
    actually returns in production. Required to verify the analyzer surfaces
    them on UnitTypeResult."""
    bridge = _failing_bridge(current_rent=current, target_rent=target)
    bridge["roi_result"]["cost_reduction_needed"] = 4800
    bridge["roi_result"]["rent_increase_needed"] = 60
    return bridge


class TestROIDiagnosticsPromotion:
    def test_diagnostics_populated_on_pass(self):
        inputs = _deal()
        client = _mock_client(_prop_estimate(), [_passing_bridge()])
        result = analyze_deal(inputs, client=client)
        u = result.unit_type_results[0]
        assert u.monthly_rent_lift == 200
        assert u.annual_rent_lift == 2400
        assert u.roi_gap_pp == pytest.approx(2.1)  # 17.1 - 15.0
        # 16800 cost / 200 lift = 84 months payback
        assert u.simple_payback_months == 84.0
        # Pass → no remediation deltas
        assert u.cost_reduction_needed_to_pass is None
        assert u.rent_increase_needed_to_pass is None

    def test_diagnostics_populated_on_failure(self):
        inputs = _deal()
        client = _mock_client(_prop_estimate(), [_failing_bridge_with_path()])
        result = analyze_deal(inputs, client=client)
        u = result.unit_type_results[0]
        assert u.roi_passes is False
        assert u.roi_gap_pp < 0  # below threshold
        assert u.cost_reduction_needed_to_pass == 4800
        assert u.rent_increase_needed_to_pass == 60

    def test_payback_none_when_no_lift(self):
        # current_rent == target_rent → zero monthly lift → payback undefined
        bridge = _failing_bridge(current_rent=900, target_rent=900)
        bridge["roi_result"]["monthly_rent_lift"] = 0
        bridge["roi_result"]["annual_rent_lift"] = 0
        client = _mock_client(_prop_estimate(), [bridge])
        result = analyze_deal(_deal(), client=client)
        assert result.unit_type_results[0].simple_payback_months is None


class TestDecisionSummaryWiring:
    def test_decision_summary_present_on_pass(self):
        inputs = _deal()
        client = _mock_client(_prop_estimate(), [_passing_bridge()])
        result = analyze_deal(inputs, client=client)
        ds = result.decision_summary
        assert "headline" in ds
        assert isinstance(ds["binding_constraints"], list)
        assert isinstance(ds["recommended_changes"], list)

    def test_decision_summary_reflects_roi_failure(self):
        inputs = _deal()
        client = _mock_client(_prop_estimate(), [_failing_bridge_with_path()])
        result = analyze_deal(inputs, client=client)
        ds = result.decision_summary
        assert "Not ready" in ds["headline"]
        # Recommended changes should include the rent and cost paths
        joined = " | ".join(ds["recommended_changes"])
        assert "$60" in joined or "$4,800" in joined

    def test_summary_text_includes_decision_section(self):
        inputs = _deal()
        client = _mock_client(_prop_estimate(), [_failing_bridge_with_path()])
        result = analyze_deal(inputs, client=client)
        assert "Decision Summary" in result.summary


# ---------------------------------------------------------------------------
# analyze_deal + comps integration (rent validation fold-in)
# ---------------------------------------------------------------------------

def _comp(rent: float, br=2, ba=1.0, sqft=850.0, name="C") -> dict:
    return {
        "property_name": name, "bedrooms": br, "bathrooms": ba, "sqft": sqft,
        "asking_rent": rent, "rent_per_sqft": round(rent / sqft, 2),
        "source": "test",
    }


class TestAnalyzeWithComps:
    def test_no_comps_no_change_in_decision_summary(self):
        """Baseline: without comps, decision_summary has no [rent] entries."""
        inputs = _deal()  # target_monthly_rent=1100, sqft=850, 2BR/1BA
        client = _mock_client(_prop_estimate(), [_passing_bridge()])
        result = analyze_deal(inputs, client=client)
        joined = " | ".join(result.decision_summary["binding_constraints"])
        assert "[rent]" not in joined

    def test_above_band_adds_constraint(self):
        """target_monthly_rent=1100 vs comps {800,900,1000} → above p75=950."""
        inputs = _deal()
        comps = {"2BR_1.0BA_850sf": [_comp(r) for r in (800, 900, 1000)]}
        client = _mock_client(_prop_estimate(), [_passing_bridge()])
        result = analyze_deal(inputs, client=client, comps_by_cohort=comps)
        joined = " | ".join(result.decision_summary["binding_constraints"])
        assert "[rent]" in joined
        assert "above the comp p75" in joined

    def test_within_band_adds_no_constraint(self):
        """target_monthly_rent=1100 inside IQR of {1000,1100,1200,1300,1400}."""
        inputs = _deal()
        comps = {"2BR_1.0BA_850sf": [_comp(r) for r in (1000, 1100, 1200, 1300, 1400)]}
        client = _mock_client(_prop_estimate(), [_passing_bridge()])
        result = analyze_deal(inputs, client=client, comps_by_cohort=comps)
        joined = " | ".join(result.decision_summary["binding_constraints"])
        assert "[rent]" not in joined

    def test_below_band_adds_no_constraint(self):
        """Below-band is good news (conservative rent) — must not block."""
        inputs = _deal()
        comps = {"2BR_1.0BA_850sf": [_comp(r) for r in (1500, 1600, 1700)]}
        client = _mock_client(_prop_estimate(), [_passing_bridge()])
        result = analyze_deal(inputs, client=client, comps_by_cohort=comps)
        joined = " | ".join(result.decision_summary["binding_constraints"])
        assert "[rent]" not in joined

    def test_insufficient_comps_does_not_block(self):
        """<3 comps → insufficient_comps verdict; not folded into constraints."""
        inputs = _deal()
        comps = {"2BR_1.0BA_850sf": [_comp(800), _comp(900)]}  # only 2 comps
        client = _mock_client(_prop_estimate(), [_passing_bridge()])
        result = analyze_deal(inputs, client=client, comps_by_cohort=comps)
        joined = " | ".join(result.decision_summary["binding_constraints"])
        assert "[rent]" not in joined

    def test_comps_dict_under_top_level_key_unwrapped_by_cli(self):
        """Sanity: analyzer accepts the bare comps_by_cohort dict — the CLI is
        responsible for unwrapping {comps_by_cohort: {...}} envelope shapes."""
        inputs = _deal()
        comps = {"2BR_1.0BA_850sf": [_comp(r) for r in (800, 900, 1000)]}
        client = _mock_client(_prop_estimate(), [_passing_bridge()])
        # Pass the bare dict directly — must work
        result = analyze_deal(inputs, client=client, comps_by_cohort=comps)
        assert any(
            "[rent]" in c for c in result.decision_summary["binding_constraints"]
        )
