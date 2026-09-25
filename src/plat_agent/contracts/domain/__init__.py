"""Per-domain request/response payload models for sibling agents.

Each module here defines the *payload* that goes inside the BridgeRequestV1
or BridgeResponseV1 envelope for one sibling agent. Keep payloads small and
forward-compatible: prefer Optional fields and don't rename existing fields
without bumping the contract version.
"""
