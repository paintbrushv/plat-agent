import json
from pathlib import Path

import pytest

import plat_agent.lifecycle.judgment as judgment_module
from plat_agent.lifecycle.judgment import JudgmentStep, synchronize_judgment_provenance
from plat_agent.lifecycle.cache import compute_input_hash
from plat_agent.lifecycle.state import LifecycleState


def _write_intake(run_dir: Path) -> None:
    intake_dir = run_dir / "intake"
    intake_dir.mkdir(parents=True, exist_ok=True)
    canonical = {
        "metadata": {"address": "1 Test Way", "year_built": 1995,
                     "as_of_date": "2026-05-05", "analyst": "Lifecycle"},
        "unit_cohorts": [
            {"cohort_id": "1BR", "unit_count": 200, "in_place_rent": 1300.0,
             "market_rent": 1300.0},
        ],
        "purchase_assumptions": {"purchase_price": 25_000_000},
        "capex_assumptions": {"renovation_cost_per_unit": 12000.0},
        "growth_assumptions": {"rent_growth": 0.040},
        "exit_assumptions": {"exit_cap_rate": 0.055},
    }
    (intake_dir / "canonical_deal.json").write_text(json.dumps(canonical))
    # _complete + _provenance to satisfy upstream
    (intake_dir / "_complete").write_text(json.dumps({
        "step": "intake", "committed_at": "2026-05-05T00:00:00Z",
        "file_manifest": ["canonical_deal.json"],
    }))


def _write_comps(run_dir: Path) -> None:
    comps_dir = run_dir / "comps"
    comps_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "subject": {"address": "1 Test Way", "metro_slug": "dallas_tx"},
        "as_of": "2026-05-05",
        "comps": [
            {"comp_id": f"comp_{i}", "name": f"c{i}", "address": f"{i} St",
             "units": 200, "cap_rate_est": 0.058,
             "unit_types": [{"unit_type": "1BR", "effective_rent": 1500.0}]}
            for i in range(1, 5)
        ],
        "submarket_aggregates": {"rent_growth_trailing_12mo": 0.030},
    }
    (comps_dir / "comps.json").write_text(json.dumps(payload))


def test_step_name_is_judgment(tmp_path: Path) -> None:
    step = JudgmentStep()
    assert step.name == "judgment"


def test_step_run_writes_all_four_artifacts_and_complete(tmp_path: Path) -> None:
    _write_intake(tmp_path)
    _write_comps(tmp_path)
    state = LifecycleState(deal_slug="d", run_id="r", steps_completed=["intake", "comps"])
    result = JudgmentStep().run(state, tmp_path)
    assert result.status == "ok"
    j = tmp_path / "judgment"
    assert (j / "positioning.json").exists()
    assert (j / "thesis.md").exists()
    assert (j / "engine_inputs.json").exists()
    assert (j / "_provenance.json").exists()
    assert (j / "_complete").exists()


def test_step_positioning_json_round_trips(tmp_path: Path) -> None:
    _write_intake(tmp_path)
    _write_comps(tmp_path)
    state = LifecycleState(deal_slug="d", run_id="r", steps_completed=["intake", "comps"])
    JudgmentStep().run(state, tmp_path)
    payload = json.loads((tmp_path / "judgment" / "positioning.json").read_text())
    assert payload["positioning"]["value"] == "value_add"
    assert payload["leverage"]["source"] in {"v1_hardcoded", "agency_dscr_constrained"}
    assert payload["recommendation"] is None
    assert payload["recommendation_confidence"] is None


def test_step_run_when_comps_missing_emits_blocker(tmp_path: Path) -> None:
    _write_intake(tmp_path)
    state = LifecycleState(deal_slug="d", run_id="r", steps_completed=["intake"])
    result = JudgmentStep().run(state, tmp_path)
    assert result.status == "blocked"
    assert any(b.id == "comps_unavailable" for b in result.blockers)
    # Spec §2.3 has no top-level comps_unavailable field — the signal is in
    # positioning.json["blockers"].
    payload = json.loads((tmp_path / "judgment" / "positioning.json").read_text())
    assert any(b["id"] == "comps_unavailable" for b in payload["blockers"])


