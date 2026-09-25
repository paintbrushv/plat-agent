"""Pydantic data models for plat-agent deal inputs and outputs."""

from typing import Optional
from pydantic import BaseModel, Field


class UnitMixEntry(BaseModel):
    """One unit type in the deal's unit mix.

    Use count > 1 for multiple identical units (e.g. 30 × 2BR/1BA 850sf).
    Rent fields are required — Plat does not derive them internally.
    """
    sqft: float = Field(gt=0)
    bedrooms: int = Field(ge=0)
    bathrooms: int = Field(ge=1)
    count: int = Field(default=1, ge=1)
    scope_level: str = "standard_value_add"
    finish_tier: str = "basic"
    current_monthly_rent: float = Field(gt=0)
    target_monthly_rent: float = Field(gt=0)


class DealInputs(BaseModel):
    """Full deal input for a single property.

    The analyst provides all fields including rent assumptions.
    Plat never derives rent internally — that is always an explicit
    analyst decision (eventually informed by market-study-agent comps).

    For full underwriting runs, provide base_deal_inputs with the
    canonical schema v0.1 (unit_cohorts, market_rent_curve, etc.).
    Plat will merge generated renovation_programs into it.
    """
    property_id: str
    total_units: int = Field(ge=0)
    unit_mix: list[UnitMixEntry]

    # Property context — passed through to plat-costmodel
    year_built: Optional[int] = None
    property_class: Optional[str] = None   # "B" or "C"
    market: Optional[str] = None           # "dallas", "birmingham", etc.
    building_type: str = ""
    exterior_items: Optional[list[str]] = None

    # Renovation program parameters
    start_month: str                        # "YYYY-MM"
    monthly_pace: int = Field(ge=1)
    downtime_days: int = Field(default=21, ge=0)
    roi_threshold_pct: float = Field(default=15.0, gt=0)
    renovation_strategy: str = "on_turnover"  # "on_turnover" or "proactive"

    # Optional: full canonical deal inputs for underwriting engine
    # If provided, Plat merges renovation_programs into this and runs
    # the underwriting engine for full cashflow analysis.
    base_deal_inputs: Optional[dict] = None

    # When True and base_deal_inputs is provided, Plat calls the underwriting
    # engine's full-cashflow path (direct import) instead of the MCP summary
    # tool. Yields the complete monthly cashflow and renovation tracking on
    # DealAnalysis.underwriting_full, with summary metrics still populated on
    # underwriting_metrics in the normalized (nested) shape.
    full_cashflow: bool = False


class UnitTypeResult(BaseModel):
    """Estimate result for one unit type (one UnitMixEntry)."""
    sqft: float
    bedrooms: int
    bathrooms: int
    count: int
    cost_estimate_low: float
    cost_estimate_high: float
    roi_pct: float
    roi_passes: bool
    renovation_program: Optional[dict] = None   # None if ROI gate failed
    roi_result: dict = {}

    # Decision-relevant ROI diagnostics promoted from roi_result for memo
    # synthesis. All fields are unit-level (single unit, not × count).
    monthly_rent_lift: float = 0.0           # target - current rent ($/mo)
    annual_rent_lift: float = 0.0            # monthly_rent_lift * 12
    roi_gap_pp: float = 0.0                  # roi_pct - threshold_pct (negative = below)
    simple_payback_months: Optional[float] = None  # cost_high / monthly_lift; None if lift <= 0
    # Path-to-pass deltas — populated only when roi_passes is False.
    cost_reduction_needed_to_pass: Optional[float] = None  # $ to cut from cost
    rent_increase_needed_to_pass: Optional[float] = None   # $/mo to raise target rent


class DealAnalysis(BaseModel):
    """Full deal analysis output from Plat.

    renovation_programs is ready to splice into the underwriting engine's
    deal schema as the renovation_programs array.
    """
    property_id: str
    total_units: int
    ready_to_underwrite: bool               # True only if ALL unit types pass ROI gate

    unit_type_results: list[UnitTypeResult]

    # Property-level totals from plat-costmodel
    total_renovation_cost_low: float
    total_renovation_cost_high: float
    exterior_capex_low: float
    exterior_capex_high: float
    risk_flags: list[dict]

    # Underwriting inputs — splice directly into deal JSON
    renovation_programs: list[dict]

    # Underwriting engine results (populated when base_deal_inputs provided)
    underwriting_metrics: Optional[dict] = None   # IRR, EM, DSCR, cap rates (normalized nested shape)
    underwriting_feasible: Optional[bool] = None  # All gates passed
    underwriting_full: Optional[dict] = None      # Full engine response (cashflow, elapsed, etc.) — only in full mode

    # Sanity flags computed from underwriting_metrics — values outside
    # accepted multifamily ranges (cap 4-7%, DSCR >=1.2x, etc.). Empty when
    # underwriting did not run or all metrics are within band.
    sanity_flags: list[dict] = []

    # Memo-ready synthesis: headline verdict + binding constraints + the
    # specific changes that would unblock the deal. Derived from ROI gate
    # results, sanity flags, and risk flags. Designed so an analyst (or an
    # LLM acting on this JSON) can write a memo without re-deriving "why."
    decision_summary: dict = {}

    summary: str                            # human-readable deal summary
