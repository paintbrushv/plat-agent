"""Synthetic interior scope for TEST-001.

``minimal_deal_inputs`` can run the underwriting engine. It does not carry
the dimensions the interior estimator requires. The values below are a
synthetic assumption, not an extracted fact, and not a property record.
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from plat_agent.dispatch.sibling import primary_checkout_root

ASSUMPTION_KIND = "synthetic_assumption"
INTERIOR_ONLY = "interior_only"
YEAR_2_UNLEVERED_NOI = Decimal("1200004.80")
PURCHASE_PRICE = Decimal("13500000")
HARNESS_SHA = "7429ff800dd9bdd6ffbee46575c0c967d94efd61"
HARNESS_ROOT = Path("/home/ubuntu/plat-harness-worktrees/yield-and-reasonability")

# Stated for TEST-001. These are not read off the underwriting fixture.
SYNTHETIC_INTERIOR_SCOPE: dict = {
    "assumption_kind": ASSUMPTION_KIND,
    "extracted_fact": False,
    "note": "Synthetic assumption for TEST-001. Not an extracted fact.",
    "scope_level": "standard_value_add",
    "finish_tier": "basic",
    "rent_premium_monthly": 50,
    "schedule": {
        "start_month": "2026-07",
        "monthly_pace": 5,
        "downtime_days": 21,
    },
    "cohorts": [
        {
            "avg_sqft": 650,
            "avg_bedrooms": 1,
            "avg_bathrooms": 1,
            "current_avg_rent": 1200,
        },
        {
            "avg_sqft": 950,
            "avg_bedrooms": 2,
            "avg_bathrooms": 2,
            "current_avg_rent": 1500,
        },
    ],
}

# The interior estimator prices one unit. These counts replicate that unit
# the way the interior path sums a template across a cohort. They are a
# stated assumption, parallel to the scope, not a renamed fixture field.
SYNTHETIC_INTERIOR_UNIT_COUNTS = (50, 50)


@dataclass(frozen=True)
class InteriorOnlyYield:
    """Interior-only capex and the year-2 unlevered yield on that cost.

    ``bid`` stays unset. This is not a presented price.
    """

    capex: Decimal
    per_unit_high: tuple[int, ...]
    year_2_unlevered_yield_on_cost: Decimal
    label: str
    bid: None
    withheld: bool
    withhold_reason: str
    harness_sha: str

    def as_dict(self) -> dict:
        return {
            "capex": format(self.capex, "f"),
            "per_unit_high": list(self.per_unit_high),
            "year_2_unlevered_yield_on_cost": format(self.year_2_unlevered_yield_on_cost, "f"),
            "label": self.label,
            "bid": self.bid,
            "withheld": self.withheld,
            "withhold_reason": self.withhold_reason,
            "harness_sha": self.harness_sha,
        }


def costmodel_src() -> Path:
    """Source tree of the unmodified plat-costmodel clone."""
    raw = os.environ.get("PLAT_COSTMODEL_PATH")
    root = Path(raw).expanduser().resolve() if raw else primary_checkout_root().parent / "plat-costmodel"
    src = root / "src"
    estimator = src / "plat_costmodel" / "estimator.py"
    if not estimator.is_file():
        raise FileNotFoundError(f"interior estimator not found at {estimator}")
    return src


def harness_src() -> Path:
    """Source tree of the yield worktree. Import only; do not write it."""
    src = HARNESS_ROOT / "harness" / "src"
    owner = src / "plat_harness" / "underwriting_direction.py"
    if not owner.is_file():
        raise FileNotFoundError(f"yield owner not found at {owner}")
    return src


def harness_head_sha() -> str:
    sha = subprocess.check_output(
        ["git", "-C", str(HARNESS_ROOT), "rev-parse", "HEAD"],
        text=True,
    ).strip()
    if sha != HARNESS_SHA:
        raise RuntimeError(f"yield worktree SHA is {sha}, expected {HARNESS_SHA}")
    return sha


def interior_capex(scope: dict, *, unit_counts: tuple[int, ...]) -> tuple[Decimal, tuple[int, ...]]:
    """Call ``estimate_unit`` and sum conservative per-unit highs times counts.

    ``rent_premium_monthly``, ``schedule``, and ``current_avg_rent`` must be
    present. The interior cost function does not use them to price the unit.
    ``year_built``, property class, and market are left unset.
    """
    _require_scope(scope)
    if len(unit_counts) != len(scope["cohorts"]):
        raise ValueError("unit_counts must align with the synthetic cohorts")
    for count in unit_counts:
        if isinstance(count, bool) or not isinstance(count, int) or count < 1:
            raise ValueError("each interior unit count must be a positive integer")

    estimate_unit = _load_estimate_unit()
    per_unit_high: list[int] = []
    total = Decimal(0)
    for cohort, count in zip(scope["cohorts"], unit_counts, strict=True):
        estimate = estimate_unit(
            unit_sqft=cohort["avg_sqft"],
            bedrooms=cohort["avg_bedrooms"],
            bathrooms=cohort["avg_bathrooms"],
            scope_level=scope["scope_level"],
            finish_tier=scope["finish_tier"],
        )
        per_unit_high.append(int(estimate.total_high))
        total += Decimal(int(estimate.total_high)) * Decimal(count)
    return total, tuple(per_unit_high)


def interior_only_yield() -> InteriorOnlyYield:
    """Interior-only year-2 yield for TEST-001. No bid."""
    sha = harness_head_sha()
    capex, per_unit_high = interior_capex(
        SYNTHETIC_INTERIOR_SCOPE,
        unit_counts=SYNTHETIC_INTERIOR_UNIT_COUNTS,
    )
    ratio = _year_2_yield(YEAR_2_UNLEVERED_NOI, PURCHASE_PRICE, capex)
    return InteriorOnlyYield(
        capex=capex,
        per_unit_high=per_unit_high,
        year_2_unlevered_yield_on_cost=ratio,
        label=INTERIOR_ONLY,
        bid=None,
        withheld=True,
        withhold_reason="The 5.5% exit cap still withholds this deal.",
        harness_sha=sha,
    )


def _require_scope(scope: dict) -> None:
    if scope.get("assumption_kind") != ASSUMPTION_KIND or scope.get("extracted_fact") is not False:
        raise ValueError("interior scope must be labeled a synthetic assumption, not an extracted fact")
    premium = scope["rent_premium_monthly"]
    if isinstance(premium, bool) or not isinstance(premium, (int, float)) or premium < 0:
        raise ValueError("rent_premium_monthly must be a non-negative number")
    _load_program_schedule().model_validate(scope["schedule"])
    cohorts = scope["cohorts"]
    if not isinstance(cohorts, list) or not cohorts:
        raise ValueError("synthetic interior scope needs at least one cohort")
    for cohort in cohorts:
        for key in ("avg_sqft", "avg_bedrooms", "avg_bathrooms", "current_avg_rent"):
            if key not in cohort:
                raise KeyError(key)
        rent = cohort["current_avg_rent"]
        if isinstance(rent, bool) or not isinstance(rent, (int, float)) or rent < 0:
            raise ValueError("current_avg_rent must be a non-negative number")


def _insert_src(src: Path) -> None:
    entry = str(src)
    if entry not in sys.path:
        sys.path.insert(0, entry)


def _load_estimate_unit():
    _insert_src(costmodel_src())
    from plat_costmodel.estimator import estimate_unit

    estimator_path = Path(estimate_unit.__code__.co_filename).resolve()
    if estimator_path != (costmodel_src() / "plat_costmodel" / "estimator.py").resolve():
        raise RuntimeError(f"refusing estimator loaded from {estimator_path}")
    return estimate_unit


def _load_program_schedule():
    _insert_src(costmodel_src())
    from plat_costmodel.schemas.scope import ProgramSchedule

    return ProgramSchedule


def _year_2_yield(noi: Decimal, price: Decimal, capex: Decimal) -> Decimal:
    _insert_src(harness_src())
    from plat_harness.underwriting_direction import year_2_unlevered_yield_on_cost

    owner = Path(year_2_unlevered_yield_on_cost.__code__.co_filename).resolve()
    expected = (harness_src() / "plat_harness" / "underwriting_direction.py").resolve()
    if owner != expected:
        raise RuntimeError(f"refusing yield function loaded from {owner}")
    if capex is None:
        raise ValueError("interior capex is missing; refusing to treat it as zero")
    return year_2_unlevered_yield_on_cost(noi, price, capex)
