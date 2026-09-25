"""Integration: full JudgmentStep against a fixture canonical_deal + comps."""

import json
from pathlib import Path


def test_public_api_exports_resolve() -> None:
    from plat_agent import lifecycle
    expected = {
        "DeltaFlag",
        "DeterministicJudgmentEngine",
        "JudgmentEngine",
        "JudgmentResult",
        "JudgmentStep",
        "PositioningClass",
        "PositioningValue",
        "ValidatedField",
        "build_engine_inputs",
        "render_thesis",
    }
    for name in expected:
        assert hasattr(lifecycle, name), f"{name} missing from lifecycle.__all__"


def test_full_step_produces_contract_compliant_positioning_json(tmp_path: Path) -> None:
    """Fixture canonical_deal + comps → positioning.json passes the §2.3 schema."""
    from plat_agent.lifecycle import JudgmentStep, LifecycleState

    intake_dir = tmp_path / "intake"
    comps_dir = tmp_path / "comps"
    intake_dir.mkdir()
    comps_dir.mkdir()

    (intake_dir / "canonical_deal.json").write_text(json.dumps({
        "metadata": {"address": "1 Test Way", "year_built": 1995,
                     "as_of_date": "2026-05-05", "analyst": "Lifecycle"},
        "unit_cohorts": [
            {"cohort_id": "1BR", "unit_count": 200, "in_place_rent": 1300.0,
             "market_rent": 1300.0},
        ],
        "purchase_assumptions": {"purchase_price": 25_000_000},
        "growth_assumptions": {"rent_growth": 0.040},
        # OM exit cap 0.045 vs comp-derived 0.058 → ~22% delta (yellow band)
        # AND OM is lower than data → broker_optimistic per §2.3 directional rule.
        "exit_assumptions": {"exit_cap_rate": 0.045},
        "capex_assumptions": {"renovation_cost_per_unit": 12000.0},
    }))
    (comps_dir / "comps.json").write_text(json.dumps({
        "subject": {"address": "1 Test Way", "metro_slug": "dallas_tx"},
        "as_of": "2026-05-05",
        "comps": [
            {"comp_id": f"c{i}", "name": f"C{i}", "address": f"{i} St",
             "units": 200, "cap_rate_est": 0.058,
             "unit_types": [{"unit_type": "1BR", "effective_rent": 1500.0}]}
            for i in range(1, 5)
        ],
        "submarket_aggregates": {"rent_growth_trailing_12mo": 0.030},
    }))

    state = LifecycleState(deal_slug="project_essex", run_id="run_002",
                           steps_completed=["intake", "comps"])
    res = JudgmentStep().run(state, tmp_path)
    assert res.status == "ok"

    pos = json.loads((tmp_path / "judgment" / "positioning.json").read_text())
    # Required fields per §2.3
    assert "positioning" in pos and pos["positioning"]["value"] in {"value_add", "stabilized"}
    assert pos["positioning"]["confidence"] >= 0.0
    assert pos["leverage"]["ltv"] is not None
    assert pos["leverage"]["source"] == "v1_hardcoded"
    assert pos["engine_inputs_relative"] == "judgment/engine_inputs.json"
    assert pos["recommendation"] is None  # orchestrator patches in §3 Step 4.5
    assert pos["recommendation_confidence"] is None
    # Broker-claim deltas are populated
    assert pos["capex_per_unit"]["delta_flag"] == "broker_underestimates"
    assert pos["rent_growth"]["delta_flag"] == "broker_optimistic"
    assert pos["exit_cap"]["delta_flag"] == "broker_optimistic"


def test_full_step_thesis_md_is_nonempty_markdown(tmp_path: Path) -> None:
    from plat_agent.lifecycle import JudgmentStep, LifecycleState
    intake_dir = tmp_path / "intake"
    intake_dir.mkdir()
    (intake_dir / "canonical_deal.json").write_text(json.dumps({
        "metadata": {"address": "1 Test Way", "year_built": 1995,
                     "as_of_date": "2026-05-05", "analyst": "Lifecycle"},
        "unit_cohorts": [{"cohort_id": "1BR", "unit_count": 200,
                          "in_place_rent": 1300.0, "market_rent": 1300.0}],
        "purchase_assumptions": {"purchase_price": 25_000_000},
    }))
    state = LifecycleState(deal_slug="d", run_id="r", steps_completed=["intake"])
    JudgmentStep().run(state, tmp_path)
    md = (tmp_path / "judgment" / "thesis.md").read_text()
    assert md.startswith("# Investment Thesis")
    assert "comps unavailable" in md.lower()
