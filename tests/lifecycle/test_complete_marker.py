import json
from pathlib import Path

from plat_agent.lifecycle.complete_marker import (
    CompleteMarker,
    read_complete_marker,
    write_complete_marker,
)


def test_write_complete_marker_creates_file(tmp_path: Path) -> None:
    step_dir = tmp_path / "intake"
    step_dir.mkdir()
    write_complete_marker(step_dir, step="intake",
                          file_manifest=["canonical_deal.json", "manifest.md"])
    assert (step_dir / "_complete").exists()


def test_complete_marker_payload_shape(tmp_path: Path) -> None:
    step_dir = tmp_path / "comps"
    step_dir.mkdir()
    write_complete_marker(step_dir, step="comps",
                          file_manifest=["comps.json", "_provenance.json"])
    payload = json.loads((step_dir / "_complete").read_text())
    assert payload["step"] == "comps"
    assert payload["file_manifest"] == ["comps.json", "_provenance.json"]
    assert "committed_at" in payload


def test_read_complete_marker_returns_typed_object(tmp_path: Path) -> None:
    step_dir = tmp_path / "judgment"
    step_dir.mkdir()
    write_complete_marker(step_dir, step="judgment",
                          file_manifest=["positioning.json"])
    marker = read_complete_marker(step_dir)
    assert isinstance(marker, CompleteMarker)
    assert marker.step == "judgment"
    assert marker.file_manifest == ["positioning.json"]


def test_read_complete_marker_returns_none_when_missing(tmp_path: Path) -> None:
    assert read_complete_marker(tmp_path / "nonexistent") is None


def test_read_complete_marker_returns_none_on_corrupt(tmp_path: Path) -> None:
    step_dir = tmp_path / "corrupt"
    step_dir.mkdir()
    (step_dir / "_complete").write_text("{not json")
    assert read_complete_marker(step_dir) is None
