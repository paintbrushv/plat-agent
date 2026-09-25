"""Payloads for plat-costmodel/cost-bridge-analyst."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


# Per-cohort scope/finish vocabularies. ``None`` on the per-cohort fields below
# means "agent decides", using this deterministic default rule:
#
#   if (year_built <= 1990) and (property_class == "C"):
#       scope_level = "standard_value_add"
#   else:
#       scope_level = "light"
#
# ``finish_tier`` defaults to ``"basic"`` when unspecified. Analysts may set
# either field explicitly to override the deterministic rule on a per-cohort
# basis (e.g. mid-renovation on a single 2BR cohort to test ROI sensitivity).
ScopeLevel = Literal[
    "light",
    "standard_value_add",
    "mid_renovation",
    "gut_renovation",
]
FinishTier = Literal["basic", "mid", "premium"]


class CostBridgeRequest(BaseModel):
    """Ask plat-costmodel for property + per-unit-type ROI gating."""

    canonical_deal_json_relative: str = Field(
        description="Path (relative to deal_root) to the canonical deal JSON "
        "produced by intake."
    )
    roi_threshold_pct: float = Field(default=15.0, gt=0)


class UnitTypeCostResult(BaseModel):
    """Per-cohort cost + ROI result returned by cost-bridge-analyst.

    ``scope_level`` and ``finish_tier`` are echoed back so the orchestrator
    (and downstream provenance) can record exactly which cost basis was used
    for this cohort. ``None`` on either field signals that the cost-bridge
    analyst applied the deterministic default rule (see module docstring) and
    no analyst override was present in the canonical input.
    """

    cohort_id: str
    count: int
    roi_pct_total_high: float
    payback_months: float | None = None
    roi_gate_pass: bool
    scope_level: ScopeLevel | None = Field(
        default=None,
        description=(
            "Per-cohort scope used for the estimate. None means 'agent "
            "decides' via the deterministic rule: vintage <= 1990 AND "
            "property_class == 'C' -> 'standard_value_add'; else 'light'."
        ),
    )
    finish_tier: FinishTier | None = Field(
        default=None,
        description=(
            "Per-cohort finish tier used for the estimate. None means "
            "'agent decides' (defaults to 'basic')."
        ),
    )


class CostBridgeResponse(BaseModel):
    property_estimate_relative: str = Field(
        description="Path to property-level cost estimate JSON."
    )
    renovation_programs_relative: str = Field(
        description="Path to schema-mapped renovation_programs JSON ready "
        "to splice into base_deal_inputs."
    )
    per_unit_type: list[UnitTypeCostResult]
    risk_flags: list[str] = Field(default_factory=list)
    ready_to_underwrite: bool = Field(
        description="True iff all unit types cleared the ROI gate."
    )
    raw_property_estimate: dict[str, Any] = Field(
        default_factory=dict,
        description="Verbatim plat-costmodel output for downstream debugging.",
    )
