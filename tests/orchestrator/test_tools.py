import asyncio
import json
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from plat_agent.orchestrator.tools import (
    compare_scenarios_tool,
    load_deal_inputs_tool,
    run_cost_model_tool,
    run_underwriting_tool,
)


def _run(c):
    return asyncio.run(c)


# ---------------------------------------------------------------------------
# Existing input-validation behavior (preserved)
# ---------------------------------------------------------------------------

def test_run_cost_model_returns_summary(monkeypatch):
    async def fake_async_call_tool(self, tool_name, arguments):
        return {"total_renovation_low": 1000000, "total_renovation_high": 1500000}

    monkeypatch.setattr(
        "plat_agent.costmodel_client.CostModelClient.async_call_tool",
        fake_async_call_tool,
    )
    result = _run(run_cost_model_tool.handler({"deal_id": "TEST-001", "scenario": "base"}))
    text = result["content"][0]["text"]
    assert "deal_id=TEST-001" in text


def test_run_underwriting_requires_deal_id():
    result = _run(run_underwriting_tool.handler({"deal_id": ""}))
    assert result.get("is_error") is True


def test_compare_scenarios_emits_delta():
    result = _run(compare_scenarios_tool.handler({
        "cost_model_summary": "irr=0.12 noi=1000000",
        "underwriting_summary": "irr=0.15 noi=1100000",
    }))
    assert "delta" in result["content"][0]["text"].lower()


# ---------------------------------------------------------------------------
# Phase-7 wiring: run_cost_model_tool delegates to CostModelClient
# ---------------------------------------------------------------------------

def test_run_cost_model_calls_costmodel_client_default_tool(monkeypatch):
    """The default cost-model tool is estimate_from_deal — it consumes the
    canonical multifamily-underwriting deal shape that load_deal_inputs
    returns, so the orchestrator can pass that shape verbatim. The
    orchestrator wraps the deal as ``{"deal_dict": <deal>}`` to match the
    MCP tool's named-arg signature."""
    captured = {}

    async def fake_async_call_tool(self, tool_name, arguments):
        captured["tool_name"] = tool_name
        captured["arguments"] = arguments
        return {
            "estimates": [{"cohort_total_low": 250000, "cohort_total_high": 300000}],
            "renovation_programs": [{"renovation_cost_per_unit": 14000}],
        }

    monkeypatch.setattr(
        "plat_agent.costmodel_client.CostModelClient.async_call_tool",
        fake_async_call_tool,
    )
    deal_inputs = {
        "schema_version": "0.1",
        "property": {"external_alias": "p", "total_units": 10},
        "unit_cohorts": [{"avg_sqft": 850, "avg_bedrooms": 2, "avg_bathrooms": 1,
                          "unit_count": 10, "current_avg_rent": 850}],
        "renovation_programs": [{"cohort_index": 0, "rent_premium_monthly": 200,
                                 "start_month": "2026-06", "monthly_pace": 5}],
    }
    result = _run(run_cost_model_tool.handler({
        "deal_id": "D1",
        "scenario": "base",
        "inputs": deal_inputs,
    }))

    assert captured["tool_name"] == "estimate_from_deal"
    # The deal is wrapped under "deal_dict" to match the MCP tool signature.
    assert captured["arguments"] == {"deal_dict": deal_inputs}
    text = result["content"][0]["text"]
    assert "deal_id=D1" in text
    assert "scenario=base" in text


def test_run_cost_model_does_not_double_wrap_pre_wrapped_inputs(monkeypatch):
    """If the caller already wrapped inputs as {"deal_dict": ...} (e.g. a
    legacy caller that knew the parameter name), don't wrap again."""
    captured = {}

    async def fake_async_call_tool(self, tool_name, arguments):
        captured["arguments"] = arguments
        return {"estimates": [], "renovation_programs": []}

    monkeypatch.setattr(
        "plat_agent.costmodel_client.CostModelClient.async_call_tool",
        fake_async_call_tool,
    )
    pre_wrapped = {"deal_dict": {"schema_version": "0.1"}}
    _run(run_cost_model_tool.handler({
        "deal_id": "D9", "scenario": "base", "inputs": pre_wrapped,
    }))
    # Single-key {"deal_dict": ...} is treated as already-wrapped.
    assert captured["arguments"] == pre_wrapped


