from datetime import datetime, timezone

import pytest

from plat_agent.lifecycle.state import (
    BlockerItem,
    LifecycleState,
    LifecycleStatus,
    RecommendationEnum,
)


def test_blocker_item_defaults() -> None:
    b = BlockerItem(step="intake", id="missing_t12", description="No T12 found.")
    assert b.cleared is False
    assert b.step == "intake"
    assert b.id == "missing_t12"
    assert b.resolution_hint is None


def test_blocker_item_cleared_set() -> None:
    b = BlockerItem(step="intake", id="missing_t12", description="x", cleared=True)
    assert b.cleared is True


def test_blocker_item_resolution_hint_set() -> None:
    b = BlockerItem(
        step="intake",
        id="missing_t12",
        description="No T12 found.",
        resolution_hint="drop broker's T12_2024.xlsx into raw_inputs/ and rerun.",
    )
    assert b.resolution_hint == "drop broker's T12_2024.xlsx into raw_inputs/ and rerun."


def test_lifecycle_state_initial() -> None:
    state = LifecycleState(deal_slug="project_essex", run_id="run_002")
    assert state.deal_slug == "project_essex"
    assert state.run_id == "run_002"
    assert state.status == "running"
    assert state.steps_completed == []
    assert state.blockers == []
    assert state.finished_at is None


def test_lifecycle_state_status_enum_values() -> None:
    # Valid statuses (any of these strings)
    valid = {
        "running",
        "memo_ready",
        "memo_ready_with_blockers",
        "failed_at_intake",
        "failed_at_comps",
        "failed_at_judgment",
        "failed_at_underwriting",
        "failed_at_memo",
        "failed_at_crm",
        "failed_at_punchlist_parse",
    }
    for s in valid:
        state = LifecycleState(deal_slug="x", run_id="r", status=s)
        assert state.status == s


def test_recommendation_enum_values() -> None:
    assert RecommendationEnum.PROCEED == "PROCEED"
    assert RecommendationEnum.DECLINE == "DECLINE"
    assert RecommendationEnum.NEEDS_DATA == "NEEDS_DATA"


def test_lifecycle_state_round_trip_json() -> None:
    state = LifecycleState(
        deal_slug="x",
        run_id="r",
        steps_completed=["intake", "comps"],
        blockers=[BlockerItem(step="intake", id="missing_t12", description="x")],
    )
    payload = state.model_dump_json()
    restored = LifecycleState.model_validate_json(payload)
    assert restored.steps_completed == ["intake", "comps"]
    assert restored.blockers[0].id == "missing_t12"
