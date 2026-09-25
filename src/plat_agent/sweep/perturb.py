"""Deal-inputs mutators for sweep operations.

All mutators return a deep copy; they never mutate the input dict.

Preset application delegates to engine.modules.scenarios._apply_scenario_deltas
so our Bull/Base/Bear semantics stay exactly in sync with the engine's own
scenario logic. See docstring on apply_preset for coupling details.

Per-axis mutators (apply_target_rent, apply_exit_cap, apply_monthly_pace)
are independent — they touch only the fields for their axis.
"""

import copy
import sys
from pathlib import Path
from typing import Any, Callable


# Lazy import from the sibling underwriting engine. The engine_path is
# configured via UnderwritingClient's env-var logic; we reuse the same
# resolution so there's one source of truth.
def _ensure_engine_on_path() -> None:
    from ..underwriting_client import _engine_path
    engine_dir = str(_engine_path())
    if engine_dir not in sys.path:
        sys.path.insert(0, engine_dir)


def get_preset_family(family: str) -> dict:
    """Return the named preset family ('stabilized' or 'value_add').

    Each family is a dict of {name: ScenarioPresets}: {"bull": ..., "bear": ...}.
    The implicit "base" case has no deltas and is handled by run_scenarios itself.
    """
    _ensure_engine_on_path()
    from engine.modules.scenarios import STABILIZED_PRESETS, VALUE_ADD_PRESETS

    families = {"stabilized": STABILIZED_PRESETS, "value_add": VALUE_ADD_PRESETS}
    if family not in families:
        raise ValueError(f"Unknown preset family: {family!r}. Must be 'stabilized' or 'value_add'.")
    return families[family]


def apply_preset(deal: dict, preset) -> dict:
    """Apply a ScenarioPresets delta bundle to a deep copy of `deal`.

    Delegates to engine.modules.scenarios._apply_scenario_deltas. The engine
    owns which fields the preset touches (rent growth, exit cap, vacancy,
    opex). Reimplementing it here would risk drift.
    """
    _ensure_engine_on_path()
    from engine.modules.scenarios import _apply_scenario_deltas

    return _apply_scenario_deltas(deal, preset)


# ---------------------------------------------------------------------------
# Per-axis mutators (used by find_break_even)
# ---------------------------------------------------------------------------

def apply_exit_cap(deal: dict, value: float) -> dict:
    """Set exit_assumptions.exit_cap_rate to `value`. Returns a new dict."""
    result = copy.deepcopy(deal)
    ea = result.setdefault("exit_assumptions", {})
    ea["exit_cap_rate"] = value
    return result


def apply_monthly_pace(deal: dict, value: int) -> dict:
    """Set monthly_pace on every renovation_program to `value`.

    v1 applies uniformly across all programs. Per-program pace sweep is
    deferred (see spec open items).
    """
    result = copy.deepcopy(deal)
    for prog in result.get("renovation_programs", []):
        prog["monthly_pace"] = value
    return result


def apply_target_rent(deal: dict, cohort_id: str, target_rent: float) -> dict:
    """Set the post-renovation target rent for the program targeting `cohort_id`.

    Looks up the cohort's initial_inplace_rent in unit_cohorts, then updates
    the matching renovation_program's rent_premium_monthly (= target - in-place)
    and post_renovation_market_rent (= target).

    Cardinality contract: the deal MUST have AT MOST ONE renovation_program
    targeting `cohort_id`. If multiple programs share a target_cohort, the
    perturbation would silently fan out and distort the multi-program scenario
    (audit Stage 3 HIGH-3). Callers that legitimately need to disambiguate
    must split the cohort or pass `program_id` directly (future API).

    Raises ValueError if:
      - cohort_id is not present in unit_cohorts, or
      - more than one renovation_program targets cohort_id.

    Zero-match is a silent no-op: the function returns a deep-copied deal
    unchanged. This matches the pre-cardinality semantics for the common
    case where a perturbation axis sweeps a cohort that has no renovation
    program defined yet.
    """
    cohort = next(
        (c for c in deal.get("unit_cohorts", []) if c.get("cohort_id") == cohort_id),
        None,
    )
    if cohort is None:
        raise ValueError(
            f"cohort_id {cohort_id!r} not found in unit_cohorts; "
            f"present cohorts: {[c.get('cohort_id') for c in deal.get('unit_cohorts', [])]}"
        )

    matching = [
        p for p in deal.get("renovation_programs", [])
        if p.get("target_cohort") == cohort_id
    ]
    if len(matching) > 1:
        raise ValueError(
            f"apply_target_rent ambiguous: {len(matching)} programs target "
            f"cohort {cohort_id!r}. Provide program_id parameter for "
            f"disambiguation."
        )

    in_place = cohort.get("initial_inplace_rent", 0.0)
    premium = target_rent - in_place

    result = copy.deepcopy(deal)
    for prog in result.get("renovation_programs", []):
        if prog.get("target_cohort") == cohort_id:
            prog["rent_premium_monthly"] = premium
            prog["post_renovation_market_rent"] = target_rent
    return result


# Dispatch dict used by find_break_even. The sweep loop stays axis-agnostic.
#
# For "target_rent", the mutator signature is (deal, cohort_id, target_rent).
# The dispatch wrappers below uniformly take (deal, **kwargs) so break_even.py
# can pass axis-specific kwargs by name.
AXIS_MUTATORS: dict[str, Callable[..., dict]] = {
    "target_rent": lambda deal, value, cohort_id: apply_target_rent(deal, cohort_id=cohort_id, target_rent=value),
    "exit_cap": lambda deal, value: apply_exit_cap(deal, value),
    "monthly_pace": lambda deal, value: apply_monthly_pace(deal, int(value)),
}
