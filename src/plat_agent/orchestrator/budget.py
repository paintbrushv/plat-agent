from dataclasses import dataclass
from typing import Any

from . import SERVER_NAME


@dataclass
class BudgetState:
    remaining: int


async def enforce_call_budget(state: BudgetState, input_data: dict[str, Any],
                              tool_use_id: str, context: Any) -> dict[str, Any]:
    if not input_data.get("tool_name", "").startswith(f"mcp__{SERVER_NAME}__"):
        return {}
    if state.remaining <= 0:
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": "Tool-call budget exhausted for this run",
            }
        }
    state.remaining -= 1
    return {}
