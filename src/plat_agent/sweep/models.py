"""Pydantic result models for sweep operations."""

from typing import Optional

from pydantic import BaseModel


class ScenarioRun(BaseModel):
    """One scenario's result inside a ScenarioSweepResult."""
    name: str                    # "Base", "Bull", "Bear"
    overrides: dict              # what was perturbed (preset deltas) — empty for Base
    metrics: dict                # normalized nested metrics from backend.run_deal
    feasible: bool               # compound gate: levered_irr >= 0.12 AND min_dscr >= 1.20
    status: str                  # "success" | "error"
    error: Optional[str] = None  # populated when status="error"


class ScenarioSweepResult(BaseModel):
    """Bull/Base/Bear comparison for one deal."""
    property_id: str
    preset_family: str           # "stabilized" | "value_add"
    status: str                  # "success" | "roi_gate_failed"
    scenarios: list[ScenarioRun]
    summary: str                 # human-readable table


class BreakEvenResult(BaseModel):
    """One-axis break-even search result."""
    property_id: str
    axis: str                    # "target_rent" | "exit_cap" | "monthly_pace"
    cohort_id: Optional[str] = None
    objective: str               # "compound" | "levered_irr" | "min_dscr"
    status: str                  # "success" | "not_bracketed" | "roi_gate_failed" | "bracket_error"

    # For compound success: which constraint bound (IRR or DSCR)
    binding_constraint: Optional[str] = None  # "irr" | "dscr"

    # The reported break-even point (compound or single-objective)
    breakpoint: Optional[float] = None
    breakpoint_metrics: Optional[dict] = None

    # Per-constraint detail (populated for whichever constraints were bisected)
    irr_breakpoint: Optional[float] = None
    irr_iterations: int = 0
    dscr_breakpoint: Optional[float] = None
    dscr_iterations: int = 0

    # Always populated — the initial bracket evaluation
    bracket_low: dict = {}
    bracket_high: dict = {}

    summary: str
