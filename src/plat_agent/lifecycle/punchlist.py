"""Punchlist contract (spec §4.2 + §4.2.1).

Two artifacts:
  - punchlist.json: machine source of truth, keyed by stable id.
  - punchlist.md: presentation regenerated from JSON; analysts edit checkboxes.

reconcile_punchlist() reads markdown after analyst edits, applies the parser
robustness rules in §4.2.1 (unknown id, deleted heading, truncated file),
and returns the reconciled BlockerItem list.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Iterable

from plat_agent.lifecycle.atomic import atomic_write_json, atomic_write_text
from plat_agent.lifecycle.state import BlockerItem, StepName


PUNCHLIST_JSON = "punchlist.json"
PUNCHLIST_MD = "punchlist.md"

# Regex: `- [ ] **id**: description` or `- [x] **id**: description`
_BLOCKER_LINE = re.compile(r"^- \[(?P<check>[ x])\] \*\*(?P<id>[^*]+)\*\*:\s*(?P<desc>.+)$")
_STEP_HEADING = re.compile(r"^## (?P<step>\w+)$")


class PunchlistParseError(Exception):
    """Raised when punchlist.md is truncated or unparseable per §4.2.1."""


def write_punchlist_json(run_dir: Path, blockers: Iterable[BlockerItem]) -> None:
    """Write punchlist.json (the machine source of truth).

    Serialization choice: ``{"blockers": [...]}`` (a list under a top-level
    key) rather than an id-keyed dict. The list form preserves *insertion
    order*, which matters because analyst review of the rendered markdown
    follows the same order — so the on-disk JSON, the rendered markdown, and
    the diff seen during reconciliation are all aligned. An id-keyed dict
    would lose this ordering guarantee for free-form analyst edits.
    """
    payload = {"blockers": [b.model_dump(mode="json") for b in blockers]}
    atomic_write_json(run_dir / PUNCHLIST_JSON, payload)


def read_punchlist_json(run_dir: Path) -> list[BlockerItem]:
    """Read punchlist.json. Returns empty list if missing."""
    path = run_dir / PUNCHLIST_JSON
    if not path.exists():
        return []
    payload = json.loads(path.read_text())
    return [BlockerItem.model_validate(b) for b in payload.get("blockers", [])]


def render_punchlist_markdown(deal_slug: str, run_id: str, blockers: list[BlockerItem]) -> str:
    """Render markdown from blockers list. Always grouped by step.

    Per spec §4.2, when a blocker has a ``resolution_hint`` it is rendered as
    an indented continuation line beneath the bullet, e.g.::

        - [ ] **missing_t12**: No T12 found.
              Resolution: drop broker's T12_2024.xlsx into raw_inputs/ and rerun.
    """
    lines = [f"# Punchlist for {deal_slug} / {run_id}", ""]
    by_step: dict[str, list[BlockerItem]] = {}
    for b in blockers:
        by_step.setdefault(b.step, []).append(b)
    for step in ["intake", "comps", "judgment", "underwriting", "memo", "crm"]:
        items = by_step.get(step, [])
        lines.append(f"## {step}")
        if not items:
            lines.append("(none)")
        else:
            for b in items:
                check = "x" if b.cleared else " "
                lines.append(f"- [{check}] **{b.id}**: {b.description}")
                if b.field:
                    lines.append(f"      Field: `{b.field}`")
                if b.unit:
                    lines.append(f"      Unit: {b.unit}")
                if b.evidence_locations:
                    lines.append("      Evidence:")
                    lines.extend(
                        f"        - `{location}`"
                        for location in b.evidence_locations
                    )
                if b.submitted_value is not None:
                    lines.append(f"      Submitted value: `{b.submitted_value}`")
                if b.resolution_hint:
                    lines.append(f"      Resolution: {b.resolution_hint}")
        lines.append("")
    return "\n".join(lines)


def write_punchlist_markdown(run_dir: Path, deal_slug: str, run_id: str,
                             blockers: list[BlockerItem]) -> None:
    """Render and atomically write punchlist.md."""
    md = render_punchlist_markdown(deal_slug, run_id, blockers)
    atomic_write_text(run_dir / PUNCHLIST_MD, md)


def reconcile_punchlist(run_dir: Path) -> list[BlockerItem]:
    """Reconcile analyst markdown edits into the JSON source of truth.

    Robustness rules per §4.2.1:
      - Deleted step heading → all JSON items under that step treated as cleared.
      - Renamed/unknown id → preserved as a blocker (forces step rerun).
      - Duplicate id → first wins; subsequent ignored.
      - Free-text or checkbox-less lines → ignored.
      - Truncated/unparseable file → raise PunchlistParseError.

    Returns the reconciled blocker list and writes back both punchlist.json
    and punchlist.md (the markdown is regenerated from JSON to fix any
    malformed analyst edits).
    """
    md_path = run_dir / PUNCHLIST_MD
    json_blockers = read_punchlist_json(run_dir)
    json_by_id = {b.id: b for b in json_blockers}

    if not md_path.exists():
        # No analyst edits to reconcile; return JSON state as-is
        return json_blockers

    md_text = md_path.read_text()

    # Truncation check: must contain at least the title line and one blank line
    if not md_text.startswith("# Punchlist for"):
        raise PunchlistParseError(
            f"punchlist.md missing or malformed title line: {md_path}"
        )

    # Parse into per-step buckets
    md_steps: dict[str, list[tuple[str, bool, str]]] = {}  # step -> [(id, cleared, desc), ...]
    current_step: str | None = None
    for line in md_text.splitlines():
        line = line.rstrip()
        m_heading = _STEP_HEADING.match(line)
        if m_heading:
            current_step = m_heading.group("step")
            md_steps.setdefault(current_step, [])
            continue
        if current_step is None:
            continue  # title or blank lines before first heading
        m_blocker = _BLOCKER_LINE.match(line)
        if m_blocker:
            md_steps[current_step].append((
                m_blocker.group("id"),
                m_blocker.group("check") == "x",
                m_blocker.group("desc"),
            ))
        # else: free text / "(none)" / empty — ignored

    # Apply rules
    reconciled: dict[str, BlockerItem] = {}
    seen_ids: set[str] = set()

    # 1. For each step section in markdown: process its items
    for step, items in md_steps.items():
        for blocker_id, cleared, desc in items:
            if blocker_id in seen_ids:
                continue  # duplicate id — first wins
            seen_ids.add(blocker_id)
            existing = json_by_id.get(blocker_id)
            if existing is not None:
                # Known id — apply checkbox state from markdown
                reconciled[blocker_id] = existing.model_copy(update={"cleared": cleared})
            else:
                # Unknown id — preserve as new uncleared blocker (forces rerun)
                reconciled[blocker_id] = BlockerItem(
                    step=step,  # type: ignore[arg-type]
                    id=blocker_id,
                    description=desc,
                    cleared=cleared,
                )

    # 2. JSON items under steps absent from markdown: treat as cleared
    for b in json_blockers:
        if b.id in seen_ids:
            continue
        if b.step not in md_steps:
            # step heading was deleted — clear it
            reconciled[b.id] = b.model_copy(update={"cleared": True})
        else:
            # step exists but item absent — also clear (analyst removed line)
            reconciled[b.id] = b.model_copy(update={"cleared": True})

    final = list(reconciled.values())

    # Write back: JSON authoritative; markdown regeneration is the caller's
    # responsibility (it needs deal_slug/run_id). Use
    # ``reconcile_and_regenerate_punchlist`` for the combined operation.
    write_punchlist_json(run_dir, final)

    return final


def reconcile_and_regenerate_punchlist(
    run_dir: Path,
    deal_slug: str,
    run_id: str,
) -> list[BlockerItem]:
    """Reconcile + regenerate markdown so malformed analyst edits don't persist.

    Calls :func:`reconcile_punchlist` to update ``punchlist.json``, then
    :func:`write_punchlist_markdown` to overwrite ``punchlist.md`` from the
    canonical JSON. This is the recommended entry point for orchestrators —
    after reconciliation, the markdown file is guaranteed to be in canonical
    shape (correct headings, formatting, resolution hints) regardless of how
    the analyst edited it.
    """
    final = reconcile_punchlist(run_dir)
    write_punchlist_markdown(run_dir, deal_slug, run_id, final)
    return final
