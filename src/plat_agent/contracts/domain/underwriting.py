"""Payloads for multifamily-underwriting/underwriting-runner."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


class UnderwritingRequest(BaseModel):
    canonical_deal_json_relative: str
    renovation_programs_relative: Optional[str] = Field(
        default=None,
        description="Optional path to schema-mapped renovation_programs from "
        "the cost-bridge step. Spliced into the deal before running.",
    )
    output_workbook: bool = Field(
        default=False,
        description="If True, also write the Excel pro forma alongside the "
        "summary metrics.",
    )


class UnderwritingMetrics(BaseModel):
    levered_irr: Optional[float] = None
    equity_multiple: Optional[float] = None
    min_dscr: Optional[float] = None
    avg_dscr: Optional[float] = None
    going_in_cap: Optional[float] = None
    exit_cap: Optional[float] = None


class UnderwritingResponse(BaseModel):
    metrics: UnderwritingMetrics
    feasibility_verdict: str = Field(
        description=(
            "'pass' | 'fail' | 'marginal' — derived MECHANICALLY from "
            "sanity_flags per the underwriting-runner agent's "
            "'Verdict classification' rule. Not from check_deal_feasibility "
            "(that MCP tool returns only a boolean and is not used)."
        )
    )
    feasibility_reasons: list[str] = Field(default_factory=list)
    summary_relative: str = Field(
        description="Path to deal_summary.json with full engine output."
    )
    workbook_relative: Optional[str] = None


class UnderwritingProvenance(BaseModel):
    """Standardized provenance for underwriting-runner sibling output.

    Mirrors `engine.api._build_provenance` shape produced by the
    multifamily-underwriting engine. Persisted to
    ``outputs/<run_id>/underwriting/_provenance.json`` by the runner so the
    orchestrator (and downstream memo writer) can surface engine version,
    inputs hash, validator status, and feasibility verdict alongside the
    metrics — without re-loading the full deal_summary.json.

    Stage 5 HIGH-2 (audit 2026-04-26): the runner emits provenance today as
    a free-form dict; this model nails it down so dispatch can validate it
    via the new `payload_model` parameter to `dispatch_sibling_agent`.
    """

    engine_version: str = Field(
        description="Underwriting engine package version (e.g. '0.4.2')."
    )
    schema_version: str = Field(
        description="Canonical deal-input schema version this run was "
        "validated against."
    )
    inputs_hash_sha256: str = Field(
        description="SHA-256 of the canonical deal-input JSON the engine ran "
        "on, after splice. Used to detect input drift across re-runs."
    )
    generated_at_utc: datetime = Field(
        description="UTC timestamp at which the engine produced this output."
    )
    validator_status: Literal["PASS", "WARN", "FAIL", "SKIPPED"] = Field(
        description="Outcome of `engine.validator.validate_deal` on the "
        "spliced canonical."
    )
    validator_issues: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Serialized ValidationIssue list — one entry per WARN/FAIL "
        "raised by the validator.",
    )
    feasibility_verdict: Literal["pass", "marginal", "fail"] = Field(
        description="Mechanical verdict derived from sanity_flags by the "
        "runner's classification rule."
    )
    feasibility_sanity_flags: list[str] = Field(
        default_factory=list,
        description="Plain-text flags (e.g. 'cap_rate_outside_4_7_band') that "
        "drove the verdict.",
    )
    feasibility_reasons: list[str] = Field(
        default_factory=list,
        description="Human-readable rationale strings matching "
        "feasibility_sanity_flags 1:1 where possible.",
    )
    cap_rate_derivation: Optional[dict[str, Any]] = Field(
        default=None,
        description="Optional breakdown of going-in vs exit cap, NOI source, "
        "comparable-set provenance — present when the runner records it.",
    )

    # Federation-specific (set by plat-agent orchestrator, not engine).
    dispatch_request_id: Optional[str] = Field(
        default=None,
        description="BridgeRequestV1 correlation id assigned by the "
        "orchestrator. Lets the memo writer cross-link metrics back to the "
        "dispatch log line.",
    )
    elapsed_seconds: Optional[float] = Field(
        default=None,
        description="Wall-clock seconds the engine took to produce this run.",
    )
