"""Lifecycle state types — the source of truth for 'where is this run'.

Persisted as JSON at runs/deals/<slug>/outputs/<run_id>/_lifecycle_state.json.
Updated atomically after every step. See spec §2.7.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field


# Status strings from spec §4.3 dependency map. Treated as string literals
# rather than a strict enum so V2 can extend without schema break.
LifecycleStatus = Literal[
    "running",
    "memo_ready",
    "needs_analyst_input",
    "memo_ready_with_blockers",
    "failed_at_intake",
    "failed_at_comps",
    "failed_at_judgment",
    "failed_at_underwriting",
    "failed_at_memo",
    "failed_at_crm",
    "failed_at_punchlist_parse",
]


StepName = Literal["intake", "comps", "judgment", "underwriting", "memo", "crm"]


class RecommendationEnum(StrEnum):
    """V1 recommendation values per spec §2.3."""

    PROCEED = "PROCEED"
    DECLINE = "DECLINE"
    NEEDS_DATA = "NEEDS_DATA"


class BlockerItem(BaseModel):
    """One item in punchlist.json. Stable id is the machine-readable anchor.

    `resolution_hint` is an optional, analyst-facing one-liner explaining
    what to do to clear the blocker. Per spec §4.2 markdown format, it is
    rendered as an indented continuation line beneath the blocker bullet.
    """

    step: StepName
    id: str  # e.g. "missing_t12", "rent_roll_dirty", "scraper_blocked"
    description: str
    resolution_hint: str | None = None
    field: str | None = None
    unit: str | None = None
    evidence_locations: list[str] = Field(default_factory=list)
    submitted_value: str | None = None
    cleared: bool = False
    created_at: datetime | None = None


class LifecycleState(BaseModel):
    """Per-run lifecycle state. Persisted at outputs/<run_id>/_lifecycle_state.json."""

    deal_slug: str
    run_id: str
    status: LifecycleStatus = "running"
    steps_completed: list[StepName] = Field(default_factory=list)
    blockers: list[BlockerItem] = Field(default_factory=list)
    started_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    finished_at: datetime | None = None
    recommendation_patched_at: datetime | None = None  # set by Step 4.5 post-step
    # Per spec §5.4 passthrough guardrail: orchestrator persists this so the
    # MemoStep banner check + CRMStep judgment_mode_override field can read
    # the actual run-time value (not a default re-derived from provenance).
    judgment_mode: str = "deterministic_v1"
