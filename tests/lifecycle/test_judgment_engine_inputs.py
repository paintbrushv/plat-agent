from plat_agent.lifecycle.judgment import (
    _solver_benchmark_from_engine_inputs,
    _validate_pricing_seed_before_solver,
    JudgmentResult,
    PositioningClass,
    ValidatedField,
    build_engine_inputs,
)


def _intake() -> dict:
    return {
        "metadata": {"address": "1 Test Way", "year_built": 1995,
                     "market": "dallas_tx", "as_of_date": "2026-05-05", "analyst": "Lifecycle",
                     "deal_id": "Test Deal", "run_id": "run_001", "purpose": "Document Ingestion"},
        "time_grid": {
            "analysis_start_date": "2026-06-01",
            "analysis_end_date": "2031-05-01",
        },
        "unit_cohorts": [
            {"cohort_id": "1BR", "unit_count": 200, "in_place_rent": 1300.0,
             "market_rent": 1300.0},
        ],
        "purchase_assumptions": {"purchase_price": 25_000_000},
    }


def _result() -> JudgmentResult:
    return JudgmentResult(
        positioning=PositioningClass(value="value_add", confidence=0.85, rationale="x"),
        capex_per_unit=ValidatedField(
            data_derived=24000.0, om_claimed=12000.0, selected=24000.0,
            confidence=0.7, delta_flag="broker_underestimates",
        ),
        renovation_pace_units_per_month=ValidatedField(
            data_derived=5.0, om_claimed=None, selected=5.0,
            confidence=0.65, delta_flag="green",
        ),
        rent_growth=ValidatedField(
            data_derived=0.030, om_claimed=0.040, selected=0.030,
            confidence=0.75, delta_flag="broker_optimistic",
        ),
        exit_cap=ValidatedField(
            data_derived=0.058, om_claimed=0.055, selected=0.058,
            confidence=0.7, delta_flag="broker_optimistic",
        ),
        leverage={"ltv": 0.65, "rate": 0.0575, "amort_years": 30, "io_months": 12,
                  "source": "v1_hardcoded"},
        engine_inputs_relative="judgment/engine_inputs.json",
        blockers=[],
    )


def test_engine_inputs_carries_intake_metadata_through() -> None:
    out = build_engine_inputs(_intake(), _result())
    assert out["metadata"]["deal_id"] == "Test Deal"
    assert out["metadata"]["address"] == "1 Test Way"
    assert out["metadata"]["market"] == "dallas_tx"
    assert out["metadata"]["year_built"] == 1995
    assert out["unit_cohorts"][0]["unit_count"] == 200


def test_engine_inputs_writes_selected_capex() -> None:
    out = build_engine_inputs(_intake(), _result())
    assert "capex_assumptions" not in out


def test_engine_inputs_writes_selected_rent_growth() -> None:
    out = build_engine_inputs(_intake(), _result())
    assert out["growth_assumptions"]["annual_growth_rate"] == 0.030
    assert out["growth_assumptions"]["growth_type"] == "annual_compound"
    assert "rent_growth" not in out["growth_assumptions"]


def test_engine_inputs_writes_selected_exit_cap() -> None:
    out = build_engine_inputs(_intake(), _result())
    assert out["exit_assumptions"]["exit_cap_rate"] == 0.058
    assert out["exit_assumptions"]["exit_month"] == "2031-05-01"


def test_engine_inputs_writes_leverage() -> None:
    out = build_engine_inputs(_intake(), _result())
    debt = out["debt_terms"]
    assert debt["rate"] == 0.0575
    assert debt["amort_years"] == 30
    assert debt["io_months"] == 12
    assert debt["commitment"] == 16_250_000.0
    assert debt["loan_start_month"] == "2026-06-01"


def test_engine_inputs_omits_capex_section_when_judgment_lacks_capex_field() -> None:
    res = _result()
    res.capex_per_unit = None
    res.renovation_pace_units_per_month = None
    out = build_engine_inputs(_intake(), res)
    assert "capex_assumptions" not in out


def test_engine_inputs_normalizes_legacy_growth_and_debt_shapes() -> None:
    intake = _intake()
    intake["growth_assumptions"] = {"rent_growth": 0.04}
    intake["debt"] = {"loans": [{"commitment": 1.0}]}
    out = build_engine_inputs(intake, _result())
    assert out["growth_assumptions"]["annual_growth_rate"] == 0.03
    assert "rent_growth" not in out["growth_assumptions"]
    assert "debt" not in out


def test_engine_inputs_uses_zero_commitment_when_price_missing() -> None:
    intake = _intake()
    intake.pop("purchase_assumptions", None)
    out = build_engine_inputs(intake, _result())
    assert out["debt_terms"]["commitment"] == 0.0


def test_solver_benchmark_reads_hold_matched_debt_guidance_when_present() -> None:
    intake = _intake()
    intake["metadata"]["property_summary"] = {
        "debt_guidance": {
            "hold_matched_recommendation": {
                "loan_option": "Agency 5 Year Fixed",
                "benchmark_rate": 0.0391,
            }
        }
    }
    assert _solver_benchmark_from_engine_inputs(intake) == "0.0391"


def test_pricing_seed_preflight_allows_normal_workforce_opex_stack() -> None:
    engine_inputs = {
        "unit_cohorts": [
            {"cohort_id": "1BR", "unit_count": 34, "in_place_rent": 771.18},
            {"cohort_id": "2BR", "unit_count": 27, "in_place_rent": 851.82},
        ],
        "opex_table": [
            {"category_name": "Insurance", "base_value": 46644.0},
            {"category_name": "Repairs & Maintenance", "base_value": 47516.83},
            {"category_name": "Utilities", "base_value": 64724.0},
            {"category_name": "Total", "base_value": 229422.32},
        ],
    }

    _validate_pricing_seed_before_solver(engine_inputs)