def test_run_cost_model_legacy_loose_kwarg_tool_does_not_wrap(monkeypatch):
    """Tools like check_renovation_roi take loose kwargs. inputs flows through
    untouched — no wrapping."""
    captured = {}

    async def fake_async_call_tool(self, tool_name, arguments):
        captured["arguments"] = arguments
        return {"roi_pct": 17.5}

    monkeypatch.setattr(
        "plat_agent.costmodel_client.CostModelClient.async_call_tool",
        fake_async_call_tool,
    )
    inputs = {"total_cost_high": 14000, "current_monthly_rent": 850, "target_monthly_rent": 1100}
    _run(run_cost_model_tool.handler({
        "deal_id": "D8", "scenario": "base",
        "tool_name": "check_renovation_roi", "inputs": inputs,
    }))
    assert captured["arguments"] == inputs  # not wrapped


def test_run_cost_model_supports_custom_tool_name(monkeypatch):
    captured = {}

    async def fake_async_call_tool(self, tool_name, arguments):
        captured["tool_name"] = tool_name
        captured["arguments"] = arguments
        return {"roi_pct": 0.27}

    monkeypatch.setattr(
        "plat_agent.costmodel_client.CostModelClient.async_call_tool",
        fake_async_call_tool,
    )
    result = _run(run_cost_model_tool.handler({
        "deal_id": "D2",
        "scenario": "base",
        "tool_name": "check_renovation_roi",
        "inputs": {"unit_sqft": 850},
    }))

    assert captured["tool_name"] == "check_renovation_roi"
    assert captured["arguments"] == {"unit_sqft": 850}
    assert "roi_pct=0.27" in result["content"][0]["text"]


def test_run_cost_model_summary_extracts_renovation_program_fields(monkeypatch):
    """``estimate_from_deal`` returns ``{estimates: [...], renovation_programs:
    [...]}``. The summary's ``_flatten_numeric`` must descend into the lists so
    the per-program numeric fields surface as compare_scenarios-parseable
    key=value pairs."""
    async def returns_estimate_from_deal_shape(self, tool_name, arguments):
        return {
            "estimates": [],
            "renovation_programs": [
                {"renovation_cost_per_unit": 14000, "rent_premium_monthly": 250},
            ],
        }
    monkeypatch.setattr(
        "plat_agent.costmodel_client.CostModelClient.async_call_tool",
        returns_estimate_from_deal_shape,
    )
    result = _run(run_cost_model_tool.handler({
        "deal_id": "D5", "scenario": "base", "inputs": {"any": "deal"},
    }))
    text = result["content"][0]["text"]
    assert "renovation_programs.0.renovation_cost_per_unit=14000" in text
    assert "renovation_programs.0.rent_premium_monthly=250" in text


def test_run_cost_model_surfaces_validation_problem_as_failure(monkeypatch):
    """When costmodel returns a ValidationProblem dict (structured error),
    the orchestrator must surface it as a tool failure — not silently treat
    it as success when no numeric fields are present."""

    async def returns_validation_problem(self, tool_name, arguments):
        return {
            "error_type": "validation_error",
            "message": "deal missing 'property'",
            "field_errors": [{"loc": ["property"], "msg": "field required"}],
            "hint": "",
        }

    monkeypatch.setattr(
        "plat_agent.costmodel_client.CostModelClient.async_call_tool",
        returns_validation_problem,
    )
    result = _run(run_cost_model_tool.handler({
        "deal_id": "D7", "scenario": "base", "inputs": {"unit_cohorts": []},
    }))
    assert result.get("is_error") is True
    text = result["content"][0]["text"]
    assert "validation_error" in text
    assert "deal missing 'property'" in text


