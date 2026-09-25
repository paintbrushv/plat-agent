"""Wave 2 Task 2.2 — `apply_target_rent` cardinality contract.

Audit Stage 3 HIGH-3: prior to this fix, `apply_target_rent` silently
fanned out a single-cohort target rent to EVERY renovation_program targeting
that cohort. With cohort_id collisions allowed (CRITICAL-1, CRITICAL-2)
this was a load-bearing silent multiplier on perturbation outputs.

Contract now:
  - 1 matching program  → mutate it.
  - 0 matching programs → silent no-op (returns deep-copied deal unchanged).
  - >1 matching programs → ValueError with disambiguation guidance.
"""

from __future__ import annotations

import copy

import pytest

from plat_agent.sweep.perturb import apply_target_rent


def _deal_with_one_program() -> dict:
    return {
        "unit_cohorts": [
            {"cohort_id": "A1", "initial_inplace_rent": 800.0},
        ],
        "renovation_programs": [
            {
                "program_id": "reno_A1",
                "target_cohort": "A1",
                "rent_premium_monthly": 200.0,
                "post_renovation_market_rent": 1000.0,
                "monthly_pace": 3,
            },
        ],
    }


def _deal_with_two_programs_same_cohort() -> dict:
    """The post-CRITICAL-2 world should reject this at splice-time, but
    `apply_target_rent` is the last line of defense for any deal that slips
    through with multi-program-per-cohort encoding."""
    return {
        "unit_cohorts": [
            {"cohort_id": "A1", "initial_inplace_rent": 800.0},
        ],
        "renovation_programs": [
            {
                "program_id": "reno_A1_yr1",
                "target_cohort": "A1",
                "rent_premium_monthly": 200.0,
                "post_renovation_market_rent": 1000.0,
                "monthly_pace": 3,
            },
            {
                "program_id": "reno_A1_yr2",
                "target_cohort": "A1",
                "rent_premium_monthly": 200.0,
                "post_renovation_market_rent": 1000.0,
                "monthly_pace": 3,
            },
        ],
    }


def _deal_with_cohort_no_program() -> dict:
    """Cohort exists in unit_cohorts but no renovation_program targets it.
    Common during sweep setup before programs are spliced in."""
    return {
        "unit_cohorts": [
            {"cohort_id": "A1", "initial_inplace_rent": 800.0},
            {"cohort_id": "B1", "initial_inplace_rent": 950.0},
        ],
        "renovation_programs": [
            {
                "program_id": "reno_A1",
                "target_cohort": "A1",
                "rent_premium_monthly": 200.0,
                "post_renovation_market_rent": 1000.0,
                "monthly_pace": 3,
            },
        ],
    }


def test_apply_target_rent_single_match_succeeds() -> None:
    deal = _deal_with_one_program()
    result = apply_target_rent(deal, cohort_id="A1", target_rent=1100.0)

    progs = [p for p in result["renovation_programs"] if p["target_cohort"] == "A1"]
    assert len(progs) == 1
    assert progs[0]["rent_premium_monthly"] == pytest.approx(300.0)  # 1100 - 800
    assert progs[0]["post_renovation_market_rent"] == pytest.approx(1100.0)


def test_apply_target_rent_zero_match_no_op() -> None:
    """Zero matching programs is a silent no-op — the cohort exists in
    unit_cohorts but has no renovation_program targeting it. Returning a
    deep-copied unchanged deal preserves the pre-cardinality semantics for
    the common sweep-setup case."""
    deal = _deal_with_cohort_no_program()
    original = copy.deepcopy(deal)

    # Cohort B1 exists but is not targeted by any program.
    result = apply_target_rent(deal, cohort_id="B1", target_rent=1200.0)

    # Must return a fresh dict, equal to the input (no programs were mutated).
    assert result is not deal
    assert result == original
    # Original dict still untouched.
    assert deal == original


def test_apply_target_rent_multiple_matches_raises() -> None:
    deal = _deal_with_two_programs_same_cohort()

    with pytest.raises(ValueError) as excinfo:
        apply_target_rent(deal, cohort_id="A1", target_rent=1100.0)

    msg = str(excinfo.value)
    assert "ambiguous" in msg
    assert "2 programs" in msg
    assert "'A1'" in msg
    assert "program_id" in msg  # disambiguation guidance


def test_apply_target_rent_unknown_cohort_still_raises() -> None:
    """Pre-existing behavior: unknown cohort_id → ValueError.
    Cardinality check runs *after* cohort existence; ordering matters because
    a missing cohort is a more fundamental failure than ambiguity."""
    deal = _deal_with_one_program()

    with pytest.raises(ValueError, match="not found in unit_cohorts"):
        apply_target_rent(deal, cohort_id="ZZZ", target_rent=1100.0)


def test_apply_target_rent_does_not_mutate_input() -> None:
    """Cardinality assert reads from `deal`, but the mutation must still be
    applied to a deep copy — verify the input dict is untouched on the
    success path."""
    deal = _deal_with_one_program()
    original = copy.deepcopy(deal)

    apply_target_rent(deal, cohort_id="A1", target_rent=1100.0)

    assert deal == original
