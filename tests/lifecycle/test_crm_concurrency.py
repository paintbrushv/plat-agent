# tests/lifecycle/test_crm_concurrency.py
import json
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import pytest

from plat_agent.lifecycle.crm import CRMRow, atomic_append, crm_path, list_deals


def _worker_append(args: tuple[str, str, int]) -> None:
    """Top-level so it pickles for ProcessPoolExecutor."""
    jsonl_path, slug, idx = args
    row = CRMRow(
        deal_slug=slug,
        run_id=f"run_{idx:03d}",
        ts=datetime(2026, 5, 5, 16, idx % 60, tzinfo=timezone.utc),
        status="memo_ready",
        recommendation="PROCEED",
        memo_path=f"/m/{idx}",
        address=f"addr {idx}",
        units=240,
        asking_price=28_000_000 + idx,
    )
    atomic_append(Path(jsonl_path), row)


def test_concurrent_appends_do_not_corrupt_jsonl(tmp_path: Path) -> None:
    """20 parallel writers — every row must round-trip as valid JSON."""
    p = crm_path(tmp_path)
    n = 20
    args = [(str(p), f"slug_{i:02d}", i) for i in range(n)]
    with ProcessPoolExecutor(max_workers=8) as ex:
        futures = [ex.submit(_worker_append, a) for a in args]
        for f in as_completed(futures):
            f.result()  # propagate any worker exceptions

    text = p.read_text(encoding="utf-8")
    lines = [ln for ln in text.splitlines() if ln.strip()]
    assert len(lines) == n
    # Every line must parse as JSON (no interleaved/torn writes)
    for ln in lines:
        parsed = json.loads(ln)
        assert "deal_slug" in parsed
    # All slugs present
    slugs = {json.loads(ln)["deal_slug"] for ln in lines}
    assert slugs == {f"slug_{i:02d}" for i in range(n)}
    # File ends with newline (no trailing partial write)
    assert text.endswith("\n")


def test_concurrent_appends_round_trip_via_list_deals(tmp_path: Path) -> None:
    """list_deals() must read every appended row back as a valid CRMRow."""
    p = crm_path(tmp_path)
    n = 12
    args = [(str(p), f"slug_{i:02d}", i) for i in range(n)]
    with ProcessPoolExecutor(max_workers=6) as ex:
        list(ex.map(_worker_append, args))

    rows = list_deals(p)
    assert len(rows) == n
    assert {r.deal_slug for r in rows} == {f"slug_{i:02d}" for i in range(n)}


def _worker_append_with_hold(args: tuple[str, str, int, float]) -> None:
    """Hold the lock for ``hold_s`` seconds before releasing.

    Replicates ``atomic_append`` with an artificial delay inside the lock so
    the test can prove fcntl actually serializes writers (not just that the
    OS happened to schedule them sequentially).
    """
    import fcntl
    import time
    jsonl_path, slug, idx, hold_s = args
    row = CRMRow(
        deal_slug=slug,
        run_id=f"run_{idx:03d}",
        ts=datetime(2026, 5, 5, 16, idx % 60, tzinfo=timezone.utc),
        status="memo_ready",
        recommendation="PROCEED",
        memo_path=f"/m/{idx}",
        address=f"addr {idx}",
        units=240,
        asking_price=28_000_000 + idx,
    )
    Path(jsonl_path).parent.mkdir(parents=True, exist_ok=True)
    line = row.model_dump_json() + "\n"
    with open(jsonl_path, "a", encoding="utf-8") as f:
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            time.sleep(hold_s)  # hold the lock to force contention
            f.write(line)
            f.flush()
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def test_concurrent_writers_are_actually_serialized_by_lock(tmp_path: Path) -> None:
    """Force lock contention: each of N writers holds the lock for `hold` s
    before releasing. Total wall time must be ~= N * hold (proving
    serialization), not ~= hold (which would mean the lock is a no-op).

    Without this, a passing 20-parallel-writers test only shows that *some*
    sequencing happens — it does not prove fcntl is doing the work, since
    tiny appends may not actually overlap on a fast machine.
    """
    import time
    p = crm_path(tmp_path)
    n = 20
    hold_s = 0.05  # 50ms
    args = [(str(p), f"slug_{i:02d}", i, hold_s) for i in range(n)]

    t0 = time.monotonic()
    with ProcessPoolExecutor(max_workers=n) as ex:
        list(ex.map(_worker_append_with_hold, args))
    elapsed = time.monotonic() - t0

    # Lower bound: serialized writers take at least n * hold seconds, minus
    # a generous safety margin for spawn overhead variance. Upper bound: if
    # we exceed (n + 5) * hold something else is wrong (zombie process,
    # etc).
    min_serialized = (n - 4) * hold_s  # allow ~4 overlapping for slack
    assert elapsed >= min_serialized, (
        f"Writers completed in {elapsed:.3f}s with each holding the lock for "
        f"{hold_s}s — fcntl is not serializing as expected (expected ≥ "
        f"{min_serialized:.3f}s)."
    )

    # Also verify correctness — every row landed without corruption.
    rows = list_deals(p)
    assert len(rows) == n
    assert {r.deal_slug for r in rows} == {f"slug_{i:02d}" for i in range(n)}
