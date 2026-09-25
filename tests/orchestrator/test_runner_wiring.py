from plat_agent.orchestrator.budget import BudgetState
from plat_agent.orchestrator.runner import SERVER_NAME, build_options


def test_allowed_tools_match_registered_tool_names():
    options = build_options(BudgetState(remaining=6))
    assert set(options.allowed_tools) == {
        f"mcp__{SERVER_NAME}__load_deal_inputs",
        f"mcp__{SERVER_NAME}__run_cost_model",
        f"mcp__{SERVER_NAME}__run_underwriting",
        f"mcp__{SERVER_NAME}__compare_scenarios",
    }


def test_budget_hook_registered_under_pretooluse():
    options = build_options(BudgetState(remaining=6))
    assert len(options.hooks["PreToolUse"]) == 1
    matcher = options.hooks["PreToolUse"][0]
    assert len(matcher.hooks) == 1


def test_server_name_constant_used_for_all_tool_identifiers():
    options = build_options(BudgetState(remaining=6))
    for tool_id in options.allowed_tools:
        assert tool_id.startswith(f"mcp__{SERVER_NAME}__")
