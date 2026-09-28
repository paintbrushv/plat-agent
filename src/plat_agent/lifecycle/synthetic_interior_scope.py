"""Synthetic interior scope for TEST-001.

``minimal_deal_inputs`` can run the underwriting engine. It does not carry
the dimensions the interior estimator requires. The values below are a
synthetic assumption, not an extracted fact, and not a property record.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from decimal import Decimal
from importlib.resources import files

from plat_agent.lifecycle.versioned_adapters import COSTMODEL_V1, UNDERWRITING_V1

ASSUMPTION_KIND = "synthetic_assumption"
INTERIOR_ONLY = "interior_only"
INTERIOR_PLUS_SYNTHETIC_ROOF = "interior plus this synthetic roof"
PURCHASE_PRICE = Decimal("13500000")
DEFERRED_COSTMODEL_SHA = COSTMODEL_V1.source_sha
UNDERWRITING_SHA = UNDERWRITING_V1.source_sha

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

# One stated roof replacement. Quantity 100 is not read from a building record.
SYNTHETIC_DEFERRED_ROOF: dict = {
    "assumption_kind": ASSUMPTION_KIND,
    "extracted_fact": False,
    "note": "Synthetic assumption for TEST-001. Not an extracted fact.",
    "item": "roof_full_replacement",
    "quantity": 100,
    "schedule": {
        "start_month": "2026-07",
        "monthly_pace": 1,
        "downtime_days": 21,
    },
}


@dataclass(frozen=True)
class InteriorOnlyYield:
    """Interior-only capex and the year-2 unlevered yield on that cost.

    ``bid`` stays unset. This is not a presented price.
    """

    capex: Decimal
    per_unit_high: tuple[int, ...]
    year_2_unlevered_noi: Decimal
    year_2_unlevered_yield_on_cost: Decimal | None
    label: str
    bid: None
    withheld: bool
    withhold_reason: str

    def as_dict(self) -> dict:
        ratio = self.year_2_unlevered_yield_on_cost
        return {
            "capex": format(self.capex, "f"),
            "per_unit_high": list(self.per_unit_high),
            "year_2_unlevered_noi": format(self.year_2_unlevered_noi, "f"),
            "year_2_unlevered_yield_on_cost": None if ratio is None else format(ratio, "f"),
            "label": self.label,
            "bid": self.bid,
            "withheld": self.withheld,
            "withhold_reason": self.withhold_reason,
        }


@dataclass(frozen=True)
class InteriorPlusRoofYield:
    """Interior capex plus one synthetic roof. Not a bid."""

    interior_capex: Decimal
    roof_capex: Decimal
    capex: Decimal
    year_2_unlevered_noi: Decimal
    year_2_unlevered_yield_on_cost: Decimal | None
    label: str
    bid: None
    withheld: bool
    withhold_reason: str

    def as_dict(self) -> dict:
        ratio = self.year_2_unlevered_yield_on_cost
        return {
            "interior_capex": format(self.interior_capex, "f"),
            "roof_capex": format(self.roof_capex, "f"),
            "capex": format(self.capex, "f"),
            "year_2_unlevered_noi": format(self.year_2_unlevered_noi, "f"),
            "year_2_unlevered_yield_on_cost": None if ratio is None else format(ratio, "f"),
            "label": self.label,
            "bid": self.bid,
            "withheld": self.withheld,
            "withhold_reason": self.withhold_reason,
        }


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
    """Interior-only capex for TEST-001, plus yield when plat-harness is installed.

    No bid. A missing plat-harness install skips the yield call.
    """
    capex, per_unit_high = interior_capex(
        SYNTHETIC_INTERIOR_SCOPE,
        unit_counts=SYNTHETIC_INTERIOR_UNIT_COUNTS,
    )
    year_2_noi = _test001_year_2_noi()
    ratio = _year_2_yield(year_2_noi, PURCHASE_PRICE, capex)
    return InteriorOnlyYield(
        capex=capex,
        per_unit_high=per_unit_high,
        year_2_unlevered_noi=year_2_noi,
        year_2_unlevered_yield_on_cost=ratio,
        label=INTERIOR_ONLY,
        bid=None,
        withheld=True,
        withhold_reason="The 5.5% exit cap still withholds this deal.",
    )


def synthetic_roof_capex(scope: dict | None = None) -> Decimal:
    """Price the stated roof item with ``estimate_deferred_maintenance``.

    Uses that tree's knowledge base and ``total_high``. Quantity stays the
    value on the scope.
    """
    scope = SYNTHETIC_DEFERRED_ROOF if scope is None else scope
    if scope.get("assumption_kind") != ASSUMPTION_KIND or scope.get("extracted_fact") is not False:
        raise ValueError("deferred roof must be labeled a synthetic assumption, not an extracted fact")
    item = scope["item"]
    quantity = scope["quantity"]
    if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 1:
        raise ValueError("deferred roof quantity must be a positive integer")
    estimate = _load_estimate_deferred_maintenance()
    schedule = _load_program_schedule().model_validate(scope["schedule"])
    result = estimate(
        items=[item],
        quantity=quantity,
        schedule=schedule,
        kb=_deferred_knowledge_base(),
    )
    total_high = result["total_high"]
    if total_high is None:
        raise ValueError("synthetic roof was not priced")
    return Decimal(str(total_high))


def interior_plus_synthetic_roof_yield() -> InteriorPlusRoofYield:
    """Interior capex plus the synthetic roof. No bid."""
    interior_total, _per_unit = interior_capex(
        SYNTHETIC_INTERIOR_SCOPE,
        unit_counts=SYNTHETIC_INTERIOR_UNIT_COUNTS,
    )
    roof = synthetic_roof_capex()
    capex = interior_total + roof
    year_2_noi = _test001_year_2_noi()
    return InteriorPlusRoofYield(
        interior_capex=interior_total,
        roof_capex=roof,
        capex=capex,
        year_2_unlevered_noi=year_2_noi,
        year_2_unlevered_yield_on_cost=_year_2_yield(year_2_noi, PURCHASE_PRICE, capex),
        label=INTERIOR_PLUS_SYNTHETIC_ROOF,
        bid=None,
        withheld=True,
        withhold_reason="The 5.5% exit cap still withholds this deal.",
    )


def load_test001_underwriting_metrics() -> dict:
    """Run the pinned public underwriting engine on its saved TEST-001 inputs.

    The reviewed distribution is checked before use. A child interpreter
    prevents a previously imported engine from supplying the numerator.
    """
    UNDERWRITING_V1.verify()
    fixture = files("plat_agent.lifecycle").joinpath("fixtures/test001_underwriting_inputs.json")
    script = (
        "import json, sys; "
        "from plat_agent.lifecycle.versioned_adapters import UNDERWRITING_V1; "
        "UNDERWRITING_V1.verify(); "
        "from engine.engine import run_underwriting; "
        "result = run_underwriting(json.load(sys.stdin)); "
        "print(json.dumps(result['metrics']))"
    )
    env = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    completed = subprocess.run(
        [sys.executable, "-I", "-c", script],
        input=fixture.read_text(encoding="utf-8"),
        text=True,
        capture_output=True,
        env=env,
        check=True,
        timeout=60,
    )
    return json.loads(completed.stdout)


def _test001_year_2_noi() -> Decimal:
    value = load_test001_underwriting_metrics()["noi"]["year_2_unlevered_noi"]
    if value is None:
        raise ValueError("underwriting engine did not return year_2_unlevered_noi")
    return Decimal(str(value))


def record_test001_thesis():
    """Record a new thesis from this engine run; never rewrite the old snapshot."""
    from plat_harness.original_thesis import record_original_thesis
    from plat_harness.reasonability import present_underwriting

    metrics = load_test001_underwriting_metrics()
    issued = present_underwriting(
        {
            "purchase_price": PURCHASE_PRICE,
            "going_in_cap_rate": metrics["yields"]["going_in_cap_rate"],
            "exit_cap_rate": metrics["yields"]["exit_cap_rate"],
            "price_per_unit": PURCHASE_PRICE / Decimal(100),
            "units": 100,
            "minimum_dscr": metrics["dscr"]["minimum_dscr"],
            "year_1_noi": metrics["noi"]["year_1_noi"],
        }
    )
    capex = interior_capex(SYNTHETIC_INTERIOR_SCOPE, unit_counts=SYNTHETIC_INTERIOR_UNIT_COUNTS)[0]
    capex += synthetic_roof_capex()
    record = record_original_thesis(
        "TEST-001",
        purchase_price=PURCHASE_PRICE,
        year_2_unlevered_noi=Decimal(str(metrics["noi"]["year_2_unlevered_noi"])),
        capex=capex,
        present_as_bid=issued["present_as_bid"],
    )
    return issued, record


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


def _load_estimate_unit():
    COSTMODEL_V1.verify()
    from plat_costmodel.estimator import estimate_unit
    return estimate_unit


def _load_program_schedule():
    COSTMODEL_V1.verify()
    from plat_costmodel.schemas.scope import ProgramSchedule

    return ProgramSchedule


def _load_estimate_deferred_maintenance():
    COSTMODEL_V1.verify()
    from plat_costmodel.deferred_estimator import estimate_deferred_maintenance

    return estimate_deferred_maintenance


def _deferred_knowledge_base() -> dict:
    import yaml

    COSTMODEL_V1.verify()
    with files("plat_costmodel").joinpath("data/knowledge_base.yaml").open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def _year_2_yield(noi: Decimal, price: Decimal, capex: Decimal) -> Decimal | None:
    """Call the installed yield function. Skip when plat-harness is not installed."""
    try:
        from plat_harness.underwriting_direction import year_2_unlevered_yield_on_cost
    except ImportError:
        return None
    if capex is None:
        raise ValueError("interior capex is missing; refusing to treat it as zero")
    return year_2_unlevered_yield_on_cost(noi, price, capex)
