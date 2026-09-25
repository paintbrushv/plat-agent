"""Sensitivity and scenario sweep layer for plat-agent.

Public API:
    run_scenarios(inputs, preset="value_add", analysis=None, backend=None)
        -> ScenarioSweepResult
    find_break_even(inputs, axis, bounds, cohort_id=None, objective="compound",
                    irr_threshold=0.12, dscr_threshold=1.20, tolerance=0.005,
                    max_iterations=20, analysis=None, backend=None)
        -> BreakEvenResult

Backend injection (for tests, Azure execution, future backends):
    RunBackend — Protocol for any engine-invoking backend
    LocalRunBackend — v1 implementation wrapping UnderwritingClient.run_summary
    AzureRunBackend — submits to the engine's /api/runs and polls
"""

from .azure_backend import AzureRunBackend
from .backend import LocalRunBackend, RunBackend, make_backend
from .break_even import find_break_even, find_break_even_from_canonical
from .models import BreakEvenResult, ScenarioRun, ScenarioSweepResult
from .scenarios import run_scenarios, run_scenarios_from_canonical

__all__ = [
    "AzureRunBackend",
    "BreakEvenResult",
    "LocalRunBackend",
    "RunBackend",
    "ScenarioRun",
    "ScenarioSweepResult",
    "find_break_even",
    "find_break_even_from_canonical",
    "make_backend",
    "run_scenarios",
    "run_scenarios_from_canonical",
]
