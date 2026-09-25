"""Tests for canonical-dict entry points: run_scenarios_from_canonical and
find_break_even_from_canonical.

These wrappers let federation/orchestrator code drop a canonical schema v0.1
dict straight into the sweep layer without the legacy DealInputs assembly.
"""

import copy
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from plat_agent.sweep import (
    find_break_even_from_canonical,
    run_scenarios_from_canonical,
)


SAMPLE_CANONICAL = Path(
    "/path/to/projects/multifamily-underwriting/runs/deals/"
    "demo_on_first_street/outputs/run_001/canonical_inputs.json"
)


@pytest.fixture
def first_street_canonical() -> dict:
    """Load the first_street run_001 canonical dict, deep-copied per test."""
    if not SAMPLE_CANONICAL.exists():
        pytest.skip(f"Fixture not present: {SAMPLE_CANONICAL}")
    with SAMPLE_CANONICAL.open() as f:
        return json.load(f)


def _metrics_feasible(levered_irr: float = 0.15, min_dscr: float = 1.30) -> dict:
    return {
        "status": "success",
        "irr": {"levered_irr": levered_irr},
        "equity_multiple": {"levered_em": 1.85},
        "dscr": {"minimum": min_dscr, "average": min_dscr + 0.2},
        "yields": {"going_in_cap_rate": 0.055},
    }


def _mock_backend(results_per_call: list[dict]) -> MagicMock:
    backend = MagicMock()
    backend.run_deal.side_effect = iter(results_per_call)
    return backend


# ---------------------------------------------------------------------------
# run_scenarios_from_canonical
# ---------------------------------------------------------------------------

def test_run_scenarios_from_canonical_first_street(first_street_canonical):
    backend = _mock_backend([
        _metrics_feasible(0.16, 1.35),  # Base
        _metrics_feasible(0.19, 1.45),  # Bull
        _metrics_feasible(0.10, 1.10),  # Bear
    ])
    result = run_scenarios_from_canonical(
        first_street_canonical, preset="value_add", backend=backend,
    )
    assert result.status == "success"
    assert result.property_id == "First Street on Mockingbird"
    assert len(result.scenarios) == 3
    assert [s.name for s in result.scenarios] == ["Base", "Bull", "Bear"]
    assert backend.run_deal.call_count == 3


def test_run_scenarios_from_canonical_rejects_missing_metadata(first_street_canonical):
    bad = copy.deepcopy(first_street_canonical)
    bad["metadata"] = {}  # strip deal_id
    with pytest.raises(ValueError, match="deal_id"):
        run_scenarios_from_canonical(bad, preset="value_add", backend=MagicMock())


def test_run_scenarios_from_canonical_rejects_missing_unit_cohorts(first_street_canonical):
    bad = copy.deepcopy(first_street_canonical)
    bad["unit_cohorts"] = []
    with pytest.raises(ValueError, match="unit_cohorts"):
        run_scenarios_from_canonical(bad, preset="value_add", backend=MagicMock())


# ---------------------------------------------------------------------------
# find_break_even_from_canonical
# ---------------------------------------------------------------------------

def test_find_break_even_from_canonical_first_street(first_street_canonical):
    """Bisect levered_irr across exit_cap. IRR(ec) = 0.20 - 4*(ec - 0.05)
    crosses 0.12 at ec=0.07 — bracketed by (0.05, 0.08).
    """
    def run_deal(deal):
        ec = deal["exit_assumptions"]["exit_cap_rate"]
        irr = 0.20 - 4 * (ec - 0.05)
        return _metrics_feasible(levered_irr=irr, min_dscr=1.40)

    backend = MagicMock()
    backend.run_deal.side_effect = lambda deal: run_deal(deal)

    result = find_break_even_from_canonical(
        first_street_canonical,
        axis="exit_cap",
        bounds=(0.05, 0.08),
        objective="levered_irr",
        irr_threshold=0.12,
        tolerance=0.0005,
        max_iterations=20,
        backend=backend,
    )
    assert result.status == "success"
    assert result.property_id == "First Street on Mockingbird"
    assert result.axis == "exit_cap"
    assert result.irr_breakpoint is not None
    assert abs(result.irr_breakpoint - 0.07) < 0.001
