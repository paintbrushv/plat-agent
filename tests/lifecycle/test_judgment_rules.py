import pytest

from plat_agent.lifecycle.judgment_rules import (
    compute_delta_flag,
    compute_delta_severity,
    is_directional_optimistic,
)


def test_delta_green_when_om_within_yellow_threshold() -> None:
    # om = data * 1.05 → 5% delta < 15% yellow threshold
    flag = compute_delta_flag(data_derived=10000.0, om_claimed=10500.0,
                              direction_when_higher_om="optimistic")
    assert flag == "green"


def test_delta_yellow_at_15pct() -> None:
    # om = data * 1.20 → 20% delta in yellow band [15%, 25%)
    flag = compute_delta_flag(data_derived=10000.0, om_claimed=12000.0,
                              direction_when_higher_om="optimistic")
    assert flag == "yellow"


def test_delta_red_at_25pct_or_more() -> None:
    flag = compute_delta_flag(data_derived=10000.0, om_claimed=13000.0,
                              direction_when_higher_om="optimistic")
    assert flag == "red"


def test_delta_directional_broker_optimistic_for_higher_om() -> None:
    # rent_growth: higher OM = broker optimistic
    flag = compute_delta_flag(data_derived=0.030, om_claimed=0.040,
                              direction_when_higher_om="optimistic",
                              prefer_directional=True)
    # 33% delta → red, but directional preference returns "broker_optimistic"
    assert flag == "broker_optimistic"


def test_delta_directional_broker_underestimates_for_lower_om() -> None:
    # capex_per_unit: lower OM = broker underestimates
    flag = compute_delta_flag(data_derived=18000.0, om_claimed=12000.0,
                              direction_when_higher_om="underestimates",
                              prefer_directional=True)
    # OM is 33% LOWER → broker_underestimates
    assert flag == "broker_underestimates"


def test_delta_green_when_om_missing() -> None:
    # No OM claim → delta_flag green by definition
    flag = compute_delta_flag(data_derived=10000.0, om_claimed=None,
                              direction_when_higher_om="optimistic")
    assert flag == "green"


def test_delta_green_when_data_derived_zero() -> None:
    # data_derived=0 → cannot compute pct delta; default green
    flag = compute_delta_flag(data_derived=0.0, om_claimed=100.0,
                              direction_when_higher_om="optimistic")
    assert flag == "green"


def test_is_directional_optimistic_higher_om_optimistic() -> None:
    assert is_directional_optimistic(
        data_derived=0.030, om_claimed=0.040,
        direction_when_higher_om="optimistic",
    ) is True


def test_is_directional_optimistic_lower_om_underestimates() -> None:
    # capex case: lower OM is broker underestimating, NOT optimistic from cap-rate POV.
    # The helper returns "is the OM-versus-data delta directionally bad for the GP?"
    assert is_directional_optimistic(
        data_derived=18000.0, om_claimed=12000.0,
        direction_when_higher_om="underestimates",
    ) is True


def test_severity_green_under_yellow_threshold() -> None:
    # 5% delta < 15% yellow threshold
    assert compute_delta_severity(data_derived=10000.0, om_claimed=10500.0) == "green"


def test_severity_yellow_in_band() -> None:
    # 20% delta in yellow band [15%, 25%)
    assert compute_delta_severity(data_derived=10000.0, om_claimed=12000.0) == "yellow"


def test_severity_red_at_or_above_25pct() -> None:
    # 30% delta ≥ 25% red threshold
    assert compute_delta_severity(data_derived=10000.0, om_claimed=13000.0) == "red"


def test_severity_independent_of_direction() -> None:
    # Severity must NOT depend on direction — both directions of a delta
    # whose absolute pct exceeds the 25% red threshold under spec §1 must
    # land on "red". Swap the data/om sides of the same pair (10k vs 15k)
    # to confirm symmetry: 50% one way, 33.3% the other — both red.
    assert compute_delta_severity(data_derived=10000.0, om_claimed=15000.0) == "red"
    assert compute_delta_severity(data_derived=15000.0, om_claimed=10000.0) == "red"


def test_severity_green_when_om_missing_or_zero_data() -> None:
    assert compute_delta_severity(data_derived=10000.0, om_claimed=None) == "green"
    assert compute_delta_severity(data_derived=0.0, om_claimed=100.0) == "green"


