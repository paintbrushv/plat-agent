import json
from pathlib import Path

from plat_agent.lifecycle.runner import (
    persist_state,
    write_initial_state,
)
from plat_agent.lifecycle.state import LifecycleState


def test_write_initial_state_creates_lifecycle_file(tmp_path: Path) -> None:
    write_initial_state(tmp_path, deal_slug="project_essex", run_id="run_001")
    state_path = tmp_path / "_lifecycle_state.json"
    assert state_path.exists()
    payload = json.loads(state_path.read_text())
    assert payload["deal_slug"] == "project_essex"
    assert payload["run_id"] == "run_001"
    assert payload["status"] == "running"
    assert payload["steps_completed"] == []
    assert payload["blockers"] == []
    assert "started_at" in payload


def test_persist_state_round_trip(tmp_path: Path) -> None:
    state = LifecycleState(
        deal_slug="d",
        run_id="r",
        steps_completed=["intake"],
    )
    persist_state(tmp_path, state)
    payload = json.loads((tmp_path / "_lifecycle_state.json").read_text())
    assert payload["steps_completed"] == ["intake"]


def test_persist_state_overwrites_atomically(tmp_path: Path) -> None:
    state = LifecycleState(deal_slug="d", run_id="r")
    persist_state(tmp_path, state)
    state.steps_completed = ["intake", "comps"]
    persist_state(tmp_path, state)
    payload = json.loads((tmp_path / "_lifecycle_state.json").read_text())
    assert payload["steps_completed"] == ["intake", "comps"]
    # No leftover .tmp files
    assert list(tmp_path.glob("_lifecycle_state.json.tmp")) == []
