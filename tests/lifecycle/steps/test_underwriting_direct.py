from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

from plat_agent.contracts.envelope import BridgeRequestV1
from plat_agent.dispatch.sibling import SiblingRepo
from plat_agent.lifecycle.state import LifecycleState
from plat_agent.lifecycle.steps.underwriting import (
    UnderwritingStep,
    _compute_engine_inputs_hash,
    dispatch_sibling_agent,
)


def _write_engine_inputs(run_dir: Path) -> None:
    judgment_dir = run_dir / "judgment"
    judgment_dir.mkdir(parents=True, exist_ok=True)
    (judgment_dir / "engine_inputs.json").write_text(
        json.dumps(
            {
                "metadata": {
                    "deal_id": "Willow Court",
                    "run_id": "run_001",
                    "as_of_date": "2026-06-01",
                    "analyst": "plat-agent",
                    "purpose": "Document Ingestion",
                },
                "time_grid": {
                    "analysis_start_date": "2026-06-01",
                    "analysis_end_date": "2031-05-01",
                },
                "unit_cohorts": [{"cohort_id": "A1", "unit_count": 10}],
                "market_rent_curve": [],
                "loss_to_lease": [],
                "physical_vacancy_curve": [],
                "collection_loss_curve": [],
                "revenue_programs": [],
                "program_adoption_curve": [],
                "opex_table": [],
                "capex_table": [],
                "exit_assumptions": {
                    "exit_cap_rate": 0.06,
                    "exit_month": "2031-05-01",
                },
                "growth_assumptions": {
                    "growth_type": "annual_compound",
                    "annual_growth_rate": 0.03,
                },
                "purchase_assumptions": {
                    "purchase_price": 25000000,
                    "equity_contribution": 10000000,
                },
                "debt_terms": {
                    "commitment": 15000000,
                    "rate": 0.055,
                    "amort_years": 30,
                },
            }
        )
    )


def test_underwriting_direct_subprocess_default_path_runs_without_llm_dispatch(
    tmp_path: Path,
) -> None:
    state = LifecycleState(deal_slug="willow_court", run_id="run_001")
    deal_root = tmp_path / "deals" / "willow_court"
    run_dir = deal_root / "outputs" / "run_001"
    _write_engine_inputs(run_dir)

    repo_root = tmp_path / "mfu"
    (repo_root / "runs").mkdir(parents=True)
    (repo_root / "runs" / "federated_underwrite.py").write_text("# stub\n")

    captured = {}

    def fake_run(cmd, cwd, capture_output, text, timeout):
        captured["cmd"] = list(cmd)
        captured["cwd"] = cwd
        underwriting_dir = run_dir / "underwriting"
        underwriting_dir.mkdir(parents=True, exist_ok=True)
        (underwriting_dir / "deal_summary.json").write_text(
            json.dumps({"metrics": {"coc": {"cash_on_cash_year_1": 0.061}}})
        )
        engine_inputs = json.loads(
            (run_dir / "judgment" / "engine_inputs.json").read_text()
        )
        (underwriting_dir / "inputs.json").write_text(json.dumps(engine_inputs))
        (underwriting_dir / "_provenance.json").write_text(
            json.dumps(
                {
                    "engine_version": "0.1.0",
                    "schema_version": "0.1",
                    "inputs_hash_sha256": _compute_engine_inputs_hash(engine_inputs),
                    "generated_at_utc": "2026-07-20T00:00:00Z",
                    "validator_status": "PASS",
                    "validator_issues": [],
                    "feasibility_verdict": "pass",
                    "feasibility_sanity_flags": [],
                    "feasibility_reasons": [],
                    "cap_rate_derivation": None,
                }
            )
        )
        (underwriting_dir / "_response_envelope.json").write_text("{}")
        envelope = {
            "contract_version": "v1",
            "status": "ok",
            "deal_slug": "willow_court",
            "run_id": "run_001",
            "agent_name": "underwriting-runner",
            "payload": {
                "metrics": {
                    "levered_irr": 0.12,
                    "equity_multiple": 1.5,
                    "min_dscr": 1.25,
                    "avg_dscr": 1.3,
                    "going_in_cap": 0.05,
                    "exit_cap": 0.06,
                },
                "feasibility_verdict": "pass",
                "feasibility_reasons": [],
                "summary_relative": "outputs/run_001/underwriting/deal_summary.json",
                "workbook_relative": None,
            },
            "artifacts": [
                {
                    "relative_path": "outputs/run_001/underwriting/deal_summary.json",
                    "kind": "json",
                    "description": "summary",
                }
            ],
            "provenance": [],
            "error": None,
            "sanity_flags": [],
        }
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(envelope), stderr="")

    with patch("plat_agent.lifecycle.steps.underwriting.subprocess.run", side_effect=fake_run):
        step = UnderwritingStep(sibling_repo=SiblingRepo(name="mfu", path=repo_root))
        result = step.run(state, run_dir)

    assert result.status == "ok"
    assert captured["cwd"] == str(repo_root)
    assert str(repo_root / "runs" / "federated_underwrite.py") in captured["cmd"]
    assert "--canonical-relative" in captured["cmd"]
    assert "outputs/run_001/judgment/engine_inputs.json" in captured["cmd"]


