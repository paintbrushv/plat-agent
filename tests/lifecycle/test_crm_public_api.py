# tests/lifecycle/test_crm_public_api.py
"""Verify CRM public API is accessible from plat_agent.lifecycle."""

import pytest


def test_crm_names_in_lifecycle_namespace() -> None:
    from plat_agent import lifecycle
    expected = {
        "CRMRow",
        "CRMStep",
        "book_deal",
        "list_deals",
        "get_latest",
        "atomic_append",
        "crm_path",
        "CRM_FILENAME",
    }
    for name in expected:
        assert hasattr(lifecycle, name), f"{name} missing from plat_agent.lifecycle"


def test_crm_names_in_all() -> None:
    from plat_agent import lifecycle
    for name in (
        "CRMRow", "CRMStep", "book_deal", "list_deals", "get_latest",
        "atomic_append", "crm_path", "CRM_FILENAME",
    ):
        assert name in lifecycle.__all__, f"{name} missing from __all__"


def test_crm_step_is_lifecycle_step_protocol() -> None:
    from pathlib import Path

    from plat_agent.lifecycle import CRMStep, LifecycleStep
    step = CRMStep(runs_deals_root=Path("/tmp/r"))
    assert isinstance(step, LifecycleStep)
    assert step.name == "crm"


def test_crm_module_importable_directly() -> None:
    from plat_agent.lifecycle import crm
    assert crm.CRM_FILENAME == "_crm.jsonl"
