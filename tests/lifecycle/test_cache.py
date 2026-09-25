import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from plat_agent.lifecycle.cache import (
    compute_input_hash,
    is_satisfied,
    write_provenance,
)
from plat_agent.lifecycle.complete_marker import write_complete_marker
from plat_agent.lifecycle.defaults import CONTRACT_VERSION
from plat_agent.lifecycle.punchlist import write_punchlist_json
from plat_agent.lifecycle.state import LifecycleState


def _make_run(tmp_path: Path) -> Path:
    """Build a minimal run directory with intake+comps step dirs."""
    intake = tmp_path / "intake"
    intake.mkdir()
    (intake / "canonical_deal.json").write_text("{}")
    write_complete_marker(intake, step="intake", file_manifest=["canonical_deal.json"])
    return tmp_path


def test_compute_input_hash_stable(tmp_path: Path) -> None:
    a = tmp_path / "a.json"
    a.write_text('{"x": 1}')
    h1 = compute_input_hash([a])
    h2 = compute_input_hash([a])
    assert h1 == h2
    assert len(h1) == 64  # sha256


def test_compute_input_hash_changes_when_file_changes(tmp_path: Path) -> None:
    a = tmp_path / "a.json"
    a.write_text('{"x": 1}')
    h1 = compute_input_hash([a])
    a.write_text('{"x": 2}')
    h2 = compute_input_hash([a])
    assert h1 != h2


def test_is_satisfied_passes_clean_step(tmp_path: Path) -> None:
    run_dir = _make_run(tmp_path)
    state = LifecycleState(deal_slug="d", run_id="r", steps_completed=["intake"])
    write_provenance(run_dir / "intake", input_hash="h", contract_version=CONTRACT_VERSION,
                     extra={"as_of": datetime.now(timezone.utc).isoformat()})
    write_punchlist_json(run_dir, [])
    assert is_satisfied(state, run_dir, step="intake", current_input_hash="h") is True


def test_is_satisfied_fails_when_step_not_completed(tmp_path: Path) -> None:
    run_dir = _make_run(tmp_path)
    state = LifecycleState(deal_slug="d", run_id="r", steps_completed=[])
    write_provenance(run_dir / "intake", input_hash="h", contract_version=CONTRACT_VERSION)
    write_punchlist_json(run_dir, [])
    assert is_satisfied(state, run_dir, step="intake", current_input_hash="h") is False


def test_is_satisfied_fails_when_complete_marker_missing(tmp_path: Path) -> None:
    intake = tmp_path / "intake"
    intake.mkdir()
    (intake / "canonical_deal.json").write_text("{}")
    # No _complete marker
    state = LifecycleState(deal_slug="d", run_id="r", steps_completed=["intake"])
    write_provenance(intake, input_hash="h", contract_version=CONTRACT_VERSION)
    write_punchlist_json(tmp_path, [])
    assert is_satisfied(state, tmp_path, step="intake", current_input_hash="h") is False


def test_is_satisfied_fails_when_manifest_file_missing(tmp_path: Path) -> None:
    intake = tmp_path / "intake"
    intake.mkdir()
    write_complete_marker(intake, step="intake", file_manifest=["canonical_deal.json"])
    # Note: file in manifest does NOT exist
    state = LifecycleState(deal_slug="d", run_id="r", steps_completed=["intake"])
    write_provenance(intake, input_hash="h", contract_version=CONTRACT_VERSION)
    write_punchlist_json(tmp_path, [])
    assert is_satisfied(state, tmp_path, step="intake", current_input_hash="h") is False


def test_is_satisfied_fails_when_input_hash_changes(tmp_path: Path) -> None:
    run_dir = _make_run(tmp_path)
    state = LifecycleState(deal_slug="d", run_id="r", steps_completed=["intake"])
    write_provenance(run_dir / "intake", input_hash="OLD", contract_version=CONTRACT_VERSION)
    write_punchlist_json(run_dir, [])
    assert is_satisfied(state, run_dir, step="intake", current_input_hash="NEW") is False


def test_is_satisfied_fails_when_contract_version_changes(tmp_path: Path) -> None:
    run_dir = _make_run(tmp_path)
    state = LifecycleState(deal_slug="d", run_id="r", steps_completed=["intake"])
    write_provenance(run_dir / "intake", input_hash="h", contract_version="0.0.1")
    write_punchlist_json(run_dir, [])
    assert is_satisfied(state, run_dir, step="intake", current_input_hash="h") is False


def test_is_satisfied_fails_for_comps_after_ttl(tmp_path: Path) -> None:
    comps = tmp_path / "comps"
    comps.mkdir()
    (comps / "comps.json").write_text(json.dumps({
        "as_of": (datetime.now(timezone.utc) - timedelta(days=10)).date().isoformat(),
    }))
    write_complete_marker(comps, step="comps", file_manifest=["comps.json"])
    write_provenance(comps, input_hash="h", contract_version=CONTRACT_VERSION)
    write_punchlist_json(tmp_path, [])
    state = LifecycleState(deal_slug="d", run_id="r", steps_completed=["comps"])
    assert is_satisfied(state, tmp_path, step="comps", current_input_hash="h") is False


def test_is_satisfied_fails_when_marker_step_mismatch(tmp_path: Path) -> None:
    # _complete marker present but its `step` field belongs to a different
    # step (e.g., copy/paste accident). is_satisfied must treat this as a
    # cache miss and force a rerun.
    intake = tmp_path / "intake"
    intake.mkdir()
    (intake / "canonical_deal.json").write_text("{}")
    # Wrong step in marker — say it's a "comps" marker copied into intake/
    write_complete_marker(intake, step="comps", file_manifest=["canonical_deal.json"])
    state = LifecycleState(deal_slug="d", run_id="r", steps_completed=["intake"])
    write_provenance(intake, input_hash="h", contract_version=CONTRACT_VERSION)
    write_punchlist_json(tmp_path, [])
    assert is_satisfied(state, tmp_path, step="intake", current_input_hash="h") is False


def test_is_satisfied_fails_when_uncleared_blocker_present(tmp_path: Path) -> None:
    from plat_agent.lifecycle.state import BlockerItem
    run_dir = _make_run(tmp_path)
    state = LifecycleState(deal_slug="d", run_id="r", steps_completed=["intake"])
    write_provenance(run_dir / "intake", input_hash="h", contract_version=CONTRACT_VERSION)
    write_punchlist_json(run_dir, [
        BlockerItem(step="intake", id="missing_t12", description="x", cleared=False),
    ])
    assert is_satisfied(state, run_dir, step="intake", current_input_hash="h") is False
