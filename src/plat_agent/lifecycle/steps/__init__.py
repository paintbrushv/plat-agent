"""Lifecycle step adapters — one module per step.

Each adapter implements the LifecycleStep protocol from plan-01 and
wires a sibling-repo agent (or pure-Python subsystem) into the
orchestrator's serial pipeline.
"""

from plat_agent.lifecycle.steps.comps import CompsStep
from plat_agent.lifecycle.steps.intake import IntakeStep

__all__ = ["CompsStep", "IntakeStep"]