def test_run_cost_model_handles_client_exception(monkeypatch):
    async def boom(self, tool_name, arguments):
        raise RuntimeError("subprocess died")

    monkeypatch.setattr(
        "plat_agent.costmodel_client.CostModelClient.async_call_tool",
        boom,
    )
    result = _run(run_cost_model_tool.handler({"deal_id": "D3", "scenario": "base"}))
    assert result.get("is_error") is True
    assert "subprocess died" in result["content"][0]["text"]


# ---------------------------------------------------------------------------
# Phase-7 wiring: run_underwriting_tool delegates to UnderwritingClient
# ---------------------------------------------------------------------------

def test_run_underwriting_calls_underwriting_client(monkeypatch):
    captured = {}

    def fake_run_summary(self, inputs):
        captured["inputs"] = inputs
        return {
            "status": "success",
            "irr": {"levered_irr": 0.145, "unlevered_irr": 0.095},
            "equity_multiple": {"levered_em": 1.85},
            "dscr": {"minimum": 1.25, "average": 1.45},
            "yields": {"going_in_cap_rate": 0.055, "exit_cap_rate": 0.06},
        }

    monkeypatch.setattr(
        "plat_agent.underwriting_client.UnderwritingClient.run_summary",
        fake_run_summary,
    )
    result = _run(run_underwriting_tool.handler({
        "deal_id": "D1",
        "inputs": {"metadata": {"deal_id": "D1"}, "unit_cohorts": []},
    }))

    assert captured["inputs"] == {"metadata": {"deal_id": "D1"}, "unit_cohorts": []}
    text = result["content"][0]["text"]
    assert "deal_id=D1" in text
    assert "irr=0.145" in text
    assert "dscr=1.25" in text
    assert "cap=0.055" in text
    assert "em=1.85" in text


def test_run_underwriting_handles_engine_error_status(monkeypatch):
    def fake_run_summary(self, inputs):
        return {"status": "error", "error": "validation failed: missing rent_roll"}

    monkeypatch.setattr(
        "plat_agent.underwriting_client.UnderwritingClient.run_summary",
        fake_run_summary,
    )
    result = _run(run_underwriting_tool.handler({"deal_id": "D1"}))
    assert result.get("is_error") is True
    assert "validation failed" in result["content"][0]["text"]


def test_run_underwriting_wraps_blocking_call_in_thread(monkeypatch):
    """run_summary is sync and may block; the @tool handler must offload it
    via asyncio.to_thread so it does not stall the event loop."""
    main_loop_thread = threading.get_ident()
    seen = {}

    def fake_run_summary(self, inputs):
        # If we are running on the main event loop's thread, asyncio.to_thread
        # was bypassed and we'd be blocking the loop.
        seen["thread_id"] = threading.get_ident()
        # Sleep to make a regression (running on the loop thread) easy to spot.
        time.sleep(0.05)
        return {"status": "success", "irr": {"levered_irr": 0.1}}

    monkeypatch.setattr(
        "plat_agent.underwriting_client.UnderwritingClient.run_summary",
        fake_run_summary,
    )
    _run(run_underwriting_tool.handler({"deal_id": "D1"}))
    assert seen["thread_id"] != main_loop_thread


def test_run_underwriting_handles_client_exception(monkeypatch):
    def boom(self, inputs):
        raise RuntimeError("worker startup failed")

    monkeypatch.setattr(
        "plat_agent.underwriting_client.UnderwritingClient.run_summary",
        boom,
    )
    result = _run(run_underwriting_tool.handler({"deal_id": "D1"}))
    assert result.get("is_error") is True
    assert "worker startup failed" in result["content"][0]["text"]


