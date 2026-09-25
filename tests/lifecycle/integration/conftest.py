"""Shared fixtures for lifecycle integration tests.

Implementation note: in the actual lifecycle architecture, real dispatch
to sibling agents (multifamily-underwriting) is wrapped inside
IntakeStep / CompsStep / UnderwritingStep. These tests inject mock
step adapters via the `steps=` param of `run_lifecycle()`, which is the
same dependency-injection pattern used by every other lifecycle test.
The smoke tests therefore exercise the real orchestrator (run_id alloc,
raw_inputs snapshot, dependency map, recommendation patch, CRM step)
end-to-end while keeping the cross-repo dispatch boundary mocked.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from plat_agent.lifecycle.atomic import atomic_write_json, atomic_write_text
from plat_agent.lifecycle.cache import write_provenance
from plat_agent.lifecycle.complete_marker import write_complete_marker
from plat_agent.lifecycle.protocol import StepResult


FIXTURE_ROOT = Path(__file__).resolve().parents[2] / "fixtures" / "lifecycle"


@pytest.fixture
def fixture_root() -> Path:
    return FIXTURE_ROOT


@pytest.fixture
def clean_data_room(tmp_path: Path) -> Path:
    """Build the clean fixture into a tmp dir so tests don't mutate the source."""
    from tests.fixtures.lifecycle.builders import write_clean_deal_room
    target = tmp_path / "clean_deal"
    write_clean_deal_room(target)
    return target


@pytest.fixture
def missing_t12_data_room(tmp_path: Path) -> Path:
    from tests.fixtures.lifecycle.builders import write_missing_t12_deal_room
    target = tmp_path / "missing_t12"
    write_missing_t12_deal_room(target)
    return target


@pytest.fixture
def fake_comps_response() -> dict:
    """Pre-built comps payload matching the clean-deal fixture."""
    return json.loads(
        (FIXTURE_ROOT / "comps" / "outputs" / "synthetic_clean_deal__comps.json").read_text()
    )


@pytest.fixture
def project_root(tmp_path: Path) -> Path:
    """Isolated project root; runs/deals/ lives under it."""
    p = tmp_path / "project_root"
    p.mkdir()
    return p


# --- Step-mock factory helpers ---
# Each helper returns a MagicMock that satisfies the LifecycleStep protocol
# (name attr, run(state, run_dir), is_satisfied(state, run_dir)). The run()
# side_effect writes contract-shaped artifacts + a _complete marker so the
# orchestrator can reason about cache validity and the recommendation patch
# step has well-formed positioning.json + deal_summary.json to work with.


def _intake_run_factory():
    def intake_run(state, run_dir):
        d = run_dir / "intake"
        d.mkdir(exist_ok=True)
        atomic_write_json(d / "canonical_deal.json", {
            "metadata": {
                "address": "1234 Main St, Dallas, TX 75201",
                "year_built": 2005,
                "market": "dallas_tx",
            },
            "unit_cohorts": [{"cohort_id": "1br", "unit_count": 240}],
            "purchase_assumptions": {"purchase_price": 28_000_000},
        })
        atomic_write_text(d / "manifest.md", "# manifest\n")
        write_provenance(d, input_hash="x", status="ok")
        write_complete_marker(
            d, step="intake",
            file_manifest=["canonical_deal.json", "manifest.md", "_provenance.json"],
        )
        return StepResult(status="ok")
    return intake_run


def _comps_run_factory(comps_payload: dict | None = None,
                       call_counter: list[int] | None = None):
    payload = comps_payload or {
        "subject": {"address": "1234 Main St, Dallas, TX 75201",
                    "metro_slug": "dallas_tx"},
        "as_of": "2026-05-05",
        "comps": [
            {"comp_id": "c1", "name": "A", "address": "x", "units": 240},
            {"comp_id": "c2", "name": "B", "address": "y", "units": 220},
            {"comp_id": "c3", "name": "C", "address": "z", "units": 198},
        ],
    }

    def comps_run(state, run_dir):
        if call_counter is not None:
            call_counter.append(1)
        d = run_dir / "comps"
        d.mkdir(exist_ok=True)
        atomic_write_json(d / "comps.json", payload)
        write_provenance(d, input_hash="x", status="ok")
        write_complete_marker(
            d, step="comps",
            file_manifest=["comps.json", "_provenance.json"],
        )
        return StepResult(status="ok")
    return comps_run


