from pathlib import Path

import pytest

from plat_agent.lifecycle.runner import snapshot_raw_inputs


def _make_data_room(tmp_path: Path) -> Path:
    src = tmp_path / "data_room"
    src.mkdir()
    (src / "OM.pdf").write_bytes(b"OM-bytes")
    (src / "T12.xlsx").write_bytes(b"T12-bytes")
    (src / "subdir").mkdir()
    (src / "subdir" / "rent_roll.xlsx").write_bytes(b"RR-bytes")
    return src


def test_snapshot_creates_run_scoped_dir(tmp_path: Path) -> None:
    src = _make_data_room(tmp_path)
    run_dir = tmp_path / "outputs" / "run_001"
    snapshot_raw_inputs(src, run_dir)
    assert (run_dir / "raw_inputs" / "OM.pdf").exists()
    assert (run_dir / "raw_inputs" / "T12.xlsx").exists()
    assert (run_dir / "raw_inputs" / "subdir" / "rent_roll.xlsx").exists()


def test_snapshot_preserves_file_contents(tmp_path: Path) -> None:
    src = _make_data_room(tmp_path)
    run_dir = tmp_path / "outputs" / "run_001"
    snapshot_raw_inputs(src, run_dir)
    assert (run_dir / "raw_inputs" / "OM.pdf").read_bytes() == b"OM-bytes"
    assert (run_dir / "raw_inputs" / "subdir" / "rent_roll.xlsx").read_bytes() == b"RR-bytes"


def test_snapshot_skips_existing_files(tmp_path: Path) -> None:
    """Idempotent: re-snapshot does not error when raw_inputs already populated."""
    src = _make_data_room(tmp_path)
    run_dir = tmp_path / "outputs" / "run_001"
    snapshot_raw_inputs(src, run_dir)
    # Re-run — must not raise
    snapshot_raw_inputs(src, run_dir)
    assert (run_dir / "raw_inputs" / "OM.pdf").exists()


def test_snapshot_from_deal_root_excludes_generated_outputs(tmp_path: Path) -> None:
    deal_root = tmp_path / "the_dylan"
    raw_inputs = deal_root / "raw_inputs"
    raw_inputs.mkdir(parents=True)
    (raw_inputs / "OM.pdf").write_bytes(b"OM")
    prior_run_raw = deal_root / "outputs" / "run_001" / "raw_inputs"
    prior_run_raw.mkdir(parents=True)
    (prior_run_raw / "stale.pdf").write_bytes(b"stale")

    run_dir = deal_root / "outputs" / "run_002"
    snapshot_raw_inputs(deal_root, run_dir)

    assert (run_dir / "raw_inputs" / "raw_inputs" / "OM.pdf").exists()
    assert not (run_dir / "raw_inputs" / "outputs" / "run_001" / "raw_inputs" / "stale.pdf").exists()


def test_snapshot_raises_when_source_missing(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        snapshot_raw_inputs(tmp_path / "nonexistent", tmp_path / "out")


def test_snapshot_raises_when_source_is_file(tmp_path: Path) -> None:
    src_file = tmp_path / "single.pdf"
    src_file.write_bytes(b"x")
    with pytest.raises(NotADirectoryError):
        snapshot_raw_inputs(src_file, tmp_path / "out")
