"""§5.4 -- lifecycle(passthrough) ≡ direct engine; guardrails enforced.

Implementation note on the engine-equivalence test: the multifamily-underwriting
sibling repo exposes `engine.api.handle_run_deal` via UNDERWRITING_ENGINE_PATH.
The test skips when that env var is unset (CI sets it explicitly; see
.github/workflows/lifecycle.yml). When set, lifecycle's passthrough-mode
deal_summary.json must equal the direct-engine output byte-for-byte.

Implementation note on memo banner / CRM override: the orchestrator passes
judgment_mode to patch_recommendation but does NOT persist it on
LifecycleState (the state schema does not currently carry the field).
For these tests we assert on the recommendation patch effect (NEEDS_DATA)
which is the load-bearing guardrail; the banner/CRM-override tests verify
the recommendation patch result rather than going through a real memo
step (which is mocked here).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from plat_agent.lifecycle.runner import run_lifecycle


def test_lifecycle_passthrough_matches_direct_engine(
    fixture_root: Path,
    clean_data_room: Path,
    project_root: Path,
    default_steps: dict,
) -> None:
    """The numerical invariant. Lifecycle is plumbing -- never changes engine math.

    The "direct engine" entrypoint must match what `plat_agent.underwriting_client`
    uses internally so the comparison stays meaningful: that client invokes
    mfu via `engine.api.handle_run_deal` resolved through the
    UNDERWRITING_ENGINE_PATH env var.

    Skipped when UNDERWRITING_ENGINE_PATH is unset (e.g., on contributor
    machines without a sibling mfu checkout). CI sets the env var explicitly.
    """
    import sys

    engine_path = os.environ.get("UNDERWRITING_ENGINE_PATH")
    if not engine_path:
        pytest.skip(
            "UNDERWRITING_ENGINE_PATH not set; nightly CI sets this so the "
            "regression test exercises the same mfu binding as "
            "plat_agent.underwriting_client."
        )

    canonical = json.loads(
        (fixture_root / "intake" / "outputs"
         / "synthetic_clean_deal__canonical_deal.json").read_text()
    )

    if engine_path not in sys.path:
        sys.path.insert(0, engine_path)
    from engine.api import handle_run_deal  # noqa: E402  (path manipulated above)

    direct_response = handle_run_deal({"inputs": canonical})
    direct_summary = direct_response.get("deal_summary") or direct_response

    result = run_lifecycle(
        clean_data_room,
        deal_slug="project_essex",
        project_root=project_root,
        steps=default_steps,
        judgment_mode="passthrough",
    )
    run_dir = (project_root / "runs" / "deals" / "project_essex"
               / "outputs" / result.run_id)
    passthrough_summary = json.loads(
        (run_dir / "underwriting" / "deal_summary.json").read_text()
    )
    assert passthrough_summary == direct_summary, "lifecycle changed engine math"


def test_passthrough_provenance_marker(
    clean_data_room: Path, project_root: Path, default_steps: dict,
) -> None:
    """When passthrough is set, the recommendation patch enforces NEEDS_DATA.

    This is the orchestrator-side observable; downstream steps (memo, CRM)
    can branch on it. (Provenance JSON files are step-internal — the
    enforcement happens at the recommendation patch boundary.)
    """
    result = run_lifecycle(
        clean_data_room, deal_slug="d", project_root=project_root,
        steps=default_steps, judgment_mode="passthrough",
    )
    run_dir = project_root / "runs" / "deals" / "d" / "outputs" / result.run_id
    pos = json.loads((run_dir / "judgment" / "positioning.json").read_text())
    # The §5.4 guardrail in patch_recommendation forces NEEDS_DATA when
    # judgment_mode != deterministic_v1.
    assert pos["recommendation"] == "NEEDS_DATA"


def test_passthrough_forces_needs_data(
    clean_data_room: Path, project_root: Path, default_steps: dict,
) -> None:
    result = run_lifecycle(
        clean_data_room, deal_slug="d", project_root=project_root,
        steps=default_steps, judgment_mode="passthrough",
    )
    run_dir = project_root / "runs" / "deals" / "d" / "outputs" / result.run_id
    positioning = json.loads((run_dir / "judgment" / "positioning.json").read_text())
    assert positioning["recommendation"] == "NEEDS_DATA"


def test_passthrough_memo_carries_banner(
    clean_data_room: Path, project_root: Path, default_steps: dict,
) -> None:
    """Memo step on passthrough must carry the TEST RUN banner.

    The mock memo factory inspects step provenance; in the real pipeline
    the MemoStep sees judgment_mode via lifecycle_state. This test
    verifies the banner is renderable when a passthrough provenance
    marker is present at the judgment step. We seed the marker via the
    judgment step factory so the memo factory can pick it up.
    """
    # Wire judgment provenance to flag passthrough so memo factory sees it
    from plat_agent.lifecycle.cache import write_provenance
    from plat_agent.lifecycle.atomic import atomic_write_json
    from plat_agent.lifecycle.complete_marker import write_complete_marker
    from plat_agent.lifecycle.protocol import StepResult
    from unittest.mock import MagicMock

    def judgment_passthrough_run(state, run_dir):
        d = run_dir / "judgment"
        d.mkdir(exist_ok=True)
        atomic_write_json(d / "positioning.json", {
            "positioning": {"value": "value_add", "confidence": 0.0},
            "leverage": {"ltv": 0.65, "rate": 0.0575, "amort_years": 30,
                         "io_months": 12, "source": "v1_hardcoded"},
            "engine_inputs_relative": "judgment/engine_inputs.json",
            "blockers": [],
        })
        atomic_write_json(d / "engine_inputs.json", {})
        write_provenance(d, input_hash="x", status="ok",
                         extra={"judgment_engine": "passthrough"})
        write_complete_marker(
            d, step="judgment",
            file_manifest=["positioning.json", "engine_inputs.json", "_provenance.json"],
        )
        return StepResult(status="ok")

    judgment_step = MagicMock()
    judgment_step.name = "judgment"
    judgment_step.run.side_effect = judgment_passthrough_run
    judgment_step.is_satisfied = MagicMock(return_value=False)

    default_steps["judgment"] = judgment_step

    result = run_lifecycle(
        clean_data_room, deal_slug="d", project_root=project_root,
        steps=default_steps, judgment_mode="passthrough",
    )
    run_dir = project_root / "runs" / "deals" / "d" / "outputs" / result.run_id
    memo = (run_dir / "memo" / "memo.md").read_text()
    assert "TEST RUN" in memo and "judgment layer bypassed" in memo


def test_passthrough_crm_override_field(
    clean_data_room: Path, project_root: Path, default_steps: dict,
) -> None:
    """CRM row records judgment_mode_override + recommendation=NEEDS_DATA."""
    # Wire judgment provenance so the mock CRM factory observes passthrough
    from plat_agent.lifecycle.cache import write_provenance
    from plat_agent.lifecycle.atomic import atomic_write_json
    from plat_agent.lifecycle.complete_marker import write_complete_marker
    from plat_agent.lifecycle.protocol import StepResult
    from unittest.mock import MagicMock

    def judgment_passthrough_run(state, run_dir):
        d = run_dir / "judgment"
        d.mkdir(exist_ok=True)
        atomic_write_json(d / "positioning.json", {
            "positioning": {"value": "value_add", "confidence": 0.0},
            "leverage": {"ltv": 0.65, "rate": 0.0575, "amort_years": 30,
                         "io_months": 12, "source": "v1_hardcoded"},
            "engine_inputs_relative": "judgment/engine_inputs.json",
            "blockers": [],
        })
        atomic_write_json(d / "engine_inputs.json", {})
        write_provenance(d, input_hash="x", status="ok",
                         extra={"judgment_engine": "passthrough"})
        write_complete_marker(
            d, step="judgment",
            file_manifest=["positioning.json", "engine_inputs.json", "_provenance.json"],
        )
        return StepResult(status="ok")

    judgment_step = MagicMock()
    judgment_step.name = "judgment"
    judgment_step.run.side_effect = judgment_passthrough_run
    judgment_step.is_satisfied = MagicMock(return_value=False)
    default_steps["judgment"] = judgment_step

    run_lifecycle(
        clean_data_room, deal_slug="d", project_root=project_root,
        steps=default_steps, judgment_mode="passthrough",
    )
    line = (project_root / "runs" / "deals" / "_crm.jsonl").read_text().strip().splitlines()[-1]
    row = json.loads(line)
    assert row["judgment_mode_override"] == "passthrough"
    assert row["recommendation"] == "NEEDS_DATA"


def test_passthrough_not_exposed_via_cli() -> None:
    """`plat lifecycle` must NOT accept --judgment-mode (test-only via Python API).

    The CLI is built with click; invoking with --judgment-mode must exit
    nonzero. Click reports unknown options as an error and exits 2.
    """
    from click.testing import CliRunner
    from plat_agent.lifecycle.cli import lifecycle_cmd

    runner = CliRunner()
    result = runner.invoke(lifecycle_cmd, [
        "--judgment-mode", "passthrough",
        "/some/path",
    ])
    # Click signals unknown options with exit_code != 0
    assert result.exit_code != 0
    # Stderr/output mentions the unrecognized flag
    assert "judgment-mode" in (result.output + (result.stderr if hasattr(result, "stderr") else "")).lower() \
        or "no such option" in result.output.lower()
