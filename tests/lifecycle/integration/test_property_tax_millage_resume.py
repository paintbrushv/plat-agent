from __future__ import annotations

import json
import os
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Callable

import pytest

from plat_agent.dispatch.sibling import SiblingRepo
from plat_agent.lifecycle.atomic import atomic_write_json, atomic_write_text
from plat_agent.lifecycle.cache import (
    compute_input_hash,
    is_satisfied as cache_is_satisfied,
    write_provenance,
)
from plat_agent.lifecycle.complete_marker import write_complete_marker
from plat_agent.lifecycle.crm import CRMStep
from plat_agent.lifecycle.memo import MemoStep
from plat_agent.lifecycle.protocol import StepResult
from plat_agent.lifecycle.runner import run_lifecycle
from plat_agent.lifecycle.state import LifecycleState
from plat_agent.lifecycle.steps.underwriting import UnderwritingStep


pytestmark = pytest.mark.integration


@pytest.fixture
def multifamily_repo() -> Path:
    configured = os.environ.get("MULTIFAMILY_UNDERWRITING_REPO")
    if not configured:
        pytest.skip(
            "real cross-repo integration requires "
            "MULTIFAMILY_UNDERWRITING_REPO"
        )
    repo = Path(configured).expanduser().resolve()
    runner = repo / "runs" / "federated_underwrite.py"
    if not runner.is_file():
        pytest.skip(
            "MULTIFAMILY_UNDERWRITING_REPO has no "
            "runs/federated_underwrite.py"
        )
    return repo


def _intake_input_paths(run_dir: Path) -> list[Path]:
    raw_inputs = run_dir / "raw_inputs"
    return sorted(
        (path for path in raw_inputs.rglob("*") if path.is_file()),
        key=lambda path: path.relative_to(raw_inputs).as_posix(),
    )


def _comps_input_paths(run_dir: Path) -> list[Path]:
    return [run_dir / "intake" / "canonical_deal.json"]


def _judgment_input_paths(run_dir: Path) -> list[Path]:
    return [
        run_dir / "intake" / "canonical_deal.json",
        run_dir / "comps" / "comps.json",
    ]


class _FixtureStep:
    def __init__(
        self,
        name: str,
        run: Callable[[LifecycleState, Path], StepResult],
        input_paths: Callable[[Path], list[Path]],
        calls: dict[str, int],
    ) -> None:
        self.name = name
        self._run = run
        self._input_paths = input_paths
        self._calls = calls

    def run(self, state: LifecycleState, run_dir: Path) -> StepResult:
        self._calls[self.name] += 1
        return self._run(state, run_dir)

    def is_satisfied(self, state: LifecycleState, run_dir: Path) -> bool:
        satisfied = cache_is_satisfied(
            state,
            run_dir,
            step=self.name,
            current_input_hash=compute_input_hash(self._input_paths(run_dir)),
        )
        result = "hits" if satisfied else "misses"
        self._calls[f"{self.name}_cache_{result}"] += 1
        return satisfied


class _RecordingStep:
    def __init__(
        self,
        step: UnderwritingStep,
        calls: dict[str, int],
    ) -> None:
        self.name = step.name
        self._step = step
        self._calls = calls

    def run(self, state: object, run_dir: Path) -> StepResult:
        self._calls[self.name] += 1
        return self._step.run(state, run_dir)

    def is_satisfied(self, state: object, run_dir: Path) -> bool:
        return self._step.is_satisfied(state, run_dir)


def _write_step_metadata(
    step_dir: Path,
    *,
    step: str,
    inputs: list[Path],
    manifest: list[str],
) -> None:
    write_provenance(step_dir, input_hash=compute_input_hash(inputs), status="ok")
    write_complete_marker(
        step_dir,
        step=step,
        file_manifest=[*manifest, "_provenance.json"],
    )


