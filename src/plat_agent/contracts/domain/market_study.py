"""Payloads for market-study-agent/comp-finder + demographics-analyst."""

from __future__ import annotations

import re
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator

# Canonical regex for cohort keys; cross-agent contract.
# Format: "<bedrooms>BR_<bathrooms>BA_<sqft>sf"
# Example: "2BR_1.5BA_977sf"
COHORT_KEY_PATTERN: str = r"^\d+BR_\d+(\.\d)?BA_\d+sf$"
_COHORT_KEY_RE: re.Pattern[str] = re.compile(COHORT_KEY_PATTERN)


def cohort_key(bedrooms: int, bathrooms: float, sqft: int) -> str:
    """Canonical cohort key string for cross-agent communication.

    Format: '<bedrooms>BR_<bathrooms>BA_<sqft>sf'
    bathrooms rounded to 1 decimal; sqft to int.
    Example: cohort_key(2, 1.5, 977) -> '2BR_1.5BA_977sf'

    All callers (request builders + comp-finder agent body) MUST use this
    formatter — never inline string interpolation. This guarantees the
    output matches `COHORT_KEY_PATTERN` and round-trips cleanly across
    plat-agent / market-study-agent boundaries.
    """
    return f"{int(bedrooms)}BR_{float(bathrooms):.1f}BA_{int(sqft)}sf"


def _validate_cohort_key(key: str) -> str:
    """Raise ValueError if `key` does not match the canonical format."""
    if not _COHORT_KEY_RE.match(key):
        raise ValueError(
            f"cohort key '{key}' does not match canonical format "
            f"'{COHORT_KEY_PATTERN}'. Use cohort_key() to build keys."
        )
    return key


class CompFinderRequest(BaseModel):
    coverage_signal: "CompCoverageSignal | None" = Field(
        default=None,
        description=(
            "plat-agent assessment of whether the broker OM seed is too thin "
            "to trust on its own, and whether comp-finder should expand the "
            "local comp universe beyond the broker list."
        ),
    )
    bootstrap_plan: "MarketStudyBootstrapPlan | None" = Field(
        default=None,
        description=(
            "plat-agent preflight describing whether market-study-agent should "
            "run /new-metro, /new-property, /full-onboarding, /pull-comps, "
            "and /analyze-comps before translating artifacts back to the "
            "lifecycle contract."
        ),
    )
    subject_name: str | None = Field(
        default=None,
        description="Subject property name when intake could resolve it.",
    )
    property_address: str
    subject_units: int | None = Field(
        default=None,
        description="Total unit count for the subject property when known.",
    )
    market: str = Field(description="City or submarket name — 'dallas', 'midland_tx', etc.")
    unit_mix_summary: list[dict] = Field(
        description="List of {bedrooms, bathrooms, sqft, count} for the subject's cohorts."
    )
    om_comp_seed: list[dict] = Field(
        default_factory=list,
        description="Optional broker OM rent comp seed rows for market-study YAML bootstrap.",
    )
    om_source_relative: str | None = Field(
        default=None,
        description="Deal-root-relative OM path that produced om_comp_seed, when available.",
    )
    radius_miles: float = Field(default=2.0, gt=0)
    max_age_months: int = Field(
        default=18, description="Skip lease comps older than this."
    )
    year_built_min: int | None = Field(
        default=None,
        description="Minimum vintage year for comps. Filters out comps newer than this.",
    )
    year_built_max: int | None = Field(
        default=None,
        description="Maximum vintage year for comps. Filters out older comps.",
    )
    class_tier: Literal["A", "B", "C"] | None = Field(
        default=None,
        description="Property class tier filter. Comps must match.",
    )
    renovation_status: Literal["classic", "light", "mid", "gut"] | None = Field(
        default=None,
        description="Renovation status filter for value-add comp matching.",
    )


