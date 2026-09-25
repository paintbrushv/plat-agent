import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from plat_agent.lifecycle.cache import write_provenance
from plat_agent.lifecycle.complete_marker import read_complete_marker
from plat_agent.lifecycle.state import LifecycleState
from plat_agent.contracts.domain.underwriting import UnderwritingProvenance
from plat_agent.lifecycle.steps.underwriting import (
    UnderwritingStep,
    _compute_engine_inputs_hash,
    synchronize_underwriting_provenance,
)


def _setup_judgment_inputs(run_dir: Path) -> None:
    """Create judgment/engine_inputs.json + _complete so step has upstream."""
    judgment_dir = run_dir / "judgment"
    judgment_dir.mkdir(parents=True)
    (judgment_dir / "engine_inputs.json").write_text(json.dumps({
        "metadata": {"address": "1 Test Way", "year_built": 2005},
        "unit_cohorts": [{"cohort_id": "c1", "unit_count": 100}],
        "purchase_assumptions": {"purchase_price": 25000000},
    }))
    (judgment_dir / "_complete").write_text(json.dumps({
        "step": "judgment", "committed_at": "2026-05-05T00:00:00Z",
        "file_manifest": ["engine_inputs.json"],
    }))


def test_underwriting_step_name() -> None:
    step = UnderwritingStep()
    assert step.name == "underwriting"


def test_underwriting_step_run_writes_deal_summary(tmp_path: Path) -> None:
    _setup_judgment_inputs(tmp_path)
    fake_client = MagicMock()
    fake_client.run_summary.return_value = {
        "metrics": {
            "irr": {"levered_irr": 0.15},
            "equity_multiple": {"levered_em": 1.78},
            "yields": {"going_in_cap_rate": 0.052, "exit_cap_rate": 0.058},
            "dscr": {"average_dscr": 1.45, "minimum_dscr": 1.22},
        },
        "sanity_flags": [],
        "feasibility_verdict": "pass",
    }
    step = UnderwritingStep(client=fake_client)
    state = LifecycleState(deal_slug="d", run_id="r", steps_completed=["intake", "comps", "judgment"])

    result = step.run(state, tmp_path)

    assert result.status == "ok"
    summary_path = tmp_path / "underwriting" / "deal_summary.json"
    assert summary_path.exists()
    payload = json.loads(summary_path.read_text())
    assert payload["metrics"]["irr"]["levered_irr"] == 0.15


def test_underwriting_step_writes_complete_marker(tmp_path: Path) -> None:
    _setup_judgment_inputs(tmp_path)
    fake_client = MagicMock()
    fake_client.run_summary.return_value = {
        "metrics": {
            "irr": {"levered_irr": 0.15},
            "equity_multiple": {"levered_em": 1.78},
            "yields": {"going_in_cap_rate": 0.052},
            "dscr": {"minimum_dscr": 1.22},
        },
        "sanity_flags": [],
    }
    step = UnderwritingStep(client=fake_client)
    state = LifecycleState(deal_slug="d", run_id="r")

    step.run(state, tmp_path)

    marker = read_complete_marker(tmp_path / "underwriting")
    assert marker is not None
    assert marker.step == "underwriting"
    assert "deal_summary.json" in marker.file_manifest
    assert "_provenance.json" in marker.file_manifest


def test_underwriting_step_returns_error_on_client_exception(tmp_path: Path) -> None:
    _setup_judgment_inputs(tmp_path)
    fake_client = MagicMock()
    fake_client.run_summary.side_effect = RuntimeError("engine boom")
    step = UnderwritingStep(client=fake_client)
    state = LifecycleState(deal_slug="d", run_id="r")

    result = step.run(state, tmp_path)

    assert result.status == "error"
    assert "engine boom" in (result.error_message or "")
    # _provenance.json with status=error must still exist for dependency-map
    prov = json.loads((tmp_path / "underwriting" / "_provenance.json").read_text())
    assert prov["status"] == "error"