def _temporary_multifamily_repo_facade(
    project_root: Path,
    multifamily_repo: Path,
) -> SiblingRepo:
    """Expose configured worktree code with its repository virtualenv."""
    local_venv = multifamily_repo / ".venv"
    if (local_venv / "bin" / "python").is_file():
        return SiblingRepo(name="multifamily-underwriting", path=multifamily_repo)

    venv_candidates = []
    if multifamily_repo.parent.name == ".worktrees":
        venv_candidates.append(multifamily_repo.parents[1] / ".venv")
    parent_venv = next(
        (
            candidate
            for candidate in venv_candidates
            if (candidate / "bin" / "python").is_file()
        ),
        None,
    )
    if parent_venv is None:
        pytest.skip(
            "configured multifamily repository has no usable .venv; "
            "worktrees require the parent repository virtualenv"
        )

    facade = project_root.parent / "real-multifamily-underwriting"
    facade.mkdir()
    (facade / "runs").symlink_to(multifamily_repo / "runs", target_is_directory=True)
    (facade / ".venv").symlink_to(parent_venv, target_is_directory=True)
    return SiblingRepo(name="multifamily-underwriting", path=facade)


def lifecycle_steps_using_real_multifamily_underwriting(
    *,
    project_root: Path,
    canonical_fixture: Path,
    multifamily_repo: Path,
) -> tuple[dict[str, object], dict[str, int]]:
    calls = {
        "intake": 0,
        "comps": 0,
        "judgment": 0,
        "underwriting": 0,
        "intake_cache_hits": 0,
        "intake_cache_misses": 0,
        "comps_cache_hits": 0,
        "comps_cache_misses": 0,
        "judgment_cache_hits": 0,
        "judgment_cache_misses": 0,
    }

    def intake_run(state: LifecycleState, run_dir: Path) -> StepResult:
        intake_dir = run_dir / "intake"
        intake_dir.mkdir(parents=True, exist_ok=True)
        canonical = json.loads(canonical_fixture.read_text())
        atomic_write_json(intake_dir / "canonical_deal.json", canonical)
        atomic_write_text(intake_dir / "manifest.md", "# Minimal property-tax fixture\n")
        _write_step_metadata(
            intake_dir,
            step="intake",
            inputs=_intake_input_paths(run_dir),
            manifest=["canonical_deal.json", "manifest.md"],
        )
        return StepResult(status="ok")

    def comps_run(state: LifecycleState, run_dir: Path) -> StepResult:
        comps_dir = run_dir / "comps"
        comps_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_json(
            comps_dir / "comps.json",
            {
                "subject": {
                    "address": "100 Millage Test Way, Dallas, TX 75201",
                    "metro_slug": "dallas_tx",
                },
                "as_of": date.today().isoformat(),
                "comps": [],
            },
        )
        _write_step_metadata(
            comps_dir,
            step="comps",
            inputs=_comps_input_paths(run_dir),
            manifest=["comps.json"],
        )
        return StepResult(status="ok")

    def judgment_run(state: LifecycleState, run_dir: Path) -> StepResult:
        judgment_dir = run_dir / "judgment"
        judgment_dir.mkdir(parents=True, exist_ok=True)
        canonical_path = run_dir / "intake" / "canonical_deal.json"
        canonical = json.loads(canonical_path.read_text())
        atomic_write_json(
            judgment_dir / "positioning.json",
            {
                "positioning": {"value": "core_plus", "confidence": 0.85},
                "leverage": {
                    "ltv": 0.65,
                    "rate": 0.0575,
                    "amort_years": 30,
                    "io_months": 12,
                    "source": "v1_hardcoded",
                },
                "engine_inputs_relative": "judgment/engine_inputs.json",
                "blockers": [],
            },
        )
        atomic_write_json(judgment_dir / "engine_inputs.json", canonical)
        atomic_write_text(
            judgment_dir / "thesis.md",
            "# Deterministic property-tax acceptance fixture\n",
        )
        _write_step_metadata(
            judgment_dir,
            step="judgment",
            inputs=_judgment_input_paths(run_dir),
            manifest=["positioning.json", "engine_inputs.json", "thesis.md"],
        )
        return StepResult(status="ok")

    sibling_repo = _temporary_multifamily_repo_facade(
        project_root,
        multifamily_repo,
    )
    underwriting = _RecordingStep(
        UnderwritingStep(sibling_repo=sibling_repo),
        calls,
    )

    return (
        {
            "intake": _FixtureStep(
                "intake", intake_run, _intake_input_paths, calls
            ),
            "comps": _FixtureStep(
                "comps", comps_run, _comps_input_paths, calls
            ),
            "judgment": _FixtureStep(
                "judgment", judgment_run, _judgment_input_paths, calls
            ),
            "underwriting": underwriting,
            "memo": MemoStep(),
            "crm": CRMStep(project_root / "runs" / "deals"),
        },
        calls,
    )


