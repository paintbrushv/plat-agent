"""Atomic write utilities — all lifecycle artifact writes must be atomic.

Pattern: write to <path>.tmp, then os.replace(<path>.tmp, <path>).
os.replace() is atomic on POSIX and Windows. Mid-write crash leaves
no .tmp at the final path (callers can detect partial writes).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def atomic_write_text(path: Path, content: str) -> None:
    """Write text to path atomically. Creates parent directories if missing."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content)
    os.replace(tmp, path)


def atomic_write_json(path: Path, data: Any, *, indent: int | None = 2) -> None:
    """Serialize data to JSON and write atomically."""
    atomic_write_text(path, json.dumps(data, indent=indent, sort_keys=False))