def test_underwriting_step_dispatches_to_federated_runner(tmp_path: Path) -> None:
    """When no `client` is injected, UnderwritingStep MUST dispatch to the
    federated underwriting-runner (not the legacy MCP path). Verifies fix
    for known-issues-v1 P1 #3.
    """
    from plat_agent.contracts.envelope import BridgeResponseV1

    deal_root = tmp_path / "deals" / "d"
    run_dir = deal_root / "outputs" / "r"
    run_dir.mkdir(parents=True)
    _setup_judgment_inputs(run_dir)

    # Sibling-emitted artifacts at the canonical run-scoped location.
    underwriting_dir = run_dir / "underwriting"
    underwriting_dir.mkdir()
    (underwriting_dir / "deal_summary.json").write_text(json.dumps({
        "metrics": {
            "irr": {"levered_irr": 0.12, "unlevered_irr": 0.10},
            "equity_multiple": {"levered_em": 1.65, "unlevered_em": 1.40},
            "dscr": {"minimum_dscr": 1.31, "average_dscr": 1.45},
            "yields": {"going_in_cap_rate": 0.058, "exit_cap_rate": 0.062},
        },
    }))
    engine_provenance = {
        "engine_version": "0.1.0",
        "schema_version": "0.1",
        "inputs_hash_sha256": _compute_engine_inputs_hash(
            json.loads((run_dir / "judgment" / "engine_inputs.json").read_text())
        ),
        "generated_at_utc": "2026-07-20T00:00:00Z",
        "validator_status": "PASS",
        "validator_issues": [],
        "feasibility_verdict": "marginal",
        "feasibility_sanity_flags": ["irr_below_12pct_hurdle"],
        "feasibility_reasons": ["below 12pct hurdle"],
        "cap_rate_derivation": None,
    }
    (underwriting_dir / "inputs.json").write_text(
        (run_dir / "judgment" / "engine_inputs.json").read_text()
    )
    (underwriting_dir / "_provenance.json").write_text(
        json.dumps(engine_provenance)
    )
    (underwriting_dir / "_response_envelope.json").write_text("{}")

    captured = {}

    def fake_dispatch(repo, request, *, payload_model=None, **kwargs):
        captured["agent_name"] = request.agent_name
        captured["payload_model"] = payload_model
        captured["payload"] = request.payload
        return BridgeResponseV1(
            status="ok",
            deal_slug=request.deal_slug,
            run_id=request.run_id,
            agent_name="underwriting-runner",
            payload={
                "metrics": {
                    "levered_irr": 0.12,
                    "equity_multiple": 1.65,
                    "min_dscr": 1.31,
                    "avg_dscr": 1.45,
                    "going_in_cap": 0.058,
                    "exit_cap": 0.062,
                },
                "feasibility_verdict": "marginal",
                "feasibility_reasons": ["below 12pct hurdle"],
                "summary_relative": "outputs/r/underwriting/deal_summary.json",
                "workbook_relative": None,
            },
        )

    state = LifecycleState(deal_slug="d", run_id="r")
    step = UnderwritingStep(dispatcher=fake_dispatch)
    result = step.run(state, run_dir)

    assert result.status == "ok", result.error_message
    assert captured["agent_name"] == "underwriting-runner"
    # Lifecycle re-stamps _provenance.json with cache-validity inputs.
    prov = json.loads((underwriting_dir / "_provenance.json").read_text())
    assert "input_hash" in prov
    assert prov["status"] == "ok"
    # And surfaces feasibility_sanity_flags from the federated provenance.
    assert "irr_below_12pct_hurdle" in prov.get("feasibility_sanity_flags", [])
    assert prov["inputs_hash_sha256"] == _compute_engine_inputs_hash(
        json.loads((run_dir / "judgment" / "engine_inputs.json").read_text())
    )
    assert prov["federated_provenance"] == engine_provenance
    UnderwritingProvenance.model_validate(prov)
    marker = read_complete_marker(underwriting_dir)
    assert marker is not None
    assert "inputs.json" in marker.file_manifest

