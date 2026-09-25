"""§5.3 #5 -- two concurrent runs on different slugs do not corrupt _crm.jsonl."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from plat_agent.lifecycle.runner import run_lifecycle


def test_lifecycle_concurrent_different_deals_dont_corrupt_crm(
    tmp_path: Path,
    make_default_steps,
    project_root: Path,
) -> None:
    # Build two distinct data rooms (clean + om_optimistic -> different slugs)
    from tests.fixtures.lifecycle.builders import (
        write_clean_deal_room,
        write_om_optimistic_deal_room,
    )
    room_a = tmp_path / "alpha"
    room_b = tmp_path / "bravo"
    write_clean_deal_room(room_a)
    write_om_optimistic_deal_room(room_b)

    # Each run gets its own freshly-built step dict (the factories close
    # over project_root for the CRM step). They share the same project_root
    # so their CRM rows land in the same _crm.jsonl.
    def go(room: Path, slug: str):
        return run_lifecycle(
            room,
            deal_slug=slug,
            project_root=project_root,
            steps=make_default_steps(),
        )

    with ThreadPoolExecutor(max_workers=2) as ex:
        fa = ex.submit(go, room_a, "deal_alpha")
        fb = ex.submit(go, room_b, "deal_bravo")
        ra, rb = fa.result(), fb.result()

    assert ra.status in {"memo_ready", "memo_ready_with_blockers"}
    assert rb.status in {"memo_ready", "memo_ready_with_blockers"}

    # CRM has both rows AND every line parses cleanly
    lines = (project_root / "runs" / "deals" / "_crm.jsonl").read_text().splitlines()
    rows = [json.loads(line) for line in lines if line.strip()]
    slugs = {r["deal_slug"] for r in rows}
    assert slugs == {"deal_alpha", "deal_bravo"}