# ---------------------------------------------------------------------------
# Module-level singleton accessors — clients reused across tool invocations
# so we don't pay the daemon-thread + MCP-session cold start every call.
# ---------------------------------------------------------------------------

def test_cost_model_client_is_singleton_across_calls():
    """Repeat calls to _cost_model_client return the SAME instance (no cold-start per call)."""
    from plat_agent.orchestrator.tools import _cost_model_client
    _cost_model_client.cache_clear()  # ensure clean state
    a = _cost_model_client()
    b = _cost_model_client()
    assert a is b
    _cost_model_client.cache_clear()  # cleanup for other tests


def test_underwriting_client_is_singleton_across_calls():
    """Repeat calls to _underwriting_client return the SAME instance."""
    from plat_agent.orchestrator.tools import _underwriting_client
    _underwriting_client.cache_clear()
    a = _underwriting_client()
    b = _underwriting_client()
    assert a is b
    _underwriting_client.cache_clear()


# ---------------------------------------------------------------------------
# Phase-7 wiring: compare_scenarios end-to-end with real wiring outputs
# ---------------------------------------------------------------------------

def test_compare_scenarios_consumes_wired_tool_output(monkeypatch):
    """Verify that the strings emitted by the wired tools are parseable by
    compare_scenarios — the contract between the three tools is preserved."""
    async def fake_cm(self, tool_name, arguments):
        # Pretend cost model surfaces an irr-like field for cross-comparison
        return {"irr": 0.12, "noi": 1000000}

    def fake_uw(self, inputs):
        return {
            "status": "success",
            "irr": {"levered_irr": 0.15},
            "noi_summary": {"total_noi": 1100000},
            "dscr": {"minimum": 1.3},
            "yields": {"going_in_cap_rate": 0.06},
            "equity_multiple": {"levered_em": 1.9},
        }

    monkeypatch.setattr(
        "plat_agent.costmodel_client.CostModelClient.async_call_tool",
        fake_cm,
    )
    monkeypatch.setattr(
        "plat_agent.underwriting_client.UnderwritingClient.run_summary",
        fake_uw,
    )

    cm_result = _run(run_cost_model_tool.handler({"deal_id": "D", "scenario": "base"}))
    uw_result = _run(run_underwriting_tool.handler({"deal_id": "D"}))
    cmp_result = _run(compare_scenarios_tool.handler({
        "cost_model_summary": cm_result["content"][0]["text"],
        "underwriting_summary": uw_result["content"][0]["text"],
    }))
    text = cmp_result["content"][0]["text"]
    assert "delta" in text.lower()
    assert "irr" in text  # overlap key present


# ---------------------------------------------------------------------------
# Phase-7 wiring: load_deal_inputs_tool — canonical deal JSON loader
# ---------------------------------------------------------------------------

def test_load_deal_inputs_requires_deal_id():
    result = _run(load_deal_inputs_tool.handler({"deal_id": ""}))
    assert result.get("is_error") is True
    assert "deal_id" in result["content"][0]["text"].lower()


def test_load_deal_inputs_strips_whitespace_in_deal_id():
    result = _run(load_deal_inputs_tool.handler({"deal_id": "   "}))
    assert result.get("is_error") is True


def test_load_deal_inputs_returns_canonical_payload(monkeypatch, tmp_path):
    """When a candidate path exists, the tool reads it, JSON-decodes it,
    and returns `{"path": ..., "inputs": <decoded>}` as the text payload."""
    deal_id = "FAKE-DEAL"
    fake_payload = {
        "schema_version": "0.1",
        "metadata": {"deal_id": deal_id, "run_id": "r1", "as_of_date": "2026-01-01"},
        "unit_cohorts": [{"cohort_id": "A", "unit_count": 10}],
    }
    fixture_path = tmp_path / "deals" / f"{deal_id}.json"
    fixture_path.parent.mkdir(parents=True, exist_ok=True)
    fixture_path.write_text(json.dumps(fake_payload))

    # Force candidate list to point at our tmp fixture.
    from plat_agent.orchestrator import tools as tools_mod
    monkeypatch.setattr(
        tools_mod,
        "_DEAL_INPUT_CANDIDATES",
        lambda did: [tmp_path / "deals" / f"{did}.json"],
    )

    result = _run(load_deal_inputs_tool.handler({"deal_id": deal_id}))
    assert result.get("is_error") is not True
    body = json.loads(result["content"][0]["text"])
    assert body["inputs"] == fake_payload
    assert body["path"].endswith(f"{deal_id}.json")


