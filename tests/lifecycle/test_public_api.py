"""Verify the lifecycle package's public API is complete and consistent."""

import pytest


def test_public_imports_resolve() -> None:
    """All names in __all__ must be importable."""
    from plat_agent import lifecycle
    for name in lifecycle.__all__:
        assert hasattr(lifecycle, name), f"{name} in __all__ but not exported"


def test_state_types_in_public_api() -> None:
    from plat_agent.lifecycle import (
        BlockerItem,
        LifecycleState,
        LifecycleStatus,
        RecommendationEnum,
        StepName,
    )
    # Smoke
    state = LifecycleState(deal_slug="d", run_id="r")
    assert state.status == "running"


def test_protocol_in_public_api() -> None:
    from plat_agent.lifecycle import LifecycleStep, StepResult, StepStatus
    r = StepResult(status="ok")
    assert r.status == "ok"


def test_atomic_in_public_api() -> None:
    from plat_agent.lifecycle import atomic_write_json, atomic_write_text
    assert callable(atomic_write_json)
    assert callable(atomic_write_text)


def test_defaults_in_public_api() -> None:
    from plat_agent.lifecycle import (
        CONTRACT_VERSION,
        DEFAULT_LEVERAGE_V1,
        JUDGMENT_ENGINE_V1,
        derive_recommendation,
    )
    assert CONTRACT_VERSION == "1.0.0"
    assert callable(derive_recommendation)


def test_punchlist_in_public_api() -> None:
    from plat_agent.lifecycle import (
        PunchlistParseError,
        read_punchlist_json,
        reconcile_punchlist,
        render_punchlist_markdown,
        write_punchlist_json,
    )
    assert issubclass(PunchlistParseError, Exception)


def test_cache_in_public_api() -> None:
    from plat_agent.lifecycle import compute_input_hash, is_satisfied, write_provenance
    assert callable(compute_input_hash)
    assert callable(is_satisfied)
    assert callable(write_provenance)
