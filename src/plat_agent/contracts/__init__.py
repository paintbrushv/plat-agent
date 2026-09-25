"""Bridging contracts between plat-agent orchestrators and sibling-repo agents.

The envelope (BridgeRequestV1, BridgeResponseV1) is the wire format every
sibling agent must accept on stdin/argv and emit to stdout. Per-domain
request/response payloads live under contracts.domain and are nested inside
the envelope's `payload` field.

See docs/bridging_contract.md for the full spec.
"""

from plat_agent.contracts.envelope import (
    ArtifactRef,
    BridgeError,
    BridgeRequestV1,
    BridgeResponseV1,
    ProvenanceEntry,
)

__all__ = [
    "ArtifactRef",
    "BridgeError",
    "BridgeRequestV1",
    "BridgeResponseV1",
    "ProvenanceEntry",
]
