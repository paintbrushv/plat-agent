"""Payloads for supply-demand/supply-pipeline-analyst."""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class SupplyPipelineRequest(BaseModel):
    market: str = Field(description="Metro identifier — 'dfw', 'midland_odessa', etc.")
    horizon_years: int = Field(default=3, ge=1, le=10)


class SupplyPipelineResponse(BaseModel):
    market: str
    delivered_units_ttm: Optional[int] = None
    under_construction_units: Optional[int] = None
    planned_units: Optional[int] = None
    absorption_units_ttm: Optional[int] = None
    months_of_supply: Optional[float] = None
    summary_relative: str
    notes: list[str] = Field(default_factory=list)
