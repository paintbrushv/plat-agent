# tests/lifecycle/test_crm_atomic_append.py
import json
from datetime import datetime, timezone
from pathlib import Path

from plat_agent.lifecycle.crm import CRMRow, atomic_append, crm_path


def _row(slug: str = "d", run_id: str = "r") -> CRMRow:
    return CRMRow(
        deal_slug=slug, run_id=run_id,
        ts=datetime(2026, 5, 5, 16, 30, tzinfo=timezone.utc),
        status="memo_ready",
        recommendation="PROCEED",
        memo_path="/m", address="x", units=240, asking_price=28_000_000,
    )


def test_atomic_append_creates_file(tmp_path: Path) -> None:
    p = crm_path(tmp_path)
    atomic_append(p, _row())
    assert p.exists()


def test_atomic_append_creates_parent_dir(tmp_path: Path) -> None:
    runs_root = tmp_path / "runs" / "deals"
    p = crm_path(runs_root)
    atomic_append(p, _row())
    assert p.exists()


def test_atomic_append_writes_one_jsonl_line(tmp_path: Path) -> None:
    p = crm_path(tmp_path)
    atomic_append(p, _row())
    text = p.read_text()
    lines = [ln for ln in text.splitlines() if ln.strip()]
    assert len(lines) == 1
    parsed = json.loads(lines[0])
    assert parsed["deal_slug"] == "d"


def test_atomic_append_preserves_existing_rows(tmp_path: Path) -> None:
    p = crm_path(tmp_path)
    atomic_append(p, _row("a", "r1"))
    atomic_append(p, _row("b", "r2"))
    atomic_append(p, _row("c", "r3"))
    lines = [ln for ln in p.read_text().splitlines() if ln.strip()]
    assert len(lines) == 3
    slugs = [json.loads(ln)["deal_slug"] for ln in lines]
    assert slugs == ["a", "b", "c"]


def test_atomic_append_terminates_each_line_with_newline(tmp_path: Path) -> None:
    """Standard JSONL: every record ends with \\n so concatenation is safe."""
    p = crm_path(tmp_path)
    atomic_append(p, _row("a"))
    atomic_append(p, _row("b"))
    text = p.read_text()
    # Both rows end in newline (no trailing partial line)
    assert text.endswith("\n")
    assert text.count("\n") == 2
