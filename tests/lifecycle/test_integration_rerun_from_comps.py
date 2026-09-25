"""Integration test: --rerun-from comps overwrites comps + downstream."""

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from plat_agent.lifecycle.atomic import atomic_write_json, atomic_write_text
from plat_agent.lifecycle.cache import write_provenance
from plat_agent.lifecycle.complete_marker import write_complete_marker
from plat_agent.lifecycle.protocol import StepResult
from plat_agent.lifecycle.runner import run_lifecycle


@pytest.fixture
def project_root(tmp_path: Path) -> Path:
    p = tmp_path / "project_root"; p.mkdir(); return p


@pytest.fixture
def data_room(tmp_path: Path) -> Path:
    src = tmp_path / "data_room"; src.mkdir()
    (src / "OM.pdf").write_bytes(b"x")
    return src


def _writing_step(name: str, payload_supplier):
    """Step that writes a contract-shaped artifact each time it's called.

    payload_supplier is a callable that returns the JSON payload — used to
    differentiate first-run vs rerun-from artifacts.
    """
    call_idx = {"n": 0}
    def run(state, run_dir):
        d = run_dir / name; d.mkdir(exist_ok=True)
        call_idx["n"] += 1
        payload = payload_supplier(call_idx["n"])
        atomic_write_json(d / f"{name}.json" if name != "memo" else d / "memo.md",
                          payload) if name != "memo" else \
            atomic_write_text(d / "memo.md", payload)
        write_provenance(d, input_hash="x", status="ok")
        write_complete_marker(d, step=name,
                             file_manifest=[f"{name}.json" if name != "memo" else "memo.md",
                                            "_provenance.json"])
        return StepResult(status="ok")
    m = MagicMock(); m.name = name; m.run.side_effect = run
    m.is_satisfied = MagicMock(return_value=False)
    return m, call_idx


def test_rerun_from_comps_overwrites_downstream(data_room: Path,
                                                project_root: Path) -> None:
    intake_step, intake_calls = _writing_step("intake",
        lambda n: {"metadata": {"address": "1 Test Way", "year_built": 2005},
                   "unit_cohorts": [{"cohort_id": "c1", "unit_count": 100}],
                   "purchase_assumptions": {"purchase_price": 25_000_000}})
    comps_step, comps_calls = _writing_step("comps",
        lambda n: {"subject": {"address": "x", "metro_slug": "y"},
                   "as_of": f"2026-05-0{n}",  # changes per call to differentiate
                   "comps": [{"comp_id": "c1", "name": "x", "address": "x",
                              "units": 240}]})
    j_calls = {"n": 0}
    def judgment_run(state, run_dir):
        j_calls["n"] += 1
        d = run_dir / "judgment"; d.mkdir(exist_ok=True)
        atomic_write_json(d / "positioning.json", {
            "positioning": {"confidence": 0.85},
            "leverage": {"source": "v1_hardcoded"},
            "blockers": []})
        atomic_write_json(d / "engine_inputs.json", {})
        write_provenance(d, input_hash="x", status="ok")
        write_complete_marker(d, step="judgment",
                             file_manifest=["positioning.json", "engine_inputs.json",
                                            "_provenance.json"])
        return StepResult(status="ok")
    judgment_step = MagicMock(); judgment_step.name = "judgment"
    judgment_step.run.side_effect = judgment_run
    judgment_step.is_satisfied = MagicMock(return_value=False)

    underwriting_step, uw_calls = _writing_step("underwriting",
        lambda n: {"metrics": {"irr": {"levered_irr": 0.15},
                               "dscr": {"minimum_dscr": 1.25}, "coc": {"cash_on_cash_year_1": 0.08}}})
    memo_step, m_calls = _writing_step("memo", lambda n: f"# memo run {n}")
    crm_step, c_calls = _writing_step("crm",
        lambda n: {"deal_slug": "d", "run_id": "r"})

    steps = {
        "intake": intake_step, "comps": comps_step, "judgment": judgment_step,
        "underwriting": underwriting_step, "memo": memo_step, "crm": crm_step,
    }

    first = run_lifecycle(data_room, deal_slug="d", project_root=project_root,
                         steps=steps)

    # Capture pre-rerun call counts
    pre = {n: c["n"] for n, c in [
        ("intake", intake_calls), ("comps", comps_calls),
        ("judgment", j_calls), ("underwriting", uw_calls),
        ("memo", m_calls), ("crm", c_calls),
    ]}
    assert pre == {"intake": 1, "comps": 1, "judgment": 1,
                   "underwriting": 1, "memo": 1, "crm": 1}

    # Mark intake as satisfied; everything else still unsatisfied
    intake_step.is_satisfied.return_value = True

    second = run_lifecycle(data_room, deal_slug="d", project_root=project_root,
                          steps=steps, resume_run_id=first.run_id,
                          rerun_from="comps")

    post = {n: c["n"] for n, c in [
        ("intake", intake_calls), ("comps", comps_calls),
        ("judgment", j_calls), ("underwriting", uw_calls),
        ("memo", m_calls), ("crm", c_calls),
    ]}
    # Intake NOT re-run; comps + downstream all re-run exactly once more
    assert post["intake"] == 1  # still 1 (cached)
    assert post["comps"] == 2
    assert post["judgment"] == 2
    assert post["underwriting"] == 2
    assert post["memo"] == 2
    assert post["crm"] == 2

    # Comps file was overwritten with new as_of
    comps_payload = json.loads(
        (project_root / "runs" / "deals" / "d" / "outputs" / first.run_id /
         "comps" / "comps.json").read_text()
    )
    assert comps_payload["as_of"] == "2026-05-02"  # second call value
