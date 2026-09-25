"""Round-trip tests for the bridging envelope.

The envelope is the load-bearing piece of cross-repo dispatch — these tests
catch breaking schema changes the moment they regress.
"""

from __future__ import annotations

import json

from plat_agent.contracts import (
    ArtifactRef,
    BridgeError,
    BridgeRequestV1,
    BridgeResponseV1,
    ProvenanceEntry,
)
from plat_agent.contracts.domain.cost import CostBridgeRequest


def test_request_round_trips_through_json() -> None:
    payload = CostBridgeRequest(
        canonical_deal_json_relative="standardized/canonical_deal.json",
        roi_threshold_pct=15.0,
    ).model_dump()
    req = BridgeRequestV1(
        deal_slug="demo_on_west_lane",
        run_id="run_001",
        deal_root="/abs/path/to/runs/deals/demo_on_west_lane",
        agent_name="cost-bridge-analyst",
        payload=payload,
    )
    serialized = req.model_dump_json()
    rehydrated = BridgeRequestV1.model_validate_json(serialized)
    assert rehydrated == req
    assert json.loads(serialized)["payload"]["roi_threshold_pct"] == 15.0


def test_successful_response_carries_artifact_and_provenance() -> None:
    resp = BridgeResponseV1(
        deal_slug="demo_on_west_lane",
        run_id="run_001",
        agent_name="cost-bridge-analyst",
        payload={"ready_to_underwrite": True},
        artifacts=[
            ArtifactRef(
                relative_path="costmodel/property_estimate.json",
                kind="json",
                description="Property-level cost estimate (total_high basis).",
            )
        ],
        provenance=[
            ProvenanceEntry(
                source="plat-costmodel.estimate_property_from_model",
                locator="response.totals.total_high",
                note="Conservative bias — total_high used for all gating.",
            )
        ],
    )
    rehydrated = BridgeResponseV1.model_validate_json(resp.model_dump_json())
    assert rehydrated.status == "ok"
    assert len(rehydrated.artifacts) == 1
    assert len(rehydrated.provenance) == 1


def test_bridge_response_accepts_none_payload() -> None:
    """V1.4 — pydantic must accept payload=None on BridgeResponseV1.

    Willow Court regression: deal-intake legitimately returns payload=None when
    the agent prompt hits an unrecoverable extraction failure. Pre-V1.4 the
    strict `payload: dict` annotation crashed validation before V1.1 graceful
    degradation could fire.
    """
    resp = BridgeResponseV1(
        status="needs_analyst_input",
        deal_slug="willow_court",
        run_id="run_001",
        agent_name="deal-intake",
        payload=None,
        error=BridgeError(
            code="om_extraction_unavailable",
            message="OM PDF unparseable; no canonical body produced.",
            recoverable=True,
        ),
    )
    rehydrated = BridgeResponseV1.model_validate_json(resp.model_dump_json())
    assert rehydrated.payload is None
    assert rehydrated.status == "needs_analyst_input"


def test_error_response_uses_structured_bridge_error() -> None:
    resp = BridgeResponseV1(
        status="error",
        deal_slug="oak_ridge",
        run_id="run_001",
        agent_name="underwriting-runner",
        error=BridgeError(
            code="roi_gate_failed",
            message="Cohort 2BR_1BA_850sf failed ROI gate at 11.2% (threshold 15%).",
            recoverable=False,
            details={"failing_cohort": "2BR_1BA_850sf", "actual_roi_pct": 11.2},
        ),
    )
    rehydrated = BridgeResponseV1.model_validate_json(resp.model_dump_json())
    assert rehydrated.error is not None
    assert rehydrated.error.code == "roi_gate_failed"
    assert rehydrated.error.details["actual_roi_pct"] == 11.2
