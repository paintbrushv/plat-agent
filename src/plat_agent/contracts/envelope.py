"""Wire-format envelope for plat-agent ↔ sibling-agent dispatch.

Every sibling agent invoked via `claude -p --cwd <sibling_repo>` receives a
BridgeRequestV1 (JSON on stdin or as the prompt body) and must emit a
BridgeResponseV1 to stdout. The orchestrator parses stdout, validates against
this schema, and surfaces structured errors when the sibling deviates.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


CONTRACT_VERSION = "v1"


class ProvenanceEntry(BaseModel):
    """One claim about where a value came from.

    Every non-trivial value a sibling agent produces should carry at least one
    provenance entry so the memo writer can hyperlink back to the source.
    """

    source: str = Field(
        description="Source identifier — filename, URL, dataset name, or sibling-agent name."
    )
    locator: Optional[str] = Field(
        default=None,
        description="Where in the source: page number, sheet/cell, row id, query, line range.",
    )
    extracted_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    note: Optional[str] = Field(
        default=None,
        description="Why this value was chosen, mappings the agent had to guess at, etc.",
    )


class ArtifactRef(BaseModel):
    """Pointer to a file written by the sibling agent.

    Artifacts land in runs/deals/<slug>/outputs/run_NNN/<domain>/ inside the
    multifamily-underwriting repo. The path is relative to the deal directory
    so the orchestrator can resolve it without knowing the absolute deal root.
    """

    relative_path: str = Field(
        description="Path relative to runs/deals/<slug>/outputs/run_NNN/."
    )
    kind: Literal["json", "csv", "xlsx", "xlsm", "pdf", "md", "html", "image"] = "json"
    description: str = Field(
        description="One-line human-readable summary of what's in the file."
    )
    bytes: Optional[int] = None


class BridgeError(BaseModel):
    """Structured error from a sibling agent.

    Use `code` for programmatic handling; `message` for human readers.
    Raise this in the response (status='error') instead of crashing — the
    orchestrator can route around a recoverable error but cannot parse a
    Python traceback.
    """

    code: str = Field(
        description="Stable identifier — e.g. 'missing_input', 'data_quality', "
        "'roi_gate_failed', 'not_bracketed', 'sibling_repo_unavailable'."
    )
    message: str = Field(description="Human-readable explanation.")
    recoverable: bool = Field(
        default=False,
        description="True if the orchestrator can retry with adjusted inputs; "
        "False if analyst intervention is required.",
    )
    details: dict[str, Any] = Field(default_factory=dict)


class BridgeRequestV1(BaseModel):
    """Envelope sent from plat-agent orchestrator to a sibling agent.

    `payload` carries the per-domain request shape (CompFinderRequest,
    UnderwritingRequest, etc. — see contracts.domain).
    """

    contract_version: Literal["v1"] = "v1"
    deal_slug: str = Field(
        description="Stable deal identifier — folder name under "
        "multifamily-underwriting/runs/deals/. Used to resolve artifact paths."
    )
    run_id: str = Field(
        description="Per-execution identifier — e.g. 'run_001'. Multiple runs "
        "of the same deal accumulate side-by-side under outputs/."
    )
    deal_root: str = Field(
        description="Absolute path to the deal directory "
        "(runs/deals/<slug>/). Sibling resolves artifact writes from here."
    )
    agent_name: str = Field(
        description="Which sibling agent is being invoked — "
        "matches a file under <sibling_repo>/.claude/agents/."
    )
    payload: dict[str, Any] = Field(
        description="Domain-specific request body. Validate against the matching "
        "contracts.domain.* model on the sibling side."
    )
    requested_by: str = Field(
        default="plat-agent",
        description="Identifier of the orchestrator dispatching this request.",
    )


class BridgeResponseV1(BaseModel):
    """Envelope returned from a sibling agent to plat-agent orchestrator.

    Sibling agents MUST emit exactly one JSON document matching this schema
    to stdout. Anything else (logs, prose, partial JSON) is a contract
    violation and the orchestrator will treat the response as an error.
    """

    contract_version: Literal["v1"] = "v1"
    status: Literal["ok", "error", "needs_analyst_input"] = "ok"
    deal_slug: str
    run_id: str
    agent_name: str
    # V1.4 — payload is intentionally Optional. The pre-V1.4 schema required
    # `dict` and crashed pydantic strict-validation when a sibling agent
    # legitimately returned `payload=None` (e.g. deal-intake hit an
    # unrecoverable extraction failure and produced no canonical output).
    # `status` already distinguishes ok/error/needs_analyst_input, so a
    # `None` payload is a valid "step ran but produced nothing usable"
    # signal. Step adapters MUST tolerate `payload=None` and degrade
    # gracefully (write fallback artifacts + emit a blocker).
    payload: Optional[dict[str, Any]] = Field(
        default_factory=dict,
        description="Domain-specific response body. Empty dict when status='error'; "
        "None signals the agent ran but produced no usable payload (V1.4).",
    )
    artifacts: list[ArtifactRef] = Field(
        default_factory=list,
        description="Files the sibling agent wrote during the run.",
    )
    provenance: list[ProvenanceEntry] = Field(
        default_factory=list,
        description="Citations supporting non-trivial values in the payload.",
    )
    error: Optional[BridgeError] = Field(
        default=None,
        description="Required when status='error' or 'needs_analyst_input'.",
    )
    sanity_flags: list[str] = Field(
        default_factory=list,
        description="Plain-text flags the orchestrator should surface to the "
        "analyst even on a successful run — e.g. 'cap_rate_outside_4_7_band'.",
    )
