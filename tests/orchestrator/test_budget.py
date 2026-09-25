import asyncio
from plat_agent.orchestrator.budget import BudgetState, enforce_call_budget


def _run(c): return asyncio.run(c)


def test_allows_calls_under_budget():
    state = BudgetState(remaining=3)
    result = _run(enforce_call_budget(state, {"tool_name": "mcp__o__run_cost_model", "tool_input": {}}, "id", None))
    assert result == {}
    assert state.remaining == 2


def test_denies_when_budget_exhausted():
    state = BudgetState(remaining=0)
    result = _run(enforce_call_budget(state, {"tool_name": "mcp__o__run_cost_model", "tool_input": {}}, "id", None))
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
