"""Subsystem-level blocker dependency map (spec §4.3 + §4.6).

Given (step_name, step_result), decide whether the orchestrator should halt,
continue degraded, or skip downstream steps. Pure function; the orchestrator
applies the returned DependencyDecision to its pipeline state.

The full table:

  intake hard-error          → ALL halt. status=failed_at_intake. exit=1.
  intake blocker             → continue degraded (comps + judgment +
                               underwriting + memo + crm all run);
                               status=memo_ready_with_blockers.
                               This includes the needs_analyst_input
                               blocker that surfaces analyst-required
                               gaps after V1.2 auto-defaults
                               (target_monthly_rent, OM extraction).
  comps hard-error           → judgment continues with comps_unavailable=true.
                               recommendation forced NEEDS_DATA.
                               status=memo_ready_with_blockers.
  comps blocker              → judgment proceeds with degraded confidence.
  judgment hard-error        → skip underwriting. memo runs. status=failed_at_judgment. exit=1.
  judgment blocker           → skip underwriting (V1; V2 configurable).
  underwriting hard-error    → memo draft mode. CRM status=failed_at_underwriting.
  memo hard-error            → CRM logs failed_at_memo. exit=1.
  crm hard-error             → warning only; exit=0 (memo is on disk).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from plat_agent.lifecycle.protocol import StepResult
from plat_agent.lifecycle.state import LifecycleStatus, StepName


@dataclass
class DependencyDecision:
    """Orchestrator-side decision after a single step."""

    # Halt the entire pipeline immediately (no further steps).
    halt: bool = False
    # Terminal status to set on lifecycle state if halting NOW.
    terminal_status: LifecycleStatus | None = None
    # Terminal status if no later step recovers (default if pipeline runs through).
    terminal_status_if_no_recovery: LifecycleStatus | None = None
    # Skip the underwriting step specifically (judgment-related decisions).
    skip_underwriting: bool = False
    # Memo always attempts to render per §4.6 — but document explicitly.
    run_memo: bool = True
    # Comps unavailable → propagate to recommendation patch + memo banner.
    comps_unavailable: bool = False
    # Override CLI exit code (e.g., CRM failure should still exit 0).
    exit_code_override: int | None = None


def decide_after_step(step: StepName, result: StepResult) -> DependencyDecision:
    """Apply §4.3 to a single step's outcome."""
    if result.status == "ok":
        return DependencyDecision()

    if step == "intake":
        if result.status == "error":
            return DependencyDecision(halt=True, terminal_status="failed_at_intake")
        # blocker
        return DependencyDecision(
            terminal_status_if_no_recovery="memo_ready_with_blockers",
        )

    if step == "comps":
        if result.status == "error":
            # Documented alternate path: judgment runs with comps_unavailable=true
            return DependencyDecision(
                comps_unavailable=True,
                terminal_status_if_no_recovery="memo_ready_with_blockers",
            )
        # blocker
        return DependencyDecision(
            terminal_status_if_no_recovery="memo_ready_with_blockers",
        )

    if step == "judgment":
        if result.status == "error":
            return DependencyDecision(
                skip_underwriting=True,
                run_memo=True,
                terminal_status="failed_at_judgment",
            )
        # blocker — V1 also skips underwriting
        return DependencyDecision(
            skip_underwriting=True,
            terminal_status_if_no_recovery="memo_ready_with_blockers",
        )

    if step == "underwriting":
        # Hard error or blocker — memo runs in draft per §4.6
        return DependencyDecision(
            run_memo=True,
            terminal_status_if_no_recovery="failed_at_underwriting",
        )

    if step == "memo":
        return DependencyDecision(
            terminal_status="failed_at_memo",
        )

    if step == "crm":
        # Memo is on disk; CRM failure is a warning. Exit 0 per §4.3.
        return DependencyDecision(halt=True, exit_code_override=0)

    return DependencyDecision()  # default: continue
