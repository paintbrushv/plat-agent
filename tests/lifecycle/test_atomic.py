import json
from pathlib import Path

import pytest

from plat_agent.lifecycle.atomic import atomic_write_json, atomic_write_text


def test_atomic_write_text_creates_file(tmp_path: Path) -> None:
    target = tmp_path / "hello.txt"
    atomic_write_text(target, "hi")
    assert target.read_text() == "hi"


def test_atomic_write_text_replaces_existing(tmp_path: Path) -> None:
    target = tmp_path / "hello.txt"
    target.write_text("old")
    atomic_write_text(target, "new")
    assert target.read_text() == "new"


def test_atomic_write_text_no_tmp_left_behind(tmp_path: Path) -> None:
    target = tmp_path / "hello.txt"
    atomic_write_text(target, "hi")
    leftovers = list(tmp_path.glob("*.tmp*"))
    assert leftovers == []


def test_atomic_write_json_serializes(tmp_path: Path) -> None:
    target = tmp_path / "data.json"
    atomic_write_json(target, {"k": 1, "v": [2, 3]})
    assert json.loads(target.read_text()) == {"k": 1, "v": [2, 3]}


def test_atomic_write_creates_parent_dir(tmp_path: Path) -> None:
    target = tmp_path / "a" / "b" / "c.txt"
    atomic_write_text(target, "hi")
    assert target.read_text() == "hi"