def test_step_run_uses_grouped_comps_when_comps_json_is_empty(tmp_path: Path) -> None:
    _write_intake(tmp_path)
    comps_dir = tmp_path / "comps"
    comps_dir.mkdir(parents=True, exist_ok=True)
    (comps_dir / "comps.json").write_text(json.dumps({"comps": []}))
    market_study_dir = tmp_path / "market_study"
    market_study_dir.mkdir(parents=True, exist_ok=True)
    grouped = {
        "as_of": "2026-05-05",
        "comps_by_cohort": {
            "1BR_1.0BA_720sf": [
                {
                    "property_name": "Comp A",
                    "asking_rent": 1500.0,
                    "sqft": 720,
                    "rent_per_sqft": 2.08,
                }
            ]
        },
    }
    (market_study_dir / "comps_cohort_grouped.json").write_text(json.dumps(grouped))

    state = LifecycleState(deal_slug="d", run_id="r", steps_completed=["intake", "comps"])
    result = JudgmentStep().run(state, tmp_path)

    assert result.status == "ok"
    payload = json.loads((tmp_path / "judgment" / "positioning.json").read_text())
    assert payload["positioning"]["value"] == "value_add"
    assert not any(b["id"] == "comps_unavailable" for b in payload["blockers"])


def test_step_run_when_intake_missing_returns_error(tmp_path: Path) -> None:
    state = LifecycleState(deal_slug="d", run_id="r", steps_completed=[])
    result = JudgmentStep().run(state, tmp_path)
    assert result.status == "error"
    assert result.error_message
    # _provenance should be written even on error
    assert (tmp_path / "judgment" / "_provenance.json").exists()


def test_step_provenance_includes_engine_name_and_delta_counts(tmp_path: Path) -> None:
    _write_intake(tmp_path)
    _write_comps(tmp_path)
    state = LifecycleState(deal_slug="d", run_id="r", steps_completed=["intake", "comps"])
    JudgmentStep().run(state, tmp_path)
    prov = json.loads((tmp_path / "judgment" / "_provenance.json").read_text())
    assert prov["judgment_engine"] == "deterministic_v1"
    assert "delta_flag_counts" in prov
    counts = prov["delta_flag_counts"]
    # Per spec §1 V1 Defaults: counts must use threshold severity (green/yellow/red)
    # NOT directional editorial flags. Keys are exactly red/yellow/green.
    assert set(counts.keys()) == {"red", "yellow", "green"}
    # Per spec §4.1 audit trail: actual blockers persisted (not just counts).
    assert "blockers" in prov
    assert isinstance(prov["blockers"], list)


def test_synchronize_judgment_provenance_recomputes_owner_artifact_contract(
    tmp_path: Path,
) -> None:
    _write_intake(tmp_path)
    _write_comps(tmp_path)
    state = LifecycleState(
        deal_slug="d", run_id="r", steps_completed=["intake", "comps"]
    )
    JudgmentStep().run(state, tmp_path)
    provenance_path = tmp_path / "judgment" / "_provenance.json"
    stale = json.loads(provenance_path.read_text())
    stale["input_hash"] = "stale"
    stale["broker_claims_validated"] = 0
    provenance_path.write_text(json.dumps(stale))
    pricing_dir = tmp_path / "judgment" / "pricing_policy"
    pricing_dir.mkdir()
    (pricing_dir / "backsolve_summary.json").write_text(
        json.dumps({"target_coc": 0.07, "search_ceiling_reached": False})
    )

    synchronized = synchronize_judgment_provenance(tmp_path)
    positioning = json.loads(
        (tmp_path / "judgment" / "positioning.json").read_text()
    )
    fields = [
        positioning[name]
        for name in (
            "capex_per_unit",
            "renovation_pace_units_per_month",
            "rent_growth",
            "exit_cap",
        )
    ]

    assert synchronized["input_hash"] == compute_input_hash(
        [
            tmp_path / "intake" / "canonical_deal.json",
            tmp_path / "comps" / "comps.json",
        ]
    )
    assert synchronized["broker_claims_validated"] == sum(
        field["om_claimed"] is not None for field in fields
    )
    complete = json.loads((tmp_path / "judgment" / "_complete").read_text())
    assert "pricing_policy/backsolve_summary.json" in complete["file_manifest"]