def _judgment_run_factory(positioning_overrides: dict | None = None,
                          blockers: list | None = None):
    pos = {
        "positioning": {"value": "value_add", "confidence": 0.85},
        "leverage": {"ltv": 0.65, "rate": 0.0575, "amort_years": 30,
                     "io_months": 12, "source": "v1_hardcoded"},
        "engine_inputs_relative": "judgment/engine_inputs.json",
        "blockers": blockers or [],
    }
    if positioning_overrides:
        pos.update(positioning_overrides)

    def judgment_run(state, run_dir):
        d = run_dir / "judgment"
        d.mkdir(exist_ok=True)
        atomic_write_json(d / "positioning.json", pos)
        atomic_write_json(d / "engine_inputs.json", {
            "metadata": {"address": "1234 Main St, Dallas, TX 75201", "year_built": 2005},
            "unit_cohorts": [{"cohort_id": "1br", "unit_count": 240}],
            "purchase_assumptions": {"purchase_price": 28_000_000},
        })
        atomic_write_text(d / "thesis.md", "# thesis\n")
        write_provenance(d, input_hash="x", status="ok")
        write_complete_marker(
            d, step="judgment",
            file_manifest=[
                "positioning.json", "engine_inputs.json", "thesis.md",
                "_provenance.json",
            ],
        )
        from plat_agent.lifecycle.state import BlockerItem
        result_blockers = []
        for b in (blockers or []):
            if isinstance(b, BlockerItem):
                result_blockers.append(b)
            elif isinstance(b, dict):
                result_blockers.append(BlockerItem(**b))
        return StepResult(status="ok", blockers=result_blockers)
    return judgment_run


def _underwriting_run_factory():
    def underwriting_run(state, run_dir):
        d = run_dir / "underwriting"
        d.mkdir(exist_ok=True)
        atomic_write_json(d / "deal_summary.json", {
            "metrics": {
                "irr": {"levered_irr": 0.15, "unlevered_irr": 0.105},
                "equity_multiple": {"levered_em": 1.78, "unlevered_em": 1.5},
                "yields": {"going_in_cap_rate": 0.052},
                "dscr": {"minimum_dscr": 1.25, "average_dscr": 1.45}, "coc": {"cash_on_cash_year_1": 0.08},
            },
            "sanity_flags": [],
        })
        write_provenance(d, input_hash="x", status="ok")
        write_complete_marker(
            d, step="underwriting",
            file_manifest=["deal_summary.json", "_provenance.json"],
        )
        return StepResult(status="ok")
    return underwriting_run


def _memo_run_factory():
    def memo_run(state, run_dir):
        d = run_dir / "memo"
        d.mkdir(exist_ok=True)
        # Compose a memo body that adapts to the orchestrator's signals
        positioning_path = run_dir / "judgment" / "positioning.json"
        recommendation = "PROCEED"
        if positioning_path.exists():
            try:
                pos = json.loads(positioning_path.read_text())
                recommendation = pos.get("recommendation", "PROCEED")
            except Exception:
                pass

        # Detect "comps unavailable" by checking the comps step state
        comps_path = run_dir / "comps" / "comps.json"
        comps_section = ""
        if not comps_path.exists() or "unavailable" in (
            (run_dir / "comps" / "_provenance.json").read_text()
            if (run_dir / "comps" / "_provenance.json").exists()
            else ""
        ):
            comps_section = "\nComp data unavailable; recommendation forced to NEEDS_DATA.\n"

        # Detect passthrough banner (judgment_engine == passthrough in any provenance)
        banner = ""
        for step in ["intake", "judgment", "underwriting"]:
            prov_p = run_dir / step / "_provenance.json"
            if prov_p.exists():
                try:
                    prov = json.loads(prov_p.read_text())
                    if prov.get("judgment_engine") == "passthrough":
                        banner = ("> TEST RUN: judgment layer bypassed (passthrough mode); "
                                  "do NOT use for IC.\n\n")
                        break
                except Exception:
                    pass

        # Detect underwriting failure (no deal_summary.json) -> draft memo
        draft_marker = ""
        if not (run_dir / "underwriting" / "deal_summary.json").exists():
            draft_marker = "\n**DRAFT — blockers pending**\n"

        memo_md = (
            f"# Investment Committee Memo\n\n"
            f"{banner}"
            f"**Recommendation:** {recommendation}\n"
            f"{draft_marker}"
            f"{comps_section}"
            "\n## Returns\n\nLevered IRR: 15.0%\n"
        )
        atomic_write_text(d / "memo.md", memo_md)
        atomic_write_text(d / "memo.pdf", "fake pdf bytes\n")
        write_provenance(d, input_hash="x", status="ok")
        write_complete_marker(
            d, step="memo",
            file_manifest=["memo.md", "memo.pdf", "_provenance.json"],
        )
        return StepResult(status="ok")
    return memo_run


