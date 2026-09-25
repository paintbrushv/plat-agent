"""Pluggable backend for running one underwriting scenario.

RunBackend is the seam between sweep code (scenarios, break_even) and whatever
actually executes the underwriting engine. v1 ships LocalRunBackend, which
wraps the existing UnderwritingClient.run_summary. A future AzureRunBackend
(POST /api/runs + poll) plugs in by implementing the same protocol — no
changes to sweep code required.

The protocol is deliberately narrow: one deal in, normalized metrics out.
Batch / parallel semantics stay inside the backend implementation, not the
protocol.
"""

import os
from typing import Literal, Protocol

from ..underwriting_client import UnderwritingClient

BackendName = Literal["local", "azure", "auto"]


class RunBackend(Protocol):
    """Executes one deal and returns normalized nested metrics.

    The returned dict must use the nested shape produced by
    UnderwritingClient.run_summary / analyzer._normalize_full_metrics:
        {
            "status": "success" | "error",
            "irr": {"levered_irr": ..., "unlevered_irr": ...},
            "equity_multiple": {"levered_em": ..., "unlevered_em": ...},
            "dscr": {"minimum": ..., "average": ...},
            "yields": {"going_in_cap_rate": ...},
            ...
        }
    On error, status="error" and an "error" key with a message.
    """

    def run_deal(self, deal_inputs: dict) -> dict: ...


class LocalRunBackend:
    """v1 backend — wraps UnderwritingClient.run_summary (MCP path).

    The MCP summary path is fast (~1-3s per call) and already returns the
    normalized nested shape, so no additional translation is needed.
    """

    def __init__(self, client: UnderwritingClient | None = None):
        self._client = client if client is not None else UnderwritingClient()

    def run_deal(self, deal_inputs: dict) -> dict:
        return self._client.run_summary(deal_inputs)


def make_backend(name: BackendName | None = "auto") -> RunBackend:
    """Return a RunBackend by name.

    "local" → LocalRunBackend (MCP path).
    "azure" → AzureRunBackend (env-configured).
    "auto" / None → AzureRunBackend if UNDERWRITING_AZURE_ENDPOINT is set,
                    otherwise LocalRunBackend.
    """
    resolved = name or "auto"
    if resolved == "auto":
        resolved = "azure" if os.environ.get("UNDERWRITING_AZURE_ENDPOINT") else "local"
    if resolved == "local":
        return LocalRunBackend()
    if resolved == "azure":
        from .azure_backend import AzureRunBackend
        return AzureRunBackend()
    raise ValueError(f"unknown backend: {name!r} (expected 'local', 'azure', or 'auto')")