class CompEntry(BaseModel):
    property_name: str
    address: Optional[str] = None
    distance_miles: Optional[float] = None
    year_built: Optional[int] = None
    bedrooms: int
    bathrooms: float
    sqft: float
    asking_rent: float
    rent_per_sqft: float
    source: str
    source_url: Optional[str] = None


class CompCoverageSignal(BaseModel):
    source: Literal["broker_om_seed"] = "broker_om_seed"
    broker_seed_comp_count: int = Field(ge=0)
    minimum_total_comp_count: int = Field(default=5, ge=1)
    dominant_cohort_key: str | None = None
    dominant_bedrooms: int | None = Field(default=None, ge=0)
    dominant_unit_count: int | None = Field(default=None, ge=0)
    minimum_dominant_cohort_comp_count: int = Field(default=3, ge=1)
    requires_universe_expansion: bool = False
    reasons: list[str] = Field(default_factory=list)


class MarketStudySkillCommand(BaseModel):
    skill_name: Literal[
        "new-metro",
        "new-property",
        "full-onboarding",
        "pull-comps",
        "analyze-comps",
    ]
    command: str
    reason: str


class MarketStudyBootstrapPlan(BaseModel):
    market_study_repo_relative: str | None = Field(
        default=None,
        description="Deal-root-relative path to the market-study-agent repo when resolvable.",
    )
    metro_slug: str
    metro_dir_slug: str
    property_slug: str
    raw_rent_roll_relative: str | None = Field(
        default=None,
        description="Deal-root-relative raw rent roll path for /full-onboarding when available.",
    )
    metro_config_relative: str | None = None
    metro_reports_dir_relative: str | None = None
    property_config_relative: str | None = None
    property_reports_dir_relative: str | None = None
    floorplan_summary_relative: str | None = None
    metro_exists: bool
    property_exists: bool
    property_onboarded: bool
    workflow_stage: Literal[
        "new_metro",
        "new_property",
        "full_onboarding",
        "existing_property",
    ]
    recommended_skill_commands: list[MarketStudySkillCommand] = Field(default_factory=list)
    analyst_report_required: bool = Field(
        default=True,
        description=(
            "When true, comp-finder should finish with /analyze-comps so the "
            "repo-native comp_analysis markdown stays current alongside the federation JSON."
        ),
    )


class CompFinderResponse(BaseModel):
    comps_by_cohort: dict[str, list[CompEntry]] = Field(
        description="Keyed by cohort identifier — e.g. '2BR_1BA_850sf'."
    )
    comps_relative: str = Field(
        description="Path to comps.json with full comp set."
    )
    methodology_notes: list[str] = Field(
        default_factory=list,
        description="What the agent included/excluded and why. Analyst reads "
        "this before trusting the comps for target-rent setting.",
    )

    @field_validator("comps_by_cohort")
    @classmethod
    def _check_cohort_keys(
        cls, value: dict[str, list[CompEntry]]
    ) -> dict[str, list[CompEntry]]:
        for key in value:
            _validate_cohort_key(key)
        return value


class CompReconciliationEntry(BaseModel):
    """Per-cohort calibration of canonical market_rent against comp p50."""

    cohort_id: str = Field(
        description="Canonical cohort identifier — matches the cohort_id in "
        "unit_cohorts / renovation_programs.target_cohort. NOTE: this is the "
        "cohort_id (e.g. 'beal'), NOT the cohort_key (e.g. '0BR_1.0BA_530sf')."
    )
    canonical_market_rent: float = Field(
        description="Canonical market_rent_curve value at month 1 for this cohort."
    )
    comp_p50_rent: float = Field(
        description="Median asking rent across the comp set for this cohort."
    )
    comp_count: int = Field(
        ge=0,
        description="Number of comps backing comp_p50_rent.",
    )
    divergence_pct: float = Field(
        description="Signed percent divergence: "
        "(comp_p50 - canonical) / canonical * 100. "
        "Positive means comps suggest higher rent than canonical assumes."
    )
    recommendation: str = Field(
        description="Plain-text analyst-facing recommendation (e.g. 'within "
        "tolerance', 'review and consider upward adjustment', etc.)."
    )