def test_synchronize_underwriting_provenance_rejects_semantic_input_drift(
    tmp_path: Path,
) -> None:
    _setup_judgment_inputs(tmp_path)
    underwriting_dir = tmp_path / "underwriting"
    underwriting_dir.mkdir()
    judgment_inputs = json.loads(
        (tmp_path / "judgment" / "engine_inputs.json").read_text()
    )
    stale_inputs = dict(judgment_inputs)
    stale_inputs["purchase_assumptions"] = {"purchase_price": 1.0}
    (underwriting_dir / "inputs.json").write_text(json.dumps(stale_inputs))
    (underwriting_dir / "deal_summary.json").write_text("{}")
    (underwriting_dir / "_response_envelope.json").write_text("{}")
    (underwriting_dir / "_provenance.json").write_text(
        json.dumps(
            {
                "engine_version": "0.1.0",
                "schema_version": "0.1",
                "inputs_hash_sha256": _compute_engine_inputs_hash(stale_inputs),
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

    with pytest.raises(ValueError, match="does not semantically match"):
        synchronize_underwriting_provenance(tmp_path)

    assert not (underwriting_dir / "_complete").exists()



def test_underwriting_step_dispatch_error_writes_provenance(tmp_path: Path) -> None:
    from plat_agent.contracts.envelope import BridgeError, BridgeResponseV1

    deal_root = tmp_path / "deals" / "d"
    run_dir = deal_root / "outputs" / "r"
    run_dir.mkdir(parents=True)
    _setup_judgment_inputs(run_dir)

    def fake_dispatch(repo, request, *, payload_model=None, **kwargs):
        return BridgeResponseV1(
            status="error",
            deal_slug=request.deal_slug,
            run_id=request.run_id,
            agent_name="underwriting-runner",
            error=BridgeError(code="validation_failed", message="boom"),
        )

    state = LifecycleState(deal_slug="d", run_id="r")
    step = UnderwritingStep(dispatcher=fake_dispatch)
    result = step.run(state, run_dir)

    assert result.status == "error"
    assert "boom" in (result.error_message or "")
    prov = json.loads((run_dir / "underwriting" / "_provenance.json").read_text())
    assert prov["status"] == "error"


def test_underwriting_step_is_satisfied_uses_cache(tmp_path: Path) -> None:
    _setup_judgment_inputs(tmp_path)
    # Pre-populate underwriting outputs as if a prior run completed
    uw = tmp_path / "underwriting"
    uw.mkdir()
    (uw / "deal_summary.json").write_text("{}")
    from plat_agent.lifecycle.complete_marker import write_complete_marker
    from plat_agent.lifecycle.cache import compute_input_hash, write_provenance
    from plat_agent.lifecycle.defaults import CONTRACT_VERSION
    from plat_agent.lifecycle.punchlist import write_punchlist_json

    write_complete_marker(uw, step="underwriting",
                          file_manifest=["deal_summary.json", "_provenance.json"])
    h = compute_input_hash([tmp_path / "judgment" / "engine_inputs.json"])
    write_provenance(uw, input_hash=h, contract_version=CONTRACT_VERSION)
    (uw / "_provenance.json")  # already written above
    write_punchlist_json(tmp_path, [])
    state = LifecycleState(deal_slug="d", run_id="r",
                           steps_completed=["intake", "comps", "judgment", "underwriting"])

    step = UnderwritingStep(client=MagicMock())
    assert step.is_satisfied(state, tmp_path) is True


def test_underwriting_step_handles_none_payload_gracefully(tmp_path: Path) -> None:
    """V1.4 — when underwriting-runner returns payload=None (e.g. status=
    'needs_analyst_input' or 'ok' with nothing to report), UnderwritingStep
    MUST NOT crash with a pydantic ValidationError. It should write a
    structured _provenance.json (status=error, code=empty_payload_response)
    and return StepResult(status='error') with an actionable message.
    """
    from plat_agent.contracts.envelope import BridgeError, BridgeResponseV1

    deal_root = tmp_path / "deals" / "d"
    run_dir = deal_root / "outputs" / "r"
    run_dir.mkdir(parents=True)
    _setup_judgment_inputs(run_dir)

    def fake_dispatch(repo, request, *, payload_model=None, **kwargs):
        return BridgeResponseV1(
            status="needs_analyst_input",
            deal_slug=request.deal_slug,
            run_id=request.run_id,
            agent_name="underwriting-runner",
            payload=None,
            error=BridgeError(
                code="missing_engine_inputs_field",
                message="purchase_assumptions.exit_cap_rate not set",
            ),
        )

    state = LifecycleState(deal_slug="d", run_id="r")
    step = UnderwritingStep(dispatcher=fake_dispatch)
    result = step.run(state, run_dir)

    assert result.status == "error"
    assert "no payload" in (result.error_message or "").lower()
    prov_path = run_dir / "underwriting" / "_provenance.json"
    assert prov_path.exists()
    prov = json.loads(prov_path.read_text())
    assert prov["status"] == "error"
    assert prov["error_code"] == "empty_payload_response"
