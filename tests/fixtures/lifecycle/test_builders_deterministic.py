"""Builder determinism tests.

Same inputs MUST produce byte-identical outputs across runs/machines.
Hash-based comparison keeps failure messages useful when bytes diverge.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from tests.fixtures.lifecycle.builders import (
    write_clean_deal_room,
    write_missing_t12_deal_room,
    write_om_optimistic_deal_room,
)


def test_clean_deal_room_byte_identical_across_runs(tmp_path: Path) -> None:
    a = tmp_path / "a"
    b = tmp_path / "b"
    write_clean_deal_room(a)
    write_clean_deal_room(b)
    for fname in ["OM.txt", "rent_roll.csv", "T12.csv"]:
        ah = hashlib.sha256((a / fname).read_bytes()).hexdigest()
        bh = hashlib.sha256((b / fname).read_bytes()).hexdigest()
        assert ah == bh, f"{fname} not deterministic: {ah} != {bh}"


def test_missing_t12_room_byte_identical_across_runs(tmp_path: Path) -> None:
    """Build twice; hash every emitted byte; assert match.

    Hash-based comparison (not just `==`) keeps the failure message useful
    when bytes diverge -- the test prints the differing hashes so a reviewer
    can grep by digest. It also matches the determinism contract used by
    other goldens-stable tests in this plan.
    """
    a = tmp_path / "a"
    b = tmp_path / "b"
    write_missing_t12_deal_room(a)
    write_missing_t12_deal_room(b)
    assert (a / "T12.csv").exists() is False
    assert (b / "T12.csv").exists() is False
    for fname in ["OM.txt", "rent_roll.csv"]:
        ah = hashlib.sha256((a / fname).read_bytes()).hexdigest()
        bh = hashlib.sha256((b / fname).read_bytes()).hexdigest()
        assert ah == bh, f"{fname} not deterministic: {ah} != {bh}"


def test_om_optimistic_room_byte_identical_across_runs(tmp_path: Path) -> None:
    a = tmp_path / "a"
    b = tmp_path / "b"
    write_om_optimistic_deal_room(a)
    write_om_optimistic_deal_room(b)
    for fname in ["OM.txt", "rent_roll.csv", "T12.csv"]:
        assert (a / fname).read_bytes() == (b / fname).read_bytes()
    # Sanity: this OM mentions 5.0% growth (the broker overstatement)
    assert "5.0%" in (a / "OM.txt").read_text()
