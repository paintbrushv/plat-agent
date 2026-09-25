"""LifecycleStep protocol — contract every lifecycle step implements.

Steps are stateless objects that read upstream artifacts from the run
directory and write their own. Each step has a stable `name` matching
spec §2.X (intake, comps, judgment, underwriting, memo, crm).

The orchestrator (plan-07) drives the protocol: calls is_satisfied()
to check cache validity, calls run() if not satisfied.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from plat_agent.lifecycle.state import BlockerItem, LifecycleState, StepName


StepStatus = Literal["ok", "blocked", "error"]


class StepResult(BaseModel):
    """Outcome of a single LifecycleStep.run() invocation.

    - status="ok": step produced full output. Orchestrator marks step complete.
    - status="blocked": step produced partial output + blockers. Orchestrator
      consults dependency map (§4.3) to decide whether to halt or continue.
    - status="error": hard error. Step's _provenance.json carries error details.
      Orchestrator consults §4.3 for documented alternate path.
    """

    status: StepStatus
    blockers: list[BlockerItem] = Field(default_factory=list)
    error_message: str | None = None
    error_traceback: str | None = None


@runtime_checkable
class LifecycleStep(Protocol):
    """Interface every step in the lifecycle pipeline implements."""

    name: StepName

    def run(self, state: LifecycleState, run_dir: Path) -> StepResult:
        """Execute the step. Read upstream artifacts from run_dir, write own.

        run_dir is `runs/deals/<slug>/outputs/<run_id>/`.

        Must be idempotent: running twice with identical inputs produces
        identical outputs. Atomic writes throughout (use atomic_write_*).

        Must write its `_complete` marker as the LAST action (see cache.py).
        """
        ...

    def is_satisfied(self, state: LifecycleState, run_dir: Path) -> bool:
        """Cache check: True if step's outputs are valid and current.

        Default behavior delegates to lifecycle.cache.is_satisfied(); steps
        only override when they have step-specific cache rules beyond the
        defaults (input_hash, contract_version, TTL, _complete marker).
        """
        ...