class CompReconciliationResult(BaseModel):
    """Payload emitted by the comp-reconciler agent.

    Per Wave 6 Task 6.2 (CONSOLIDATED_FIX_PLAN.md). The reconciler runs
    AFTER comp-finder and BEFORE underwriting-runner. Its job is to
    cross-check the canonical's `market_rent_curve` (set by the analyst,
    sourced from OM + judgment) against the median comp rent for each
    cohort. Disagreement >10% emits a HARD warning sanity_flag — but
    advisory only, per user's Q2 answer (does NOT block the chain).

    The persisted on-disk artifact lives at
    `<deal_root>/outputs/<run_id>/market_study/comp_reconciliation.json`.
    """

    market_rent_calibration: list[CompReconciliationEntry] = Field(
        default_factory=list,
        description="Per-cohort calibration entries — one per cohort with "
        "non-zero rent_premium AND comp coverage.",
    )
    divergence_threshold_pct: float = Field(
        default=10.0,
        description="Absolute percent threshold above which a "
        "comp_disagreement_<cohort> sanity_flag is emitted. Default 10%.",
    )
    advisory_only: bool = Field(
        default=True,
        description="When true (default per Wave 6 Q2), divergence >threshold "
        "emits a HARD warn flag but does NOT halt the federation chain. "
        "Analyst can override by accepting the canonical and rerunning.",
    )
    cohorts_lacking_comp_evidence: list[str] = Field(
        default_factory=list,
        description="Cohort IDs with non-zero rent_premium but no comp set "
        "in comps.json — surfaced for analyst follow-up. NOTE: when this "
        "list is non-empty AND the upstream gate has not already blocked "
        "the run, the reconciler emits an advisory flag.",
    )


class ProposedRent(BaseModel):
    """One unit cohort with the analyst's proposed target rent."""

    bedrooms: int = Field(ge=0)
    bathrooms: float = Field(gt=0)
    sqft: float = Field(gt=0)
    proposed_target_monthly_rent: float = Field(
        gt=0,
        description="Analyst's proposed target rent for this cohort. The "
        "validator checks this against the comp set's distribution.",
    )


class RentValidationRequest(BaseModel):
    """plat-agent → market-study-agent: validate per-cohort target rents.

    Distinct from CompFinderRequest — that fetches raw comps. This validates
    a *specific* set of analyst-proposed target rents against an existing
    comp set, returning per-cohort verdicts. Useful before running the
    underwriting engine, so the analyst sees whether their target_monthly_rent
    inputs are defensible against market evidence.

    The `comps_by_cohort` payload mirrors CompFinderResponse.comps_by_cohort —
    the validator does not refetch; it consumes the analyst's existing comp
    set. This keeps the validator pure and deterministic.
    """

    proposed_rents: list[ProposedRent]
    comps_by_cohort: dict[str, list[CompEntry]] = Field(
        description="Output of comp-finder, keyed by canonical cohort_key."
    )

    @field_validator("comps_by_cohort")
    @classmethod
    def _check_cohort_keys(
        cls, value: dict[str, list[CompEntry]]
    ) -> dict[str, list[CompEntry]]:
        for key in value:
            _validate_cohort_key(key)
        return value


class RentValidationFinding(BaseModel):
    """Per-cohort validation result.

    `verdict` semantics:
        within_band         — proposed rent in [p25, p75]; defensible
        above_band          — > p75; aggressive vs comp set
        below_band          — < p25; conservative vs comp set
        insufficient_comps  — fewer than MIN_COMPS_FOR_VERDICT comps; no verdict
    """

    cohort_key: str
    proposed_rent: float
    comp_count: int = Field(ge=0)
    comp_p25: Optional[float] = None
    comp_median: Optional[float] = None
    comp_p75: Optional[float] = None
    verdict: Literal["within_band", "above_band", "below_band", "insufficient_comps"]
    confidence: Literal["high", "medium", "low", "none"]
    message: str

    @field_validator("cohort_key")
    @classmethod
    def _check_cohort_key(cls, value: str) -> str:
        return _validate_cohort_key(value)