def test_underwriting_direct_subprocess_timeout_surfaces_configured_seconds(
    tmp_path: Path,
    monkeypatch,
) -> None:
    state = LifecycleState(deal_slug="willow_court", run_id="run_001")
    deal_root = tmp_path / "deals" / "willow_court"
    run_dir = deal_root / "outputs" / "run_001"
    _write_engine_inputs(run_dir)

    repo_root = tmp_path / "mfu"
    (repo_root / "runs").mkdir(parents=True)
    (repo_root / "runs" / "federated_underwrite.py").write_text("# stub\n")
    monkeypatch.setenv("PLAT_UNDERWRITING_TIMEOUT_SECONDS", "7")

    captured = {}

    def fake_run(cmd, cwd, capture_output, text, timeout):
        captured["timeout"] = timeout
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=timeout)

    with patch("plat_agent.lifecycle.steps.underwriting.subprocess.run", side_effect=fake_run):
        step = UnderwritingStep(sibling_repo=SiblingRepo(name="mfu", path=repo_root))
        result = step.run(state, run_dir)

    assert result.status == "error"
    assert "underwriting-runner dispatch failed" in (result.error_message or "")
    assert "timed out after 7s" in (result.error_message or "")
    assert captured["timeout"] == 7


def test_underwriting_direct_wrapper_repairs_list_error_details(tmp_path: Path) -> None:
    state = LifecycleState(deal_slug="willow_court", run_id="run_001")
    deal_root = tmp_path / "deals" / "willow_court"
    run_dir = deal_root / "outputs" / "run_001"
    _write_engine_inputs(run_dir)

    repo_root = tmp_path / "mfu"
    (repo_root / "runs").mkdir(parents=True)
    (repo_root / "runs" / "federated_underwrite.py").write_text("# stub\n")
    step = UnderwritingStep()
    request = step._build_request(state, run_dir=run_dir)

    malformed_error = {
        "contract_version": "v1",
        "status": "error",
        "deal_slug": "willow_court",
        "run_id": "run_001",
        "agent_name": "underwriting-runner",
        "payload": None,
        "artifacts": [],
        "provenance": [],
        "error": {
            "code": "validation_failed",
            "details": [
                {"path": "/metadata", "message": "bad metadata", "code": "SCHEMA"}
            ],
        },
        "sanity_flags": [],
    }

    with patch(
        "plat_agent.lifecycle.steps.underwriting.subprocess.run",
        return_value=subprocess.CompletedProcess([], 0, stdout=json.dumps(malformed_error), stderr=""),
    ):
        response = dispatch_sibling_agent(
            repo=SiblingRepo(name="mfu", path=repo_root),
            request=request,
            payload_model=None,
        )

    assert response.status == "error"
    assert response.error is not None
    assert response.error.code == "validation_failed"
    assert response.error.details["issues"][0]["message"] == "bad metadata"
