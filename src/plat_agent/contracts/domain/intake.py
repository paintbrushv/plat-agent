"""Payloads for multifamily-underwriting/deal-intake."""

from __future__ import annotations

from datetime import date
from typing import Literal, Optional

from pydantic import BaseModel, Field


StrikePriceBasis = Literal[
    "om_published",
    "broker_whisper",
    "broker_whisper_minus_5pct",
    "broker_whisper_minus_10pct",
    "cap_rate_derived",
    "analyst_target",
    "other",
]

OMPricingProcess = Literal[
    "published_asking_price",
    "best_offers_loi_unpriced",
    "call_for_offers",
    "unknown",
]


class PricingProvenance(BaseModel):
    """Captures OM-vs-whisper-vs-strike pricing context.

    Mirrors the canonical schema v0.1 ``pricing_provenance`` section.
    When emitted, ``strike_price`` MUST equal
    ``purchase_assumptions.purchase_price`` — the multifamily-underwriting
    validator rejects mismatches as ``SCHEMA_VIOLATION``.
    """

    published_om_price: Optional[float] = Field(
        default=None,
        ge=0,
        description="Listed OM asking price; None when offering is unpriced.",
    )
    broker_whisper_price: Optional[float] = Field(
        default=None,
        ge=0,
        description="Verbal whisper / guidance from broker; None when no whisper.",
    )
    strike_price: float = Field(
        ge=0,
        description="Underwritten price. MUST equal purchase_assumptions.purchase_price.",
    )
    strike_price_basis: StrikePriceBasis = Field(
        description="Categorical label for how strike was set.",
    )
    strike_price_derivation: str = Field(
        min_length=1,
        description="Recipe-style explanation (e.g. '76M whisper × 0.95 ≈ 72.2M').",
    )
    om_pricing_process: Optional[OMPricingProcess] = Field(
        default=None,
        description="Marketing process the broker is running.",
    )
    as_of_date: date = Field(
        description="When this provenance snapshot was recorded.",
    )


class DealIntakeRequest(BaseModel):
    """Ask the sibling to ingest raw_inputs/ for a deal into canonical JSON."""

    raw_inputs_dir_relative: str = Field(
        default="raw_inputs",
        description="Path relative to deal_root containing OM, rent roll, T12, etc.",
    )
    property_id_hint: Optional[str] = Field(
        default=None,
        description="Optional analyst-provided property name. If omitted, the "
        "sibling agent infers from the OM.",
    )
    period_start: Optional[str] = Field(
        default=None, description="YYYY-MM start of the underwriting period."
    )
    period_end: Optional[str] = Field(
        default=None, description="YYYY-MM end of the underwriting period."
    )


class DealIntakeResponse(BaseModel):
    canonical_deal_json_relative: str = Field(
        description="Path (relative to deal_root) to the canonical deal JSON "
        "produced by ingest_deal.py."
    )
    manifest_relative: str = Field(
        description="Path to deal_manifest.md (created or updated)."
    )
    punchlist_relative: str = Field(
        description="Path to intake_punchlist.md listing analyst-required fields."
    )
    cohorts_identified: int
    documents_classified: dict[str, str] = Field(
        default_factory=dict,
        description="Map of source filename → document type "
        "('om'|'rent_roll'|'t12'|'capex_quote'|'tax_doc'|'box_score'|'debt_guidance'|'unknown').",
    )
    pricing_provenance: Optional[PricingProvenance] = Field(
        default=None,
        description="OM-vs-whisper-vs-strike pricing context, when ingest "
        "captured it. Optional; legacy intakes may omit. When present, "
        "strike_price must equal purchase_assumptions.purchase_price in the "
        "canonical deal JSON.",
    )