from plat_agent.lifecycle.judgment_rules import classify_positioning


def _intake(in_place_rent: float, units: int = 200) -> dict:
    return {
        "metadata": {"address": "1 Test Way", "year_built": 1995},
        "unit_cohorts": [
            {
                "cohort_id": "1BR",
                "unit_count": units,
                "in_place_rent": in_place_rent,
                "market_rent": in_place_rent,
            }
        ],
        "purchase_assumptions": {"purchase_price": 25_000_000},
    }


def _comps(comp_rent: float, n_comps: int = 4) -> dict:
    return {
        "subject": {"address": "1 Test Way", "metro_slug": "dallas_tx"},
        "as_of": "2026-05-05",
        "comps": [
            {
                "comp_id": f"comp_00{i}",
                "name": f"Comp {i}",
                "address": f"{i} Comp Way",
                "units": 200,
                "unit_types": [
                    {"unit_type": "1BR", "effective_rent": comp_rent, "sqft": 720},
                ],
            }
            for i in range(1, n_comps + 1)
        ],
    }


def test_classify_value_add_when_rent_gap_over_10pct() -> None:
    intake = _intake(in_place_rent=1300.0)
    comps = _comps(comp_rent=1500.0)  # ~15% gap
    pos = classify_positioning(intake, comps)
    assert pos.value == "value_add"
    assert pos.confidence >= 0.6
    assert "rent gap" in pos.rationale.lower() or "value-add" in pos.rationale.lower() or "renovation" in pos.rationale.lower()


def test_classify_stabilized_when_rent_gap_under_10pct() -> None:
    intake = _intake(in_place_rent=1480.0)
    comps = _comps(comp_rent=1500.0)  # ~1.4% gap
    pos = classify_positioning(intake, comps)
    assert pos.value == "stabilized"
    assert pos.confidence >= 0.6


def test_classify_stabilized_when_comps_rent_below_subject() -> None:
    intake = _intake(in_place_rent=1800.0)
    comps = _comps(comp_rent=1500.0)
    pos = classify_positioning(intake, comps)
    assert pos.value == "stabilized"
    assert "below subject" in pos.rationale.lower()


def test_classify_stabilized_default_when_comps_unavailable() -> None:
    intake = _intake(in_place_rent=1300.0)
    pos = classify_positioning(intake, comps=None)
    # Without comps we cannot detect rent gap → default stabilized at lower confidence
    assert pos.value == "stabilized"
    assert pos.confidence < 0.6


def test_classify_handles_no_comps_with_unit_types() -> None:
    intake = _intake(in_place_rent=1300.0)
    # Comps present but lack unit_types analytics
    comps = {
        "subject": {"address": "x", "metro_slug": "dallas_tx"},
        "as_of": "2026-05-05",
        "comps": [{"comp_id": "c1", "name": "x", "address": "y", "units": 100}],
    }
    pos = classify_positioning(intake, comps)
    assert pos.value == "stabilized"
    assert pos.confidence < 0.6


def test_classify_uses_face_rent_when_effective_rent_missing() -> None:
    intake = _intake(in_place_rent=1300.0)
    comps = {
        "subject": {"address": "x", "metro_slug": "dallas_tx"},
        "as_of": "2026-05-05",
        "comps": [
            {
                "comp_id": "c1",
                "name": "Face Rent Comp",
                "address": "1 Comp Way",
                "units": 100,
                "unit_types": [{"unit_type": "1BR", "face_rent": 1500.0, "sqft": 720}],
            }
        ],
    }
    pos = classify_positioning(intake, comps)
    assert pos.value == "value_add"
    assert pos.confidence >= 0.6


def test_classify_uses_initial_inplace_rent_when_in_place_rent_missing() -> None:
    intake = {
        "metadata": {"address": "1 Test Way", "year_built": 1995},
        "unit_cohorts": [
            {
                "cohort_id": "1BR",
                "unit_count": 200,
                "initial_inplace_rent": 1300.0,
                "target_monthly_rent": 1300.0,
            }
        ],
        "purchase_assumptions": {"purchase_price": 25_000_000},
    }
    comps = _comps(comp_rent=1500.0)
    pos = classify_positioning(intake, comps)
    assert pos.value == "value_add"
    assert pos.confidence >= 0.6


