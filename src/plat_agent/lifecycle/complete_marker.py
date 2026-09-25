"""Step commit markers (spec §4.4.1).

Each step writes _complete as its LAST action. is_satisfied() requires
the marker to exist AND every file in its manifest to exist on disk.
A mid-step crash leaves no _complete → step is not satisfied → rerun.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from plat_agent.lifecycle.atomic import atomic_write_json
from plat_agent.lifecycle.state import StepName


class CompleteMarker(BaseModel):
    """Contents of <step_dir>/_complete."""

    step: StepName
    committed_at: datetime
    file_manifest: list[str]


def write_complete_marker(
    step_dir: Path,
    *,
    step: StepName,
    file_manifest: list[str],
) -> None:
    """Write `_complete` to step_dir as the LAST action of a step."""
    marker = CompleteMarker(
        step=step,
        committed_at=datetime.now(timezone.utc),
        file_manifest=list(file_manifest),
    )
    atomic_write_json(step_dir / "_complete", marker.model_dump(mode="json"))


def read_complete_marker(step_dir: Path) -> CompleteMarker | None:
    """Read and parse `_complete`. Returns None if missing or corrupt.

    Corrupt = unreadable JSON or schema mismatch. Caller treats as 'no marker'
    so cache-check correctly forces a rerun.
    """
    path = step_dir / "_complete"
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text())
        return CompleteMarker.model_validate(payload)
    except (json.JSONDecodeError, ValueError):
        return None
