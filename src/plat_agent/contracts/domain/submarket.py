"""Payloads for submarket-atlas/submarket-scorer."""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class SubmarketScorerRequest(BaseModel):
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    parcel_id: Optional[str] = Field(
        default=None,
        description="Local parcel identifier (e.g. DCAD account number).",
    )
    market: str


class SubmarketScorerResponse(BaseModel):
    submarket_name: str
    composite_score: float = Field(ge=0, le=100)
    score_components: dict[str, float] = Field(
        default_factory=dict,
        description="Named subscores — e.g. 'demand', 'supply_pressure', "
        "'income', 'school_quality'.",
    )
    summary_relative: str