def _crm_run_factory(project_root: Path):
    def crm_run(state, run_dir):
        crm_jsonl = project_root / "runs" / "deals" / "_crm.jsonl"
        crm_jsonl.parent.mkdir(parents=True, exist_ok=True)

        # Read positioning for recommendation
        recommendation = None
        pos_p = run_dir / "judgment" / "positioning.json"
        if pos_p.exists():
            try:
                recommendation = json.loads(pos_p.read_text()).get("recommendation")
            except Exception:
                pass

        # Read deal_summary for metrics
        levered_irr = None
        ds_p = run_dir / "underwriting" / "deal_summary.json"
        if ds_p.exists():
            try:
                ds = json.loads(ds_p.read_text())
                levered_irr = ds.get("metrics", {}).get("irr", {}).get("levered_irr")
            except Exception:
                pass

        # Detect passthrough mode from judgment provenance
        judgment_mode_override = None
        prov_p = run_dir / "judgment" / "_provenance.json"
        if prov_p.exists():
            try:
                prov = json.loads(prov_p.read_text())
                if prov.get("judgment_engine") == "passthrough":
                    judgment_mode_override = "passthrough"
            except Exception:
                pass

        memo_path = run_dir / "memo" / "memo.md"
        row = {
            "deal_slug": state.deal_slug,
            "run_id": state.run_id,
            "status": state.status,
            "recommendation": recommendation,
            "levered_irr": levered_irr,
            "memo_path": str(memo_path) if memo_path.exists() else None,
            "judgment_mode_override": judgment_mode_override,
            "ts": "2026-05-05T16:30:00Z",
        }

        # Use atomic flock for concurrent safety
        from plat_agent.lifecycle.crm import atomic_append, CRMRow
        try:
            atomic_append(crm_jsonl, CRMRow(**row))
        except Exception:
            # Fallback: write as raw JSON line (CRMRow may have stricter schema)
            import fcntl
            with crm_jsonl.open("a") as f:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                try:
                    f.write(json.dumps(row) + "\n")
                finally:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)

        d = run_dir / "crm"
        d.mkdir(exist_ok=True)
        write_provenance(d, input_hash="x", status="ok")
        write_complete_marker(
            d, step="crm",
            file_manifest=["_provenance.json"],
        )
        return StepResult(status="ok")
    return crm_run


def _make_step(name: str, run_fn) -> MagicMock:
    m = MagicMock()
    m.name = name
    m.run.side_effect = run_fn
    m.is_satisfied = MagicMock(return_value=False)
    return m


@pytest.fixture
def make_default_steps(project_root: Path):
    """Factory: returns a fresh dict of mock steps (each test gets its own)."""
    def _factory(*,
                 comps_payload: dict | None = None,
                 comps_call_counter: list[int] | None = None,
                 judgment_blockers: list | None = None,
                 judgment_overrides: dict | None = None) -> dict:
        return {
            "intake": _make_step("intake", _intake_run_factory()),
            "comps": _make_step("comps", _comps_run_factory(comps_payload, comps_call_counter)),
            "judgment": _make_step(
                "judgment",
                _judgment_run_factory(judgment_overrides, judgment_blockers),
            ),
            "underwriting": _make_step("underwriting", _underwriting_run_factory()),
            "memo": _make_step("memo", _memo_run_factory()),
            "crm": _make_step("crm", _crm_run_factory(project_root)),
        }
    return _factory


@pytest.fixture
def default_steps(make_default_steps):
    """The plain-vanilla 6-step happy-path mock pipeline."""
    return make_default_steps()
