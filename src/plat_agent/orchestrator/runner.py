from functools import partial

from . import SERVER_NAME
from claude_agent_sdk import (
    ClaudeAgentOptions, ClaudeSDKClient, HookMatcher,
    create_sdk_mcp_server, AssistantMessage, TextBlock, ToolUseBlock,
)
from .tools import (
    compare_scenarios_tool,
    load_deal_inputs_tool,
    run_cost_model_tool,
    run_underwriting_tool,
)
from .budget import BudgetState, enforce_call_budget


def build_options(state: BudgetState) -> ClaudeAgentOptions:
    server = create_sdk_mcp_server(
        name=SERVER_NAME,
        version="1.0.0",
        tools=[
            load_deal_inputs_tool,
            run_cost_model_tool,
            run_underwriting_tool,
            compare_scenarios_tool,
        ],
    )
    return ClaudeAgentOptions(
        mcp_servers={SERVER_NAME: server},
        allowed_tools=[
            f"mcp__{SERVER_NAME}__load_deal_inputs",
            f"mcp__{SERVER_NAME}__run_cost_model",
            f"mcp__{SERVER_NAME}__run_underwriting",
            f"mcp__{SERVER_NAME}__compare_scenarios",
        ],
        hooks={"PreToolUse": [HookMatcher(matcher=None,
                                          hooks=[partial(enforce_call_budget, state)])]},
    )


async def screen_deal(deal_id: str, scenario: str = "base", call_budget: int = 6) -> str:
    state = BudgetState(remaining=call_budget)
    options = build_options(state)

    output: list[str] = []
    async with ClaudeSDKClient(options=options) as client:
        await client.query(
            f"Screen deal '{deal_id}' (scenario='{scenario}'). Steps:\n"
            f"1. Call load_deal_inputs with deal_id='{deal_id}' to fetch the canonical inputs JSON. "
            f"It returns a payload of the form {{\"path\": ..., \"inputs\": <dict>}}; extract the "
            f"`inputs` dict.\n"
            f"2. Call run_underwriting with deal_id='{deal_id}' and inputs=<that dict, verbatim>. "
            f"Do not invent fields or pass placeholders.\n"
            f"3. Call run_cost_model with deal_id='{deal_id}', scenario='{scenario}', "
            f"and inputs=<the inputs dict from step 1, verbatim>. The default cost-model "
            f"tool (estimate_from_deal) consumes the canonical multifamily-underwriting "
            f"deal shape directly — pass the whole inputs dict from step 1, do not extract "
            f"sub-fields or fabricate property_data wrappers.\n"
            f"4. Call compare_scenarios on the two formatted summary strings.\n"
            f"Report the delta. If load_deal_inputs fails, report the failure verbatim and stop "
            f"— do not proceed to run_underwriting with a fabricated payload."
        )
        async for msg in client.receive_response():
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        output.append(block.text)
                    elif isinstance(block, ToolUseBlock):
                        output.append(f"[tool] {block.name}({block.input})")
    return "\n".join(output)
