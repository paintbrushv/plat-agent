"""is_satisfied() cache check (spec §4.4) + provenance helpers.

The full cache-validity check requires ALL of:
  1. step name in state.steps_completed
  2. <step_dir>/_complete marker exists, AND every file in its manifest exists
  3. no uncleared blockers under this step in punchlist.json
  4. _provenance.json.input_hash matches the freshly-computed input hash
  5. _provenance.json.contract_version matches CONTRACT_VERSION
  6. TTL not exceeded (e.g., comps.json.as_of within 7 days for comps step)

If ANY check fails, the step is rerun. Conservative by design — stale
cache = stale recommendation.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from plat_agent.lifecycle.atomic import atomic_write_json
from plat_agent.lifecycle.complete_marker import read_complete_marker
from plat_agent.lifecycle.defaults import CONTRACT_VERSION, DEFAULT_TTL_DAYS
from plat_agent.lifecycle.punchlist import read_punchlist_json
from plat_agent.lifecycle.state import LifecycleState, StepName


PROVENANCE_FILE = "_provenance.json"


def compute_input_hash(input_paths: list[Path]) -> str:
    """sha256 over the contents of all input files, in order. Stable."""
    h = hashlib.sha256()
    for p in input_paths:
        if p.exists() and p.is_file():
            h.update(p.read_bytes())
        h.update(b"|")  # delimiter even for missing files (so absence vs presence differs)
    return h.hexdigest()


def write_provenance(
    step_dir: Path,
    *,
    input_hash: str,
    contract_version: str = CONTRACT_VERSION,
    status: str = "ok",
    extra: dict[str, Any] | None = None,
) -> None:
    """Write `_provenance.json` for a step. Used by every LifecycleStep."""
    payload: dict[str, Any] = {
        "status": status,
        "input_hash": input_hash,
        "contract_version": contract_version,
        "written_at": datetime.now(timezone.utc).isoformat(),
    }
    if extra:
        payload.update(extra)
    atomic_write_json(step_dir / PROVENANCE_FILE, payload)


def _read_provenance(step_dir: Path) -> dict[str, Any] | None:
    path = step_dir / PROVENANCE_FILE
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return None


def _check_ttl(step: StepName, step_dir: Path) -> bool:
    """Return True if the step's output is within its TTL (or has no TTL)."""
    ttl_days = DEFAULT_TTL_DAYS.get(step)
    if ttl_days is None:
        return True
    if step == "comps":
        # comps.json.as_of is the source of truth for comp data freshness
        comps_json = step_dir / "comps.json"
        if not comps_json.exists():
            return False
        try:
            payload = json.loads(comps_json.read_text())
            as_of_str = payload.get("as_of")
            if not as_of_str:
                return False
            as_of = datetime.fromisoformat(as_of_str).replace(tzinfo=timezone.utc) \
                if "T" not in as_of_str else datetime.fromisoformat(as_of_str)
            if as_of.tzinfo is None:
                as_of = as_of.replace(tzinfo=timezone.utc)
            age = datetime.now(timezone.utc) - as_of
            return age <= timedelta(days=ttl_days)
        except (json.JSONDecodeError, ValueError):
            return False
    # Other steps with a TTL: use _provenance.written_at
    prov = _read_provenance(step_dir)
    if prov is None:
        return False
    written_at = datetime.fromisoformat(prov["written_at"])
    if written_at.tzinfo is None:
        written_at = written_at.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - written_at) <= timedelta(days=ttl_days)


def is_satisfied(
    state: LifecycleState,
    run_dir: Path,
    *,
    step: StepName,
    current_input_hash: str,
) -> bool:
    """Full cache check per spec §4.4. True iff ALL conditions hold."""
    # 1. Step in steps_completed
    if step not in state.steps_completed:
        return False

    step_dir = run_dir / step

    # 2. _complete marker + manifest files present
    marker = read_complete_marker(step_dir)
    if marker is None:
        return False
    # Defensive: a marker whose `step` field doesn't match the directory's
    # step name indicates a copy/paste accident or directory rename. Treat
    # as cache miss so the step is rerun rather than trusting stale data.
    if marker.step != step:
        return False
    for fname in marker.file_manifest:
        if not (step_dir / fname).exists():
            return False

    # 3. No uncleared blockers for this step
    blockers = read_punchlist_json(run_dir)
    if any(b.step == step and not b.cleared for b in blockers):
        return False

    # 4 + 5. provenance: input_hash + contract_version match
    prov = _read_provenance(step_dir)
    if prov is None:
        return False
    if prov.get("input_hash") != current_input_hash:
        return False
    if prov.get("contract_version") != CONTRACT_VERSION:
        return False

    # 6. TTL
    if not _check_ttl(step, step_dir):
        return False

    return True