def test_classify_prefers_house_box_score_in_place_rent_over_stale_local_cohorts() -> None:
    intake = {
        "metadata": {
            "address": "1 Test Way",
            "year_built": 1984,
            "property_summary": {
                "house_box_score": {
                    "floorplans": [
                        {"code": "a1", "avg_in_place_rent": 1300.0, "units": 100},
                        {"code": "b1", "avg_in_place_rent": 1400.0, "units": 100},
                    ]
                }
            },
        },
        "unit_cohorts": [
            {"cohort_id": "a1", "unit_count": 100, "initial_inplace_rent": 1800.0},
            {"cohort_id": "b1", "unit_count": 100, "initial_inplace_rent": 1900.0},
        ],
        "purchase_assumptions": {"purchase_price": 25_000_000},
    }
    comps = _comps(comp_rent=1500.0)
    pos = classify_positioning(intake, comps)
    assert pos.value == "value_add"
    assert "subject in-place rent $1,350" in pos.rationale


from plat_agent.lifecycle.judgment_rules import compute_capex_per_unit


def test_capex_value_add_uses_rent_gap_factor() -> None:
    intake = _intake(in_place_rent=1300.0)
    comps = _comps(comp_rent=1500.0)
    # rent gap = $200/month → data_derived ≈ 200 * 120 = $24,000/unit
    field = compute_capex_per_unit(intake, comps, positioning_value="value_add")
    assert field is not None
    assert 22000 <= field.data_derived <= 26000
    assert field.delta_flag in {"green", "yellow", "red", "broker_underestimates"}


def test_capex_om_claim_underestimates_triggers_broker_flag() -> None:
    intake = dict(_intake(in_place_rent=1300.0))
    intake["capex_assumptions"] = {"renovation_cost_per_unit": 12000.0}
    comps = _comps(comp_rent=1500.0)
    field = compute_capex_per_unit(intake, comps, positioning_value="value_add")
    assert field is not None
    assert field.om_claimed == 12000.0
    # OM underestimates by ~50% → broker_underestimates
    assert field.delta_flag == "broker_underestimates"
    # Conservative pick: max(data, om) → should pick the higher data-derived
    assert field.selected >= 22000


def test_capex_stabilized_returns_low_capex() -> None:
    intake = _intake(in_place_rent=1480.0)
    comps = _comps(comp_rent=1500.0)
    # Stabilized → no value-add capex; small turn-cost only
    field = compute_capex_per_unit(intake, comps, positioning_value="stabilized")
    assert field is not None
    assert field.selected <= 5000


def test_capex_returns_none_when_no_intake_rent_data() -> None:
    intake = {"metadata": {}, "unit_cohorts": [], "purchase_assumptions": {"purchase_price": 1}}
    comps = _comps(comp_rent=1500.0)
    field = compute_capex_per_unit(intake, comps, positioning_value="value_add")
    assert field is None


from plat_agent.lifecycle.judgment_rules import (
    compute_exit_cap,
    compute_rent_growth,
    compute_renovation_pace,
)


def test_renovation_pace_value_add_default_5() -> None:
    intake = _intake(in_place_rent=1300.0, units=200)
    comps = _comps(comp_rent=1500.0)
    field = compute_renovation_pace(intake, comps, positioning_value="value_add")
    assert field is not None
    assert field.selected == 5.0
    assert field.confidence >= 0.5


def test_renovation_pace_stabilized_zero() -> None:
    intake = _intake(in_place_rent=1480.0, units=200)
    comps = _comps(comp_rent=1500.0)
    field = compute_renovation_pace(intake, comps, positioning_value="stabilized")
    assert field is not None
    assert field.selected == 0.0


def test_rent_growth_uses_submarket_aggregate_when_present() -> None:
    intake = _intake(in_place_rent=1300.0)
    comps = _comps(comp_rent=1500.0)
    comps["submarket_aggregates"] = {"rent_growth_trailing_12mo": 0.025}
    field = compute_rent_growth(intake, comps)
    assert field is not None
    assert abs(field.data_derived - 0.025) < 1e-9


