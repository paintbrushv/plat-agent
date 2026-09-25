# tests/lifecycle/test_crm_path.py
from pathlib import Path

from plat_agent.lifecycle.crm import CRM_FILENAME, crm_path


def test_crm_filename_constant() -> None:
    assert CRM_FILENAME == "_crm.jsonl"


def test_crm_path_under_runs_deals(tmp_path: Path) -> None:
    runs_root = tmp_path / "runs" / "deals"
    runs_root.mkdir(parents=True)
    p = crm_path(runs_root)
    assert p == runs_root / "_crm.jsonl"


def test_crm_path_does_not_create_file(tmp_path: Path) -> None:
    runs_root = tmp_path / "runs" / "deals"
    runs_root.mkdir(parents=True)
    p = crm_path(runs_root)
    assert not p.exists()  # resolver must be pure; only writers create the file


def test_crm_path_does_not_require_existing_root(tmp_path: Path) -> None:
    # Resolver returns a Path even when the parent doesn't exist yet; first
    # writer is responsible for creating dirs.
    runs_root = tmp_path / "nope" / "deals"
    p = crm_path(runs_root)
    assert p.name == "_crm.jsonl"
    assert p.parent == runs_root