def test_cli_millage_preserves_approved_ratio_in_real_downstream_tax(
    tmp_path: Path,
    multifamily_repo: Path,
) -> None:
    project_root = tmp_path / "multifamily-underwriting"
    project_root.mkdir()
    data_room = tmp_path / "Ratio Deal Room"
    data_room.mkdir()
    source_fixture = (
        Path(__file__).parents[1]
        / "fixtures"
        / "property_tax_minimal_canonical.json"
    )
    canonical = json.loads(source_fixture.read_text())
    canonical["metadata"]["property_summary"]["property_tax_policy"] = {
        "millage_rate_mills": 24.90,
        "assessment_ratio": 0.7,
        "source": "composite_evidence",
        "source_locator": (
            "millage=county_tax_notice:raw_inputs/notice.pdf, page 2;"
            "assessment_ratio=cad_rate_table:raw_inputs/CAD.pdf, page 3"
        ),
        "analyst_override": True,
    }
    canonical_fixture = tmp_path / "ratio_canonical.json"
    atomic_write_json(canonical_fixture, canonical)
    steps, calls = lifecycle_steps_using_real_multifamily_underwriting(
        project_root=project_root,
        canonical_fixture=canonical_fixture,
        multifamily_repo=multifamily_repo,
    )

    result = run_lifecycle(
        data_room,
        project_root=project_root,
        steps=steps,
        millage_rate="25.31",
    )

    assert result.status == "memo_ready"
    assert result.exit_code == 0
    assert calls["judgment"] == 1
    assert calls["underwriting"] == 1
    run_dir = (
        project_root
        / "runs"
        / "deals"
        / result.deal_slug
        / "outputs"
        / result.run_id
    )
    persisted = json.loads(
        (run_dir / "intake" / "canonical_deal.json").read_text()
    )
    expected_policy = {
        "millage_rate_mills": 25.31,
        "assessment_ratio": 0.7,
        "source": "composite_evidence",
        "source_locator": (
            "millage=plat lifecycle:--millage-rate;"
            "assessment_ratio=cad_rate_table:raw_inputs/CAD.pdf, page 3"
        ),
        "analyst_override": True,
    }
    assert (
        persisted["metadata"]["property_summary"]["property_tax_policy"]
        == expected_policy
    )
    summary = json.loads(
        (run_dir / "underwriting" / "deal_summary.json").read_text()
    )
    tax = summary["property_tax_calculation"]
    assert tax["millage_rate_mills"] == 25.31
    assert tax["assessment_ratio"] == 0.7
    assert tax["assessed_value_basis"] == 7_000_000.0
    assert tax["annual_ad_valorem_tax"] == 177_170.0
    assert tax["source"] == "composite_evidence"
    assert tax["source_locator"] == expected_policy["source_locator"]