class RentValidationResponse(BaseModel):
    """market-study-agent → plat-agent: per-cohort rent validation findings."""

    findings: list[RentValidationFinding]
    summary: str = Field(
        description="One-line analyst-facing summary across all cohorts."
    )


class DemographicsRequest(BaseModel):
    market: str
    geo_identifier: Optional[str] = Field(
        default=None,
        description="Census tract, ZIP, or county FIPS. If omitted, the agent "
        "resolves from the property address in the deal manifest.",
    )


class DemographicsResponse(BaseModel):
    population: Optional[int] = None
    median_household_income: Optional[float] = None
    employment_growth_5yr_pct: Optional[float] = None
    household_formation_5yr_pct: Optional[float] = None
    summary_relative: str


# ---------------------------------------------------------------------------
# Lifecycle spec §2.2 comps.json artifact schema.
#
# DISTINCT from CompFinderResponse (the BridgeResponseV1 payload). The
# response is the wire envelope between plat-agent and market-study-agent.
# CompsArtifact is the on-disk file at outputs/<run_id>/comps/comps.json
# that downstream judgment + memo subsystems consume. The comp-finder agent
# is responsible for emitting BOTH (the response envelope contains a
# `comps_relative` pointer to the on-disk artifact).
# ---------------------------------------------------------------------------


class CompsArtifactSubject(BaseModel):
    address: str = Field(min_length=1)
    submarket: Optional[str] = None
    metro_slug: str = Field(min_length=1)
    metro_display: Optional[str] = None


class CompsArtifactUnitType(BaseModel):
    unit_type: str
    sqft: Optional[float] = None
    face_rent: Optional[float] = None
    effective_rent: Optional[float] = None
    rent_psf: Optional[float] = None
    units_available: Optional[int] = None
    mom_change: Optional[float] = None
    yoy_change: Optional[float] = None
    concession: Optional[str] = None


class CompsArtifactComp(BaseModel):
    # Required per §2.2
    comp_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    address: str = Field(min_length=1)
    units: int = Field(gt=0)
    # Optional per §2.2 — absence indicates degraded coverage.
    distance_miles: Optional[float] = None
    year_built: Optional[int] = None
    stories: Optional[int] = None
    owner: Optional[str] = None
    management_company: Optional[str] = None
    ownership_type: Optional[
        Literal["private", "reit", "private_equity", "other"]
    ] = None
    renovation_status: Optional[
        Literal["renovated", "partial", "classic", "unknown"]
    ] = None
    renovation_year: Optional[int] = None
    condition_rating: Optional[str] = None
    last_sale_date: Optional[str] = None
    last_sale_price: Optional[float] = None
    last_sale_ppu: Optional[float] = None
    cap_rate_est: Optional[float] = None
    tier: Optional[Literal["value", "mid_market", "premium"]] = None
    unit_types: list[CompsArtifactUnitType] = Field(default_factory=list)


class CompsArtifactSubmarketAggregates(BaseModel):
    rent_growth_trailing_12mo: Optional[float] = None
    submarket_vacancy: Optional[float] = None
    comp_count: Optional[int] = None


class CompsArtifact(BaseModel):
    """Lifecycle spec §2.2 on-disk schema for outputs/<run_id>/comps/comps.json.

    `CompsStep.run()` schema-validates the file the comp-finder agent wrote
    against this model. Validation failure → comps step hard error per §4.1.
    """

    subject: CompsArtifactSubject
    as_of: str = Field(
        min_length=10,
        description="ISO YYYY-MM-DD date the comp run executed.",
    )
    comps: list[CompsArtifactComp] = Field(min_length=1)
    submarket_aggregates: Optional[CompsArtifactSubmarketAggregates] = None
    raw_snapshot_relative: Optional[str] = None
