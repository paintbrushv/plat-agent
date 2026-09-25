"""Cross-repo agent dispatch for plat-agent orchestrators.

The orchestrator pattern: invoke a sibling-repo subagent via headless
`claude -p --cwd <sibling_repo>` and parse a BridgeResponseV1 from stdout.
"""

from plat_agent.dispatch.sibling import (
    DispatchError,
    SiblingRepo,
    atomic_write_json,
    dispatch_sibling_agent,
)

__all__ = [
    "DispatchError",
    "SiblingRepo",
    "atomic_write_json",
    "dispatch_sibling_agent",
]