def test_step_is_satisfied_after_clean_run(tmp_path: Path) -> None:
    _write_intake(tmp_path)
    _write_comps(tmp_path)
    state = LifecycleState(deal_slug="d", run_id="r", steps_completed=["intake", "comps"])
    JudgmentStep().run(state, tmp_path)
    # Mark step complete in state (orchestrator does this in real flow)
    state.steps_completed.append("judgment")
    # Punchlist must exist for cache-check
    from plat_agent.lifecycle import write_punchlist_json
    write_punchlist_json(tmp_path, [])
    assert JudgmentStep().is_satisfied(state, tmp_path) is True


def test_step_is_satisfied_false_when_inputs_change(tmp_path: Path) -> None:
    _write_intake(tmp_path)
    _write_comps(tmp_path)
    state = LifecycleState(deal_slug="d", run_id="r", steps_completed=["intake", "comps"])
    JudgmentStep().run(state, tmp_path)
    state.steps_completed.append("judgment")
    from plat_agent.lifecycle import write_punchlist_json
    write_punchlist_json(tmp_path, [])
    # Mutate intake — input_hash should mismatch
    canonical_path = tmp_path / "intake" / "canonical_deal.json"
    payload = json.loads(canonical_path.read_text())
    payload["unit_cohorts"][0]["in_place_rent"] = 9999.0
    canonical_path.write_text(json.dumps(payload))
    assert JudgmentStep().is_satisfied(state, tmp_path) is False


def test_step_run_synthesizes_house_base_case_when_price_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_intake(tmp_path)
    _write_comps(tmp_path)
    intake_path = tmp_path / "intake" / "canonical_deal.json"
    intake = json.loads(intake_path.read_text())
    intake.pop("purchase_assumptions", None)
    intake["time_grid"] = {
        "analysis_start_date": "2026-06-01",
        "analysis_end_date": "2031-05-01",
    }
    intake_path.write_text(json.dumps(intake))

    def _fake_house_case(*, state, run_dir, engine_inputs):
        enriched = dict(engine_inputs)
        enriched["purchase_assumptions"] = {
            "purchase_price": 20_000_000.0,
            "equity_contribution": 7_000_000.0,
            "closing_costs": 300_000.0,
            "total_equity_basis": 7_650_000.0,
        }
        enriched["debt_terms"] = {
            "commitment": 13_000_000.0,
            "rate": 0.055,
            "amort_years": 30,
            "io_months": 36,
            "loan_start_month": "2026-06-01",
            "term_months": 60,
        }
        enriched["fund_assumptions"] = {
            "asset_management_fee_pct": 0.015,
            "annual_partnership_expenses": 25000.0,
        }
        enriched["pricing_provenance"] = {
            "strike_price": 20_000_000.0,
            "strike_price_basis": "analyst_target",
        }
        return enriched, {"house_base_case_applied": True}, None

    monkeypatch.setattr(
        judgment_module,
        "_maybe_synthesize_house_base_case",
        _fake_house_case,
    )

    state = LifecycleState(deal_slug="d", run_id="r", steps_completed=["intake", "comps"])
    result = JudgmentStep().run(state, tmp_path)
    assert result.status == "ok"

    engine_inputs = json.loads((tmp_path / "judgment" / "engine_inputs.json").read_text())
    assert engine_inputs["purchase_assumptions"]["purchase_price"] == 20_000_000.0
    assert engine_inputs["debt_terms"]["commitment"] == 13_000_000.0
    assert engine_inputs["fund_assumptions"]["asset_management_fee_pct"] == 0.015
    assert engine_inputs["pricing_provenance"]["strike_price"] == 20_000_000.0

    prov = json.loads((tmp_path / "judgment" / "_provenance.json").read_text())
    assert prov["house_base_case_applied"] is True