def test_load_deal_inputs_falls_through_missing_paths(monkeypatch, tmp_path):
    """When the first candidate is missing but a later one exists, returns
    the later one. Confirms the loop walks the full candidate list."""
    deal_id = "FOUND-LATER"
    payload = {"schema_version": "0.1", "metadata": {"deal_id": deal_id}}
    second = tmp_path / "second" / f"{deal_id}.json"
    second.parent.mkdir(parents=True, exist_ok=True)
    second.write_text(json.dumps(payload))

    from plat_agent.orchestrator import tools as tools_mod
    monkeypatch.setattr(
        tools_mod,
        "_DEAL_INPUT_CANDIDATES",
        lambda did: [
            tmp_path / "missing" / f"{did}.json",
            second,
        ],
    )
    result = _run(load_deal_inputs_tool.handler({"deal_id": deal_id}))
    assert result.get("is_error") is not True
    body = json.loads(result["content"][0]["text"])
    assert body["inputs"] == payload


def test_load_deal_inputs_fails_when_no_candidate_exists(monkeypatch, tmp_path):
    deal_id = "MISSING-DEAL"
    candidates = [
        tmp_path / "a" / f"{deal_id}.json",
        tmp_path / "b" / f"{deal_id}.json",
    ]
    from plat_agent.orchestrator import tools as tools_mod
    monkeypatch.setattr(
        tools_mod,
        "_DEAL_INPUT_CANDIDATES",
        lambda did: candidates,
    )
    result = _run(load_deal_inputs_tool.handler({"deal_id": deal_id}))
    assert result.get("is_error") is True
    text = result["content"][0]["text"]
    assert deal_id in text
    # Failure message should include the candidate paths so the agent (and
    # operator) can see exactly where we looked.
    for c in candidates:
        assert str(c) in text


def test_load_deal_inputs_handles_invalid_json(monkeypatch, tmp_path):
    """A candidate file that exists but contains invalid JSON should fail
    gracefully rather than blowing up the runner."""
    deal_id = "BAD-JSON"
    bad = tmp_path / f"{deal_id}.json"
    bad.write_text("{ this is not json")

    from plat_agent.orchestrator import tools as tools_mod
    monkeypatch.setattr(
        tools_mod,
        "_DEAL_INPUT_CANDIDATES",
        lambda did: [bad],
    )
    result = _run(load_deal_inputs_tool.handler({"deal_id": deal_id}))
    assert result.get("is_error") is True
    assert "json" in result["content"][0]["text"].lower()


def test_load_deal_inputs_finds_test_001_fixture():
    """End-to-end: the bundled `tests/fixtures/deals/TEST-001.json` fixture
    must be discoverable by the default candidate list so the smoke test
    has a deterministic deal to load."""
    result = _run(load_deal_inputs_tool.handler({"deal_id": "TEST-001"}))
    assert result.get("is_error") is not True, result["content"][0]["text"]
    body = json.loads(result["content"][0]["text"])
    inputs = body["inputs"]
    # Spot-check the fields that the underwriting engine schema requires.
    for required in (
        "schema_version",
        "metadata",
        "time_grid",
        "unit_cohorts",
        "market_rent_curve",
        "loss_to_lease",
        "physical_vacancy_curve",
        "collection_loss_curve",
        "revenue_programs",
        "program_adoption_curve",
    ):
        assert required in inputs, f"missing required field: {required}"
