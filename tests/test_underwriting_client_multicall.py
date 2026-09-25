"""Regression test: UnderwritingClient must sustain N sequential run_summary calls.

Live MCP test — requires the underwriting engine venv + server.
Gate with RUN_LIVE_MCP_TESTS=1 so CI and default `pytest tests/` skip it.
"""

import json
import os
from pathlib import Path

import pytest

from plat_agent.models import DealInputs
from plat_agent.underwriting_client import UnderwritingClient

pytestmark = pytest.mark.skipif(
    not os.environ.get("RUN_LIVE_MCP_TESTS"),
    reason="Live MCP test — set RUN_LIVE_MCP_TESTS=1 to run",
)


FIXTURE = Path(__file__).parent / "fixtures" / "sample_hills_deal.json"


def _sample_hills_merged_deal() -> dict:
    """Load the Sample Hills deal and return its base_deal_inputs (already
    has renovation_programs merged in the fixture)."""
    raw = json.loads(FIXTURE.read_text())
    inputs = DealInputs(**raw)
    assert inputs.base_deal_inputs is not None, "fixture must have base_deal_inputs"
    return inputs.base_deal_inputs


def test_three_sequential_run_summary_calls_all_succeed():
    """Sweep calls run_summary N times in quick succession — all must succeed."""
    client = UnderwritingClient()
    deal = _sample_hills_merged_deal()

    results = [client.run_summary(deal) for _ in range(3)]

    for i, r in enumerate(results):
        assert r.get("status") == "success", (
            f"call {i} failed with status={r.get('status')!r}: "
            f"error={r.get('error')!r}"
        )
