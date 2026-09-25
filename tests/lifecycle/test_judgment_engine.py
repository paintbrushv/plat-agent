from typing import Protocol

import pytest

from plat_agent.lifecycle.judgment import (
    DeltaFlag,
    JudgmentEngine,
    JudgmentResult,
    PositioningClass,
    ValidatedField,
)
from plat_agent.lifecycle.state import BlockerItem


def test_validated_field_required_keys() -> None:
    f = ValidatedField(
        data_derived=18000.0,
        om_claimed=12000.0,
        selected=16000.0,
        confidence=0.7,
        delta_flag="broker_underestimates",
    )
    assert f.selected == 16000.0
    assert f.delta_flag == "broker_underestimates"


def test_validated_field_optional_om_claimed() -> None:
    # When OM doesn't make a claim (rare but allowed), om_claimed is None
    f = ValidatedField(
        data_derived=0.030,
        om_claimed=None,
        selected=0.030,
        confidence=0.6,
        delta_flag="green",
    )
    assert f.om_claimed is None
    assert f.delta_flag == "green"


def test_judgment_result_minimal_shape() -> None:
    res = JudgmentResult(
        positioning=PositioningClass(value="value_add", confidence=0.85, rationale="x"),
        leverage={
            "ltv": 0.65, "rate": 0.0575, "amort_years": 30, "io_months": 12,
            "source": "v1_hardcoded",
        },
        engine_inputs_relative="judgment/engine_inputs.json",
        blockers=[],
    )
    assert res.positioning.value == "value_add"
    # recommendation/recommendation_confidence are NOT produced here — orchestrator patches §3 Step 4.5
    assert res.recommendation is None
    assert res.recommendation_confidence is None


def test_judgment_result_serializes_to_positioning_json_shape() -> None:
    res = JudgmentResult(
        positioning=PositioningClass(value="stabilized", confidence=0.7, rationale="y"),
        capex_per_unit=ValidatedField(
            data_derived=8000.0, om_claimed=8500.0, selected=8000.0,
            confidence=0.7, delta_flag="green",
        ),
        leverage={
            "ltv": 0.65, "rate": 0.0575, "amort_years": 30, "io_months": 12,
            "source": "v1_hardcoded",
        },
        engine_inputs_relative="judgment/engine_inputs.json",
        blockers=[],
    )
    payload = res.model_dump(mode="json")
    # Per spec §2.3 schema
    assert payload["positioning"]["value"] == "stabilized"
    assert payload["capex_per_unit"]["selected"] == 8000.0
    assert payload["leverage"]["source"] == "v1_hardcoded"
    assert payload["engine_inputs_relative"] == "judgment/engine_inputs.json"
    assert payload["recommendation"] is None
    assert payload["recommendation_confidence"] is None


def test_judgment_engine_protocol_runtime_checkable() -> None:
    """Verify a class implementing the protocol type-checks."""

    class FakeEngine:
        name = "fake"

        def evaluate(self, intake: dict, comps: dict | None) -> JudgmentResult:
            return JudgmentResult(
                positioning=PositioningClass(value="stabilized", confidence=0.5, rationale="x"),
                leverage={
                    "ltv": 0.65, "rate": 0.0575, "amort_years": 30, "io_months": 12,
                    "source": "v1_hardcoded",
                },
                engine_inputs_relative="judgment/engine_inputs.json",
                blockers=[BlockerItem(
                    step="judgment",
                    id="comps_unavailable",
                    description="Comps step did not produce comps.json.",
                )],
            )

    engine: JudgmentEngine = FakeEngine()
    res = engine.evaluate({}, None)
    assert any(b.id == "comps_unavailable" for b in res.blockers)


def test_delta_flag_values() -> None:
    # Valid delta_flag values (spec §2.3 example list)
    valid = {"green", "yellow", "red", "broker_optimistic", "broker_underestimates"}
    for v in valid:
        ValidatedField(
            data_derived=1.0, om_claimed=1.0, selected=1.0, confidence=0.5,
            delta_flag=v,  # type: ignore[arg-type]
        )


from plat_agent.lifecycle.judgment import DeterministicJudgmentEngine


def _intake_value_add() -> dict:
    return {
        "metadata": {"address": "1 Test Way", "year_built": 1995},
        "unit_cohorts": [
            {"cohort_id": "1BR", "unit_count": 200, "in_place_rent": 1300.0,
             "market_rent": 1300.0},
        ],
        "purchase_assumptions": {"purchase_price": 25_000_000},
        "growth_assumptions": {"rent_growth": 0.040},
        "exit_assumptions": {"exit_cap_rate": 0.055},
        "capex_assumptions": {"renovation_cost_per_unit": 12000.0},
    }


def _comps_with_signals() -> dict:
    return {
        "subject": {"address": "1 Test Way", "metro_slug": "dallas_tx"},
        "as_of": "2026-05-05",
        "comps": [
            {
                "comp_id": f"comp_{i}",
                "name": f"Comp {i}",
                "address": f"{i} St",
                "units": 200,
                "cap_rate_est": 0.058,
                "unit_types": [{"unit_type": "1BR", "effective_rent": 1500.0, "sqft": 720}],
            }
            for i in range(1, 5)
        ],
        "submarket_aggregates": {"rent_growth_trailing_12mo": 0.030},
    }


def test_evaluate_value_add_full_field_set() -> None:
    engine = DeterministicJudgmentEngine()
    intake = _intake_value_add()
    comps = _comps_with_signals()
    res = engine.evaluate(intake, comps)
    assert res.positioning.value == "value_add"
    assert res.capex_per_unit is not None
    assert res.renovation_pace_units_per_month is not None
    assert res.rent_growth is not None
    assert res.exit_cap is not None
    assert res.leverage["source"] == "v1_hardcoded"
    assert res.engine_inputs_relative == "judgment/engine_inputs.json"
    # No comps_unavailable blocker when comps are present
    assert not any(b.id == "comps_unavailable" for b in res.blockers)
    assert res.recommendation is None  # orchestrator patches
    assert res.recommendation_confidence is None


def test_evaluate_flags_broker_optimistic_rent_growth() -> None:
    engine = DeterministicJudgmentEngine()
    intake = _intake_value_add()  # OM rent growth = 4% vs comp 3%
    comps = _comps_with_signals()
    res = engine.evaluate(intake, comps)
    assert res.rent_growth.delta_flag == "broker_optimistic"


def test_evaluate_flags_broker_underestimates_capex() -> None:
    engine = DeterministicJudgmentEngine()
    intake = _intake_value_add()  # OM capex = $12k vs data ~$24k
    comps = _comps_with_signals()
    res = engine.evaluate(intake, comps)
    assert res.capex_per_unit.delta_flag == "broker_underestimates"


def test_evaluate_comps_unavailable_emits_blocker() -> None:
    engine = DeterministicJudgmentEngine()
    intake = _intake_value_add()
    res = engine.evaluate(intake, comps=None)
    # Without comps the rent gap can't be computed → falls back to stabilized
    assert res.positioning.value == "stabilized"
    # Spec §2.3 has no top-level comps_unavailable field; the signal is a blocker.
    assert any(b.id == "comps_unavailable" for b in res.blockers)
    # Confidence on positioning should be reduced
    assert res.positioning.confidence < 0.6


def test_evaluate_provenance_compatible_engine_name() -> None:
    engine = DeterministicJudgmentEngine()
    assert engine.name == "deterministic_v1"
