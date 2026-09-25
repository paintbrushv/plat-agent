import json
from pathlib import Path

import pytest

from plat_agent.lifecycle.punchlist import (
    PUNCHLIST_MD,
    PunchlistParseError,
    read_punchlist_json,
    reconcile_and_regenerate_punchlist,
    reconcile_punchlist,
    render_punchlist_markdown,
    write_punchlist_json,
    write_punchlist_markdown,
)
from plat_agent.lifecycle.state import BlockerItem


def test_write_and_read_punchlist_json_round_trip(tmp_path: Path) -> None:
    blockers = [
        BlockerItem(step="intake", id="missing_t12", description="No T12 found."),
        BlockerItem(step="comps", id="scraper_blocked", description="2 of 6 403'd."),
    ]
    write_punchlist_json(tmp_path, blockers)
    restored = read_punchlist_json(tmp_path)
    assert len(restored) == 2
    assert restored[0].id == "missing_t12"
    assert restored[1].id == "scraper_blocked"


def test_render_markdown_with_unchecked_items(tmp_path: Path) -> None:
    blockers = [
        BlockerItem(step="intake", id="missing_t12", description="x", cleared=False),
    ]
    md = render_punchlist_markdown("project_essex", "run_002", blockers)
    assert "# Punchlist for project_essex / run_002" in md
    assert "## intake" in md
    assert "- [ ] **missing_t12**" in md


def test_render_markdown_groups_by_step(tmp_path: Path) -> None:
    blockers = [
        BlockerItem(step="intake", id="a", description="x"),
        BlockerItem(step="comps", id="b", description="y"),
        BlockerItem(step="intake", id="c", description="z"),
    ]
    md = render_punchlist_markdown("d", "r", blockers)
    intake_section = md.split("## intake")[1].split("## comps")[0]
    assert "**a**" in intake_section
    assert "**c**" in intake_section
    assert "**b**" not in intake_section


def test_reconcile_user_checked_clears_blocker(tmp_path: Path) -> None:
    # JSON has unchecked; user edits markdown to check it; reconcile updates JSON
    blockers = [BlockerItem(step="intake", id="missing_t12", description="x", cleared=False)]
    write_punchlist_json(tmp_path, blockers)
    # Simulate user editing markdown
    edited_md = """# Punchlist for d / r

## intake
- [x] **missing_t12**: No T12 found.
"""
    (tmp_path / "punchlist.md").write_text(edited_md)
    reconciled = reconcile_punchlist(tmp_path)
    assert reconciled[0].cleared is True


def test_reconcile_user_removed_item_treats_as_cleared(tmp_path: Path) -> None:
    blockers = [BlockerItem(step="intake", id="missing_t12", description="x", cleared=False)]
    write_punchlist_json(tmp_path, blockers)
    (tmp_path / "punchlist.md").write_text("# Punchlist for d / r\n\n## intake\n(none)\n")
    reconciled = reconcile_punchlist(tmp_path)
    assert reconciled[0].cleared is True


def test_reconcile_unknown_id_logs_warning_and_treats_as_blocker(tmp_path: Path) -> None:
    # User adds a blocker line with unrecognized id
    write_punchlist_json(tmp_path, [])
    (tmp_path / "punchlist.md").write_text(
        "# Punchlist for d / r\n\n## intake\n- [ ] **unknown_blocker**: x\n"
    )
    reconciled = reconcile_punchlist(tmp_path)
    # Unknown id is preserved as a blocker so step reruns
    assert len(reconciled) == 1
    assert reconciled[0].id == "unknown_blocker"
    assert reconciled[0].cleared is False


def test_reconcile_truncated_markdown_raises(tmp_path: Path) -> None:
    # Truncated/unparseable markdown → PunchlistParseError per §4.2.1
    write_punchlist_json(tmp_path, [])
    # Half a heading line, no body — parse should error
    (tmp_path / "punchlist.md").write_text("# Punchli")
    with pytest.raises(PunchlistParseError):
        reconcile_punchlist(tmp_path)


def test_render_markdown_with_resolution_hint(tmp_path: Path) -> None:
    blockers = [
        BlockerItem(
            step="intake",
            id="missing_t12",
            description="Source T12 document not detected in raw_inputs/.",
            resolution_hint="drop broker's T12_2024.xlsx into raw_inputs/ and rerun.",
        ),
    ]
    md = render_punchlist_markdown("project_essex", "run_002", blockers)
    assert "- [ ] **missing_t12**: Source T12 document not detected in raw_inputs/." in md
    assert "      Resolution: drop broker's T12_2024.xlsx into raw_inputs/ and rerun." in md


def test_render_markdown_omits_resolution_when_absent(tmp_path: Path) -> None:
    blockers = [BlockerItem(step="intake", id="x", description="y")]
    md = render_punchlist_markdown("d", "r", blockers)
    assert "Resolution:" not in md


def test_reconcile_and_regenerate_writes_canonical_markdown(tmp_path: Path) -> None:
    # Analyst makes a malformed edit; reconcile_and_regenerate must rewrite
    # the markdown to canonical form.
    blockers = [
        BlockerItem(
            step="intake",
            id="missing_t12",
            description="x",
            resolution_hint="drop file in raw_inputs/",
        ),
    ]
    write_punchlist_json(tmp_path, blockers)
    write_punchlist_markdown(tmp_path, "d", "r", blockers)
    # Simulate analyst stomping on the markdown but checking the box
    (tmp_path / PUNCHLIST_MD).write_text(
        "# Punchlist for d / r\n\n## intake\n- [x] **missing_t12**: x\nstray garbage\n"
    )
    final = reconcile_and_regenerate_punchlist(tmp_path, "d", "r")
    assert len(final) == 1
    assert final[0].cleared is True
    # Markdown must be regenerated from JSON (resolution_hint preserved, garbage gone)
    rewritten = (tmp_path / PUNCHLIST_MD).read_text()
    assert "stray garbage" not in rewritten
    assert "Resolution: drop file in raw_inputs/" in rewritten


def test_reconcile_deleted_step_heading_treats_items_cleared(tmp_path: Path) -> None:
    # Step section absent from markdown → all items under it treated as cleared
    blockers = [
        BlockerItem(step="intake", id="missing_t12", description="x", cleared=False),
    ]
    write_punchlist_json(tmp_path, blockers)
    (tmp_path / "punchlist.md").write_text(
        "# Punchlist for d / r\n\n## comps\n(none)\n"
    )
    reconciled = reconcile_punchlist(tmp_path)
    assert reconciled[0].cleared is True
