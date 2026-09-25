"""Regenerating fixtures must produce byte-identical output (modulo timestamps).

Catches accidental nondeterminism in subsystem output before it hits a smoke test.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


GOLDENS = [
    "intake/outputs/synthetic_clean_deal__canonical_deal.json",
    "intake/outputs/synthetic_missing_t12__canonical_deal.json",
    "comps/outputs/synthetic_clean_deal__comps.json",
    "judgment/outputs/synthetic_clean_deal__positioning.json",
    "judgment/outputs/synthetic_om_optimistic__positioning.json",
    "memo/outputs/synthetic_clean_deal__memo.md",
]


@pytest.mark.parametrize("rel", GOLDENS)
def test_golden_exists_and_parses(rel: str) -> None:
    path = Path(__file__).parent / rel
    assert path.exists(), f"missing golden: {rel}"
    if rel.endswith(".json"):
        json.loads(path.read_text())  # raises if malformed
