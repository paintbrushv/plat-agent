import json
from pathlib import Path

import pytest

from plat_agent.lifecycle.recommendation_patch import patch_recommendation


def _make_judgment_dir(tmp_path: Path, **positioning_overrides) -> Path:
    judgment = tmp_path / "judgment"
    judgment.mkdir()
    base = {
        "positioning": {"value": "value_add", "confidence": 0.85, "rationale": "x"},
        "leverage": {"ltv": 0.65, "rate": 0.0575, "amort_years": 30, "io_months": 12,
                     "source": "v1_hardcoded"},
        "engine_inputs_relative": "judgment/engine_inputs.json",
        "blockers": [],
    }
    base.update(positioning_overrides)
    (judgment / "positioning.json").write_text(json.dumps(base))
    return judgment


def _make_underwriting_dir(
    tmp_path: Path,
    *,
    levered_irr=0.15,
    min_dscr=1.25,
    cash_on_cash_year_1: float | None = 0.08,  # V1.5: 8% Y1 CoC default for PROCEED
) -> Path:
    uw = tmp_path / "underwriting"
    uw.mkdir()
    (uw / "deal_summary.json").write_text(json.dumps({
        "metrics": {
            "irr": {"levered_irr": levered_irr},
            "dscr": {"minimum_dscr": min_dscr},
            "coc": {"cash_on_cash_year_1": cash_on_cash_year_1},
        },
    }))
    return uw


def test_patch_proceed_writes_back_to_positioning_json(tmp_path: Path) -> None:
    _make_judgment_dir(tmp_path)
    _make_underwriting_dir(tmp_path)
    patch_recommendation(tmp_path, comps_unavailable=False, judgment_mode="deterministic_v1")
    payload = json.loads((tmp_path / "judgment" / "positioning.json").read_text())
    # Hardcoded leverage caps at 0.6 per §6.A.1, but base rule is PROCEED
    assert payload["recommendation"] == "PROCEED"
    assert payload["recommendation_confidence"] <= 0.6


def test_patch_decline_on_low_coc(tmp_path: Path) -> None:
    """V1.5: PROCEED requires CoC >= 7%. Low CoC → DECLINE."""
    _make_judgment_dir(tmp_path)
    _make_underwriting_dir(tmp_path, cash_on_cash_year_1=0.04)
    patch_recommendation(tmp_path, comps_unavailable=False, judgment_mode="deterministic_v1")
    payload = json.loads((tmp_path / "judgment" / "positioning.json").read_text())
    assert payload["recommendation"] == "DECLINE"


def test_patch_decline_on_low_dscr(tmp_path: Path) -> None:
    """V1.5: PROCEED requires DSCR >= 1.20."""
    _make_judgment_dir(tmp_path)
    _make_underwriting_dir(tmp_path, min_dscr=1.10)
    patch_recommendation(tmp_path, comps_unavailable=False, judgment_mode="deterministic_v1")
    payload = json.loads((tmp_path / "judgment" / "positioning.json").read_text())
    assert payload["recommendation"] == "DECLINE"


def test_patch_needs_data_when_coc_missing(tmp_path: Path) -> None:
    """V1.5: missing CoC → NEEDS_DATA (engine didn't surface it)."""
    _make_judgment_dir(tmp_path)
    _make_underwriting_dir(tmp_path, cash_on_cash_year_1=None)
    patch_recommendation(tmp_path, comps_unavailable=False, judgment_mode="deterministic_v1")
    payload = json.loads((tmp_path / "judgment" / "positioning.json").read_text())
    assert payload["recommendation"] == "NEEDS_DATA"


def test_patch_needs_data_when_underwriting_missing(tmp_path: Path) -> None:
    """Per §2.3 edge case: if underwriting fails, recommendation = NEEDS_DATA."""
    _make_judgment_dir(tmp_path)
    # No underwriting dir
    patch_recommendation(tmp_path, comps_unavailable=False, judgment_mode="deterministic_v1")
    payload = json.loads((tmp_path / "judgment" / "positioning.json").read_text())
    assert payload["recommendation"] == "NEEDS_DATA"


def test_patch_needs_data_when_comps_unavailable(tmp_path: Path) -> None:
    _make_judgment_dir(tmp_path)
    _make_underwriting_dir(tmp_path)
    patch_recommendation(tmp_path, comps_unavailable=True, judgment_mode="deterministic_v1")
    payload = json.loads((tmp_path / "judgment" / "positioning.json").read_text())
    assert payload["recommendation"] == "NEEDS_DATA"


def test_patch_needs_data_when_blockers_present(tmp_path: Path) -> None:
    _make_judgment_dir(tmp_path, blockers=[{"step": "intake", "id": "missing_t12",
                                            "description": "x", "cleared": False}])
    _make_underwriting_dir(tmp_path)
    patch_recommendation(tmp_path, comps_unavailable=False, judgment_mode="deterministic_v1",
                         blocker_count=1)
    payload = json.loads((tmp_path / "judgment" / "positioning.json").read_text())
    assert payload["recommendation"] == "NEEDS_DATA"


def test_patch_passthrough_forces_needs_data(tmp_path: Path) -> None:
    """§5.4 passthrough guardrail: judgment_mode != 'deterministic_v1' forces NEEDS_DATA."""
    _make_judgment_dir(tmp_path)
    _make_underwriting_dir(tmp_path)
    patch_recommendation(tmp_path, comps_unavailable=False, judgment_mode="passthrough")
    payload = json.loads((tmp_path / "judgment" / "positioning.json").read_text())
    assert payload["recommendation"] == "NEEDS_DATA"


def test_patch_is_atomic_no_tmp_left(tmp_path: Path) -> None:
    _make_judgment_dir(tmp_path)
    _make_underwriting_dir(tmp_path)
    patch_recommendation(tmp_path, comps_unavailable=False, judgment_mode="deterministic_v1")
    assert list((tmp_path / "judgment").glob("positioning.json.tmp")) == []


def test_patch_sets_recommendation_patched_at(tmp_path: Path) -> None:
    """Returns timestamp for orchestrator to update lifecycle state."""
    _make_judgment_dir(tmp_path)
    _make_underwriting_dir(tmp_path)
    ts = patch_recommendation(tmp_path, comps_unavailable=False,
                              judgment_mode="deterministic_v1")
    assert ts is not None