def test_rent_growth_falls_back_to_default_3pct_without_comps() -> None:
    intake = _intake(in_place_rent=1300.0)
    field = compute_rent_growth(intake, comps=None)
    assert field is not None
    assert abs(field.selected - 0.03) < 1e-9
    assert field.confidence < 0.6


def test_rent_growth_om_optimistic_triggers_broker_flag() -> None:
    intake = dict(_intake(in_place_rent=1300.0))
    intake["growth_assumptions"] = {"rent_growth": 0.05}
    comps = _comps(comp_rent=1500.0)
    comps["submarket_aggregates"] = {"rent_growth_trailing_12mo": 0.025}
    field = compute_rent_growth(intake, comps)
    assert field is not None
    assert field.om_claimed == 0.05
    # 100% delta from data → broker_optimistic
    assert field.delta_flag == "broker_optimistic"
    # Conservative: pick the lower (data-derived)
    assert field.selected == field.data_derived


def test_exit_cap_uses_avg_comp_cap_rate_when_present() -> None:
    intake = _intake(in_place_rent=1300.0)
    comps = _comps(comp_rent=1500.0)
    for c in comps["comps"]:
        c["cap_rate_est"] = 0.055
    field = compute_exit_cap(intake, comps)
    assert field is not None
    assert abs(field.data_derived - 0.055) < 1e-9


def test_exit_cap_fallback_default_60bps_above_going_in() -> None:
    intake = _intake(in_place_rent=1300.0)
    field = compute_exit_cap(intake, comps=None)
    # No comps + no OM → default is reasonable (e.g., 0.060)
    assert field is not None
    assert 0.05 <= field.selected <= 0.07


def test_exit_cap_om_optimistic_triggers_broker_flag() -> None:
    intake = dict(_intake(in_place_rent=1300.0))
    intake["exit_assumptions"] = {"exit_cap_rate": 0.045}
    comps = _comps(comp_rent=1500.0)
    for c in comps["comps"]:
        c["cap_rate_est"] = 0.060
    field = compute_exit_cap(intake, comps)
    assert field is not None
    assert field.om_claimed == 0.045
    # OM is LOWER than data-derived → seller sees lower cap = higher value = optimistic
    assert field.delta_flag == "broker_optimistic"


from plat_agent.lifecycle.defaults import DEFAULT_LEVERAGE_V1, LEVERAGE_SOURCE_V1_HARDCODED
from plat_agent.lifecycle.judgment_rules import compute_leverage
import importlib


def test_leverage_v1_returns_hardcoded_with_source_marker() -> None:
    intake = _intake(in_place_rent=1300.0)
    lev = compute_leverage(intake)
    assert lev["ltv"] == DEFAULT_LEVERAGE_V1["ltv"]
    assert lev["rate"] == DEFAULT_LEVERAGE_V1["rate"]
    assert lev["amort_years"] == DEFAULT_LEVERAGE_V1["amort_years"]
    assert lev["io_months"] == DEFAULT_LEVERAGE_V1["io_months"]
    assert lev["source"] == LEVERAGE_SOURCE_V1_HARDCODED


def test_leverage_v1_ignores_intake_debt_section() -> None:
    # Even if intake carries debt terms, V1 ignores them (V2 will parse them)
    intake = dict(_intake(in_place_rent=1300.0))
    intake["debt"] = {"loans": [{"ltv": 0.80, "rate": 0.06, "amort_years": 25, "io_months": 0}]}
    lev = compute_leverage(intake)
    assert lev["ltv"] == DEFAULT_LEVERAGE_V1["ltv"]
    assert lev["source"] == LEVERAGE_SOURCE_V1_HARDCODED


def test_agency_leverage_activates_when_engine_path_set(monkeypatch) -> None:
    import plat_agent.lifecycle.judgment_rules as jr

    monkeypatch.setenv(
        "UNDERWRITING_ENGINE_PATH",
        "/path/to/projects/multifamily-underwriting",
    )
    importlib.reload(jr)

    intake = {
        "metadata": {"year_built": 2023},
        "purchase_assumptions": {"purchase_price": 60_000_000},
        "opex_table": {"t12_noi": 3_000_000},
        "broker_claims": {"year_1_noi": 3_500_000},
    }
    lev = jr.compute_leverage(intake)
    assert lev["source"] == "agency_dscr_constrained"
    assert lev["rate"] == 0.055
