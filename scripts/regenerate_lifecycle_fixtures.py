"""Regenerate per-subsystem expected outputs from data-room fixtures.

Run once after schema/fixture changes:
    .venv/bin/python scripts/regenerate_lifecycle_fixtures.py

NOTE on intake/comps regen: in V1 the intake + comp-finder agents live in
the multifamily-underwriting sibling repo and require headless `claude -p`
dispatch. Until those agents are reachable from a hermetic test env,
the intake / comps / memo goldens are hand-curated contract-shaped JSON
(see tests/fixtures/lifecycle/{intake,comps,judgment,memo}/outputs/).

This script is the placeholder hook: when the agents become locally
runnable, point each `# TODO regen via ...` comment at the real entrypoint.
"""

from __future__ import annotations

import json
from pathlib import Path

from tests.fixtures.lifecycle.builders import (
    write_clean_deal_room,
    write_missing_t12_deal_room,
    write_om_optimistic_deal_room,
)


FIXTURE_ROOT = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "lifecycle"
SCENARIOS = {
    "synthetic_clean_deal": write_clean_deal_room,
    "synthetic_missing_t12": write_missing_t12_deal_room,
    "synthetic_om_optimistic": write_om_optimistic_deal_room,
}


def main() -> None:
    """Regenerate the data-room source files from builders.

    Subsystem goldens (canonical_deal, comps, positioning, memo) are
    contract-shaped fixtures hand-maintained until the sibling agents
    become locally runnable. See module docstring.
    """
    for slug, builder in SCENARIOS.items():
        scratch = FIXTURE_ROOT / "data_rooms" / slug
        builder(scratch)
        print(f"regenerated data room: {slug}")

    # Verify the goldens still parse (cheap drift check)
    goldens = [
        "intake/outputs/synthetic_clean_deal__canonical_deal.json",
        "intake/outputs/synthetic_missing_t12__canonical_deal.json",
        "comps/outputs/synthetic_clean_deal__comps.json",
        "judgment/outputs/synthetic_clean_deal__positioning.json",
        "judgment/outputs/synthetic_om_optimistic__positioning.json",
    ]
    for rel in goldens:
        path = FIXTURE_ROOT / rel
        assert path.exists(), f"missing golden: {rel}"
        json.loads(path.read_text())
        print(f"verified golden: {rel}")
    memo = FIXTURE_ROOT / "memo/outputs/synthetic_clean_deal__memo.md"
    assert memo.exists(), "missing memo golden"
    print(f"verified golden: {memo.relative_to(FIXTURE_ROOT)}")


if __name__ == "__main__":
    main()
