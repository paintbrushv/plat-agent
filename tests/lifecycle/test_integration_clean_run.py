"""End-to-end integration test for run_lifecycle() with all dispatches mocked.

This is the single most important test in plan-07: it verifies the full
pipeline composition works against synthetic data, with each step's
dispatch boundary mocked to return contract-shaped fixtures.
"""

import json
import shutil
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from plat_agent.lifecycle.runner import run_lifecycle

FIXTURE_DR = Path(__file__).parent.parent / "fixtures" / "lifecycle" / \
             "data_rooms" / "synthetic_clean_deal"


@pytest.fixture
def project_root(tmp_path: Path) -> Path:
    p = tmp_path / "project_root"
    p.mkdir()
    return p


@pytest.fixture
def data_room(tmp_path: Path) -> Path:
    """Copy the fixture data room into tmp so tests can mutate freely."""
    dest = tmp_path / "data_room"
    shutil.copytree(FIXTURE_DR, dest)
    return dest


def test_clean_run_end_to_end(data_room: Path, project_root: Path) -> None:
    """Full 6-step pipeline against synthetic_clean_deal fixture."""
    # Mock each step to write contract-shaped artifacts and a _complete marker
    from plat_agent.lifecycle.protocol import StepResult
    from plat_agent.lifecycle.complete_marker import write_complete_marker
    from plat_agent.lifecycle.atomic import atomic_write_json, atomic_write_text
    from plat_agent.lifecycle.cache import write_provenance

    def intake_run(state, run_dir):
        d = run_dir / "intake"; d.mkdir()
        atomic_write_json(d / "canonical_deal.json", {
            "metadata": {"address": "1 Test Way", "year_built": 2005},
            "unit_cohorts": [{"cohort_id": "c1", "unit_count": 100}],
            "purchase_assumptions": {"purchase_price": 25_000_000},
        })
        atomic_write_text(d / "manifest.md", "# manifest")
        write_provenance(d, input_hash="x", status="ok")
        write_complete_marker(d, step="intake",
                             file_manifest=["canonical_deal.json", "manifest.md",
                                            "_provenance.json"])
        return StepResult(status="ok")

    def comps_run(state, run_dir):
        d = run_dir / "comps"; d.mkdir()
        atomic_write_json(d / "comps.json", {
            "subject": {"address": "1 Test Way", "metro_slug": "dallas_tx"},
            "as_of": "2026-05-05",
            "comps": [{"comp_id": "c1", "name": "Foo", "address": "x", "units": 240}],
        })
        write_provenance(d, input_hash="x", status="ok")
        write_complete_marker(d, step="comps",
                             file_manifest=["comps.json", "_provenance.json"])
        return StepResult(status="ok")

    def judgment_run(state, run_dir):
        d = run_dir / "judgment"; d.mkdir()
        atomic_write_json(d / "positioning.json", {
            "positioning": {"value": "value_add", "confidence": 0.85},
            "leverage": {"ltv": 0.65, "rate": 0.0575, "amort_years": 30,
                         "io_months": 12, "source": "v1_hardcoded"},
            "engine_inputs_relative": "judgment/engine_inputs.json",
            "blockers": [],
        })
        atomic_write_json(d / "engine_inputs.json", {
            "metadata": {"address": "1 Test Way", "year_built": 2005},
            "unit_cohorts": [{"cohort_id": "c1", "unit_count": 100}],
            "purchase_assumptions": {"purchase_price": 25_000_000},
        })
        atomic_write_text(d / "thesis.md", "# thesis")
        write_provenance(d, input_hash="x", status="ok")
        write_complete_marker(d, step="judgment",
                             file_manifest=["positioning.json", "engine_inputs.json",
                                            "thesis.md", "_provenance.json"])
        return StepResult(status="ok")

    def underwriting_run(state, run_dir):
        d = run_dir / "underwriting"; d.mkdir()
        atomic_write_json(d / "deal_summary.json", {
            "metrics": {
                "irr": {"levered_irr": 0.15},
                "equity_multiple": {"levered_em": 1.78},
                "yields": {"going_in_cap_rate": 0.052},
                "dscr": {"minimum_dscr": 1.25, "average_dscr": 1.45}, "coc": {"cash_on_cash_year_1": 0.08},
            },
            "sanity_flags": [],
        })
        write_provenance(d, input_hash="x", status="ok")
        write_complete_marker(d, step="underwriting",
                             file_manifest=["deal_summary.json", "_provenance.json"])
        return StepResult(status="ok")

    def memo_run(state, run_dir):
        d = run_dir / "memo"; d.mkdir()
        atomic_write_text(d / "memo.md", "# Memo for Test Way")
        atomic_write_text(d / "memo.pdf", "fake pdf bytes")  # placeholder
        write_provenance(d, input_hash="x", status="ok")
        write_complete_marker(d, step="memo",
                             file_manifest=["memo.md", "memo.pdf", "_provenance.json"])
        return StepResult(status="ok")

    def crm_run(state, run_dir):
        # Append to project-level CRM JSONL
        crm_path = project_root / "runs" / "deals" / "_crm.jsonl"
        crm_path.parent.mkdir(parents=True, exist_ok=True)
        with crm_path.open("a") as f:
            f.write(json.dumps({"deal_slug": state.deal_slug,
                                "run_id": state.run_id,
                                "status": state.status,
                                "ts": "2026-05-05T16:30:00Z"}) + "\n")
        d = run_dir / "crm"; d.mkdir()
        write_provenance(d, input_hash="x", status="ok")
        write_complete_marker(d, step="crm",
                             file_manifest=["_provenance.json"])
        return StepResult(status="ok")

    def make_step(name, fn):
        m = MagicMock(); m.name = name
        m.run.side_effect = fn
        m.is_satisfied = MagicMock(return_value=False)
        return m

    steps = {
        "intake": make_step("intake", intake_run),
        "comps": make_step("comps", comps_run),
        "judgment": make_step("judgment", judgment_run),
        "underwriting": make_step("underwriting", underwriting_run),
        "memo": make_step("memo", memo_run),
        "crm": make_step("crm", crm_run),
    }

    result = run_lifecycle(
        data_room,
        deal_slug="synthetic_clean_deal",
        project_root=project_root,
        steps=steps,
    )

    # Pipeline reached the end
    assert result.status == "memo_ready"
    assert result.exit_code == 0
    assert result.run_id == "run_001"
    assert result.crm_row_appended is True

    # Each step's outputs exist
    run_dir = (project_root / "runs" / "deals" / "synthetic_clean_deal" /
               "outputs" / "run_001")
    assert (run_dir / "intake" / "canonical_deal.json").exists()
    assert (run_dir / "comps" / "comps.json").exists()
    assert (run_dir / "judgment" / "positioning.json").exists()
    assert (run_dir / "underwriting" / "deal_summary.json").exists()
    assert (run_dir / "memo" / "memo.md").exists()

    # Step 4.5 patched the recommendation
    pos = json.loads((run_dir / "judgment" / "positioning.json").read_text())
    assert pos["recommendation"] == "PROCEED"
    assert pos["recommendation_confidence"] <= 0.6  # v1_hardcoded clamp

    # CRM has the row
    crm_lines = (project_root / "runs" / "deals" / "_crm.jsonl").read_text().splitlines()
    assert len(crm_lines) == 1
    assert json.loads(crm_lines[0])["deal_slug"] == "synthetic_clean_deal"

    # Final state
    final = json.loads((run_dir / "_lifecycle_state.json").read_text())
    assert final["status"] == "memo_ready"
    assert final["steps_completed"] == ["intake", "comps", "judgment",
                                        "underwriting", "memo", "crm"]
    assert final["finished_at"] is not None