def test_missing_millage_blocks_then_resume_reaches_consistent_tax_outputs(
    tmp_path: Path,
    multifamily_repo: Path,
) -> None:
    project_root = tmp_path / "multifamily-underwriting"
    project_root.mkdir()
    data_room = tmp_path / "Deal Room"
    data_room.mkdir()
    canonical_fixture = (
        Path(__file__).parents[1]
        / "fixtures"
        / "property_tax_minimal_canonical.json"
    )
    steps, calls = lifecycle_steps_using_real_multifamily_underwriting(
        project_root=project_root,
        canonical_fixture=canonical_fixture,
        multifamily_repo=multifamily_repo,
    )

    blocked = run_lifecycle(data_room, project_root=project_root, steps=steps)

    assert blocked.status == "needs_analyst_input"
    assert blocked.exit_code == 2
    assert calls["judgment"] == 0
    assert calls["underwriting"] == 0

    resumed = run_lifecycle(
        data_room,
        project_root=project_root,
        steps=steps,
        deal_slug=blocked.deal_slug,
        resume_run_id=blocked.run_id,
        millage_rate="25.31",
    )

    assert resumed.status == "memo_ready"
    assert resumed.exit_code == 0
    assert calls["judgment"] == 1
    assert calls["underwriting"] == 1
    assert calls["intake"] == 1
    assert calls["comps"] == 1
    assert calls["intake_cache_hits"] == 1
    assert calls["comps_cache_misses"] == 1
    cached = run_lifecycle(
        data_room,
        project_root=project_root,
        steps=steps,
        deal_slug=blocked.deal_slug,
        resume_run_id=blocked.run_id,
        millage_rate="25.31",
    )
    assert cached.status == "memo_ready"
    assert cached.exit_code == 0
    assert calls["intake"] == 1
    assert calls["comps"] == 1
    assert calls["judgment"] == 1
    assert calls["underwriting"] == 1
    assert calls["intake_cache_hits"] == 2
    assert calls["comps_cache_hits"] == 1
    assert calls["judgment_cache_hits"] == 1
    run_dir = (
        project_root
        / "runs"
        / "deals"
        / blocked.deal_slug
        / "outputs"
        / blocked.run_id
    )
    canonical = json.loads(
        (run_dir / "intake" / "canonical_deal.json").read_text()
    )
    engine_inputs = json.loads(
        (run_dir / "judgment" / "engine_inputs.json").read_text()
    )
    summary = json.loads(
        (run_dir / "underwriting" / "deal_summary.json").read_text()
    )
    memo = (run_dir / "memo" / "memo.md").read_text()
    crm = json.loads((run_dir / "crm" / "crm_row.json").read_text())

    expected_policy = {
        "millage_rate_mills": 25.31,
        "assessment_ratio": 1.0,
        "source": "analyst",
        "source_locator": "plat lifecycle:--millage-rate",
        "analyst_override": False,
    }
    assert canonical["metadata"]["property_summary"]["property_tax_policy"] == expected_policy
    assert engine_inputs["metadata"]["property_summary"]["property_tax_policy"] == expected_policy
    assert next(
        row
        for row in engine_inputs["opex_table"]
        if row["category_name"] == "Real Estate Taxes"
    )["base_value"] == 123_456

    tax = summary["property_tax_calculation"]
    assert tax["millage_rate_mills"] == 25.31
    assert tax["assessment_ratio"] == 1.0
    assert tax["purchase_price_basis"] == 10_000_000.0
    assert tax["assessed_value_basis"] == 10_000_000.0
    assert tax["annual_ad_valorem_tax"] == 253_100.0
    assert tax["source"] == "analyst"
    assert tax["source_locator"] == "plat lifecycle:--millage-rate"
    first_twelve_tax_rows = [
        row
        for row in summary["opex"]["by_category_by_month"]
        if row["category"] == "Real Estate Taxes"
    ][:12]
    modeled_year_one_tax = sum(
        (Decimal(str(row["expense"])) for row in first_twelve_tax_rows),
        Decimal("0"),
    )
    assert len(first_twelve_tax_rows) == 12
    assert (
        abs(modeled_year_one_tax - Decimal(str(tax["annual_ad_valorem_tax"])))
        <= Decimal("0.06")
    )
    assert modeled_year_one_tax != Decimal("123456")
    first_twelve_cashflow = summary["cashflow"]["by_month"][:12]
    first_twelve_opex_totals = summary["opex"]["totals_by_month"][:12]
    first_twelve_months = {
        row["month"] for row in first_twelve_opex_totals
    }
    category_sums_by_month = {
        month: Decimal("0") for month in first_twelve_months
    }
    category_counts_by_month = {
        month: 0 for month in first_twelve_months
    }
    for row in summary["opex"]["by_category_by_month"]:
        month = row["month"]
        if month not in first_twelve_months:
            continue
        category_sums_by_month[month] += Decimal(str(row["expense"]))
        category_counts_by_month[month] += 1

    assert len(category_sums_by_month) == 12
    for cashflow, opex in zip(
        first_twelve_cashflow,
        first_twelve_opex_totals,
        strict=True,
    ):
        month = opex["month"]
        assert cashflow["month"] == month
        independent_cent_rounding_tolerance = (
            Decimal("0.005")
            * Decimal(category_counts_by_month[month] + 1)
        )
        assert (
            abs(
                category_sums_by_month[month]
                - Decimal(str(opex["total_opex"]))
            )
            <= independent_cent_rounding_tolerance
        )
        assert Decimal(str(cashflow["total_opex"])) == Decimal(
            str(opex["total_opex"])
        )
        assert (
            Decimal(str(cashflow["effective_gross_income"]))
            - Decimal(str(cashflow["total_opex"]))
            == Decimal(str(cashflow["net_operating_income"]))
        )
    assert "Annual ad valorem tax: $253,100.00" in memo
    assert crm["property_tax_annual_ad_valorem_tax"] == tax["annual_ad_valorem_tax"]
    assert crm["property_tax_source_locator"] == tax["source_locator"]
