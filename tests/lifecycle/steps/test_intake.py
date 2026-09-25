"""Tests for the lifecycle intake step (plan-05)."""

from __future__ import annotations

from pathlib import Path

from plat_agent.lifecycle.protocol import LifecycleStep
from plat_agent.lifecycle.state import LifecycleState
from plat_agent.lifecycle.steps.intake import IntakeStep


def test_intake_step_has_name_intake() -> None:
    step = IntakeStep()
    assert step.name == "intake"


def test_intake_step_satisfies_lifecycle_step_protocol() -> None:
    step = IntakeStep()
    # runtime_checkable Protocol from plan-01 — verifies attrs + methods
    assert isinstance(step, LifecycleStep)


from plat_agent.contracts.envelope import BridgeRequestV1
from plat_agent.contracts.domain.intake import DealIntakeRequest


def test_build_request_envelope_from_state(tmp_path: Path) -> None:
    state = LifecycleState(deal_slug="project_essex", run_id="run_002")
    deal_root = tmp_path / "deals" / "project_essex"
    run_dir = deal_root / "outputs" / "run_002"
    run_dir.mkdir(parents=True)
    (run_dir / "raw_inputs").mkdir()

    step = IntakeStep()
    request = step._build_request(state, run_dir=run_dir, deal_root=deal_root)

    assert isinstance(request, BridgeRequestV1)
    assert request.deal_slug == "project_essex"
    assert request.run_id == "run_002"
    assert request.deal_root == str(deal_root)
    assert request.agent_name == "deal-intake"


def test_build_request_payload_points_at_run_scoped_raw_inputs(tmp_path: Path) -> None:
    state = LifecycleState(deal_slug="d", run_id="run_001")
    deal_root = tmp_path / "deals" / "d"
    run_dir = deal_root / "outputs" / "run_001"
    run_dir.mkdir(parents=True)

    step = IntakeStep()
    request = step._build_request(state, run_dir=run_dir, deal_root=deal_root)

    payload = DealIntakeRequest.model_validate(request.payload)
    # Per spec §3 pre-step: raw_inputs is run-scoped under outputs/<run_id>/
    assert payload.raw_inputs_dir_relative == "outputs/run_001/raw_inputs"


def test_build_request_payload_validates_against_DealIntakeRequest(tmp_path: Path) -> None:
    state = LifecycleState(deal_slug="d", run_id="r")
    deal_root = tmp_path / "deals" / "d"
    run_dir = deal_root / "outputs" / "r"
    run_dir.mkdir(parents=True)

    step = IntakeStep()
    request = step._build_request(state, run_dir=run_dir, deal_root=deal_root)
    # Should not raise
    DealIntakeRequest.model_validate(request.payload)


from unittest.mock import patch

from plat_agent.contracts.envelope import (
    ArtifactRef,
    BridgeError,
    BridgeResponseV1,
)


def _make_ok_response(deal_slug: str, run_id: str) -> BridgeResponseV1:
    return BridgeResponseV1(
        status="ok",
        deal_slug=deal_slug,
        run_id=run_id,
        agent_name="deal-intake",
        payload={
            "canonical_deal_json_relative": f"outputs/{run_id}/intake/canonical_deal.json",
            "manifest_relative": f"outputs/{run_id}/intake/manifest.md",
            "punchlist_relative": f"outputs/{run_id}/intake/intake_punchlist.md",
            "cohorts_identified": 3,
            "documents_classified": {"OM_Final.pdf": "om", "RR.xlsx": "rent_roll"},
        },
        artifacts=[
            ArtifactRef(
                relative_path=f"outputs/{run_id}/intake/canonical_deal.json",
                kind="json",
                description="Canonical deal JSON",
            ),
            ArtifactRef(
                relative_path=f"outputs/{run_id}/intake/manifest.md",
                kind="md",
                description="Doc manifest",
            ),
        ],
    )


def test_dispatch_called_with_deal_intake_response_payload_model(tmp_path: Path) -> None:
    """IntakeStep must pass payload_model=DealIntakeResponse so dispatch
    validates the sibling's payload at the boundary (Stage 3 HIGH-1)."""
    from plat_agent.contracts.domain.intake import DealIntakeResponse
    state = LifecycleState(deal_slug="d", run_id="r")
    deal_root = tmp_path / "deals" / "d"
    run_dir = deal_root / "outputs" / "r"
    run_dir.mkdir(parents=True)
    (run_dir / "raw_inputs").mkdir()

    captured = {}

    def fake_dispatch(repo, request, *, payload_model=None, **kwargs):
        captured["payload_model"] = payload_model
        captured["agent_name"] = request.agent_name
        return _make_ok_response("d", "r")

    step = IntakeStep()
    with patch(
        "plat_agent.lifecycle.steps.intake.dispatch_sibling_agent",
        side_effect=fake_dispatch,
    ):
        # Stub the artifact files so post-run verification doesn't fail
        intake_dir = run_dir / "intake"
        intake_dir.mkdir(parents=True)
        (intake_dir / "canonical_deal.json").write_text("{}")
        (intake_dir / "manifest.md").write_text("# manifest\n")
        (intake_dir / "intake_punchlist.md").write_text("")
        step.run(state, run_dir)

    assert captured["payload_model"] is DealIntakeResponse
    assert captured["agent_name"] == "deal-intake"


def test_dispatch_uses_mfu_sibling_repo(tmp_path: Path, monkeypatch) -> None:
    """SiblingRepo resolution should use PLAT_MULTIFAMILY_UNDERWRITING_PATH
    if set (the env-var convention from dispatch.sibling.SiblingRepo)."""
    fake_repo = tmp_path / "fake_mfu"
    (fake_repo / ".claude" / "agents").mkdir(parents=True)
    (fake_repo / ".claude" / "agents" / "deal-intake.md").write_text("# x\n")
    monkeypatch.setenv("PLAT_MULTIFAMILY_UNDERWRITING_PATH", str(fake_repo))

    state = LifecycleState(deal_slug="d", run_id="r")
    deal_root = tmp_path / "deals" / "d"
    run_dir = deal_root / "outputs" / "r"
    run_dir.mkdir(parents=True)
    (run_dir / "raw_inputs").mkdir()

    captured_repo = {}

    def fake_dispatch(repo, request, **kwargs):
        captured_repo["path"] = repo.path
        return _make_ok_response("d", "r")

    step = IntakeStep()
    with patch(
        "plat_agent.lifecycle.steps.intake.dispatch_sibling_agent",
        side_effect=fake_dispatch,
    ):
        intake_dir = run_dir / "intake"
        intake_dir.mkdir(parents=True)
        (intake_dir / "canonical_deal.json").write_text("{}")
        (intake_dir / "manifest.md").write_text("")
        (intake_dir / "intake_punchlist.md").write_text("")
        step.run(state, run_dir)

    assert captured_repo["path"] == fake_repo.resolve()


def test_status_ok_returns_step_result_ok(tmp_path: Path) -> None:
    state = LifecycleState(deal_slug="d", run_id="r")
    deal_root = tmp_path / "deals" / "d"
    run_dir = deal_root / "outputs" / "r"
    run_dir.mkdir(parents=True)
    (run_dir / "raw_inputs").mkdir()
    intake_dir = run_dir / "intake"
    intake_dir.mkdir()
    (intake_dir / "canonical_deal.json").write_text("{}")
    (intake_dir / "manifest.md").write_text("")
    (intake_dir / "intake_punchlist.md").write_text("")

    step = IntakeStep()
    with patch(
        "plat_agent.lifecycle.steps.intake.dispatch_sibling_agent",
        return_value=_make_ok_response("d", "r"),
    ):
        result = step.run(state, run_dir)
    assert result.status == "ok"
    assert result.blockers == []


def test_status_needs_analyst_input_returns_blocked(tmp_path: Path) -> None:
    state = LifecycleState(deal_slug="d", run_id="r")
    deal_root = tmp_path / "deals" / "d"
    run_dir = deal_root / "outputs" / "r"
    run_dir.mkdir(parents=True)
    (run_dir / "raw_inputs").mkdir()
    intake_dir = run_dir / "intake"
    intake_dir.mkdir()
    (intake_dir / "canonical_deal.json").write_text("{}")
    (intake_dir / "manifest.md").write_text("")
    (intake_dir / "intake_punchlist.md").write_text(
        "# Punchlist\n- missing_t12: T12 not detected\n"
    )

    response = BridgeResponseV1(
        status="needs_analyst_input",
        deal_slug="d",
        run_id="r",
        agent_name="deal-intake",
        payload={
            "canonical_deal_json_relative": "outputs/r/intake/canonical_deal.json",
            "manifest_relative": "outputs/r/intake/manifest.md",
            "punchlist_relative": "outputs/r/intake/intake_punchlist.md",
            "cohorts_identified": 0,
            "documents_classified": {},
        },
        artifacts=[],
        error=BridgeError(
            code="missing_input",
            message="T12 not detected in raw_inputs/",
            recoverable=True,
            details={"blockers": [
                {"id": "missing_t12", "description": "T12 not detected in raw_inputs/"},
            ]},
        ),
    )

    step = IntakeStep()
    with patch(
        "plat_agent.lifecycle.steps.intake.dispatch_sibling_agent",
        return_value=response,
    ):
        result = step.run(state, run_dir)
    assert result.status == "blocked"
    assert len(result.blockers) >= 1
    assert all(b.step == "intake" for b in result.blockers)
    assert result.blockers[0].id == "missing_t12"


def test_status_error_returns_error_with_message(tmp_path: Path) -> None:
    state = LifecycleState(deal_slug="d", run_id="r")
    deal_root = tmp_path / "deals" / "d"
    run_dir = deal_root / "outputs" / "r"
    run_dir.mkdir(parents=True)
    (run_dir / "raw_inputs").mkdir()

    response = BridgeResponseV1(
        status="error",
        deal_slug="d",
        run_id="r",
        agent_name="deal-intake",
        payload={},
        artifacts=[],
        error=BridgeError(
            code="schema_violation",
            message="ingest_deal.py crashed",
            recoverable=False,
        ),
    )

    step = IntakeStep()
    with patch(
        "plat_agent.lifecycle.steps.intake.dispatch_sibling_agent",
        return_value=response,
    ):
        result = step.run(state, run_dir)
    assert result.status == "error"
    assert result.error_message is not None
    assert "ingest_deal.py crashed" in result.error_message


from plat_agent.lifecycle.punchlist import (
    read_punchlist_json,
    write_punchlist_json,
    PUNCHLIST_MD,
)


def test_blockers_merged_into_run_level_punchlist(tmp_path: Path) -> None:
    state = LifecycleState(deal_slug="d", run_id="r")
    deal_root = tmp_path / "deals" / "d"
    run_dir = deal_root / "outputs" / "r"
    run_dir.mkdir(parents=True)
    (run_dir / "raw_inputs").mkdir()
    intake_dir = run_dir / "intake"
    intake_dir.mkdir()
    (intake_dir / "canonical_deal.json").write_text("{}")
    (intake_dir / "manifest.md").write_text("")
    (intake_dir / "intake_punchlist.md").write_text("")

    response = BridgeResponseV1(
        status="needs_analyst_input",
        deal_slug="d",
        run_id="r",
        agent_name="deal-intake",
        payload={
            "canonical_deal_json_relative": "outputs/r/intake/canonical_deal.json",
            "manifest_relative": "outputs/r/intake/manifest.md",
            "punchlist_relative": "outputs/r/intake/intake_punchlist.md",
            "cohorts_identified": 0,
            "documents_classified": {},
        },
        error=BridgeError(
            code="missing_input",
            message="missing T12",
            recoverable=True,
            details={"blockers": [
                {"id": "missing_t12", "description": "T12 not detected"},
                {"id": "rent_roll_dirty", "description": "12 of 240 null in-place rents"},
            ]},
        ),
    )

    step = IntakeStep()
    with patch(
        "plat_agent.lifecycle.steps.intake.dispatch_sibling_agent",
        return_value=response,
    ):
        step.run(state, run_dir)

    items = read_punchlist_json(run_dir)
    ids = {b.id for b in items}
    assert "missing_t12" in ids
    assert "rent_roll_dirty" in ids
    # punchlist.md regenerated for analyst
    assert (run_dir / PUNCHLIST_MD).exists()


def test_blockers_preserve_other_steps_existing_items(tmp_path: Path) -> None:
    """If a prior step (or earlier resume) wrote blockers under another step,
    the intake step must not delete them — only merge its own."""
    from plat_agent.lifecycle.state import BlockerItem as BI
    state = LifecycleState(deal_slug="d", run_id="r")
    deal_root = tmp_path / "deals" / "d"
    run_dir = deal_root / "outputs" / "r"
    run_dir.mkdir(parents=True)
    (run_dir / "raw_inputs").mkdir()
    intake_dir = run_dir / "intake"
    intake_dir.mkdir()
    (intake_dir / "canonical_deal.json").write_text("{}")
    (intake_dir / "manifest.md").write_text("")
    (intake_dir / "intake_punchlist.md").write_text("")

    # Pre-existing blocker from another step
    write_punchlist_json(run_dir, [
        BI(step="comps", id="scraper_blocked", description="2 of 6 403'd"),
    ])

    response = _make_ok_response("d", "r")
    step = IntakeStep()
    with patch(
        "plat_agent.lifecycle.steps.intake.dispatch_sibling_agent",
        return_value=response,
    ):
        step.run(state, run_dir)

    items = read_punchlist_json(run_dir)
    ids = {b.id for b in items}
    assert "scraper_blocked" in ids


def test_intake_step_replaces_only_its_own_prior_blockers(tmp_path: Path) -> None:
    """Re-running intake with a now-fixed input should clear its own stale
    blockers, not duplicate them."""
    from plat_agent.lifecycle.state import BlockerItem as BI
    state = LifecycleState(deal_slug="d", run_id="r")
    deal_root = tmp_path / "deals" / "d"
    run_dir = deal_root / "outputs" / "r"
    run_dir.mkdir(parents=True)
    (run_dir / "raw_inputs").mkdir()
    intake_dir = run_dir / "intake"
    intake_dir.mkdir()
    (intake_dir / "canonical_deal.json").write_text("{}")
    (intake_dir / "manifest.md").write_text("")
    (intake_dir / "intake_punchlist.md").write_text("")

    # Stale intake blocker from a prior run
    write_punchlist_json(run_dir, [
        BI(step="intake", id="missing_t12", description="x"),
        BI(step="comps", id="scraper_blocked", description="y"),
    ])

    response = _make_ok_response("d", "r")  # status=ok, no blockers
    step = IntakeStep()
    with patch(
        "plat_agent.lifecycle.steps.intake.dispatch_sibling_agent",
        return_value=response,
    ):
        step.run(state, run_dir)

    items = read_punchlist_json(run_dir)
    ids_by_step = {(b.step, b.id) for b in items}
    assert ("intake", "missing_t12") not in ids_by_step  # cleared
    assert ("comps", "scraper_blocked") in ids_by_step    # preserved


def test_status_ok_but_canonical_missing_returns_error(tmp_path: Path) -> None:
    """If the sibling claims ok but canonical_deal.json is absent, surface
    as an error — the run is not actually usable downstream."""
    state = LifecycleState(deal_slug="d", run_id="r")
    deal_root = tmp_path / "deals" / "d"
    run_dir = deal_root / "outputs" / "r"
    run_dir.mkdir(parents=True)
    (run_dir / "raw_inputs").mkdir()
    # Note: deliberately NOT creating canonical_deal.json
    intake_dir = run_dir / "intake"
    intake_dir.mkdir()
    (intake_dir / "manifest.md").write_text("")
    (intake_dir / "intake_punchlist.md").write_text("")

    step = IntakeStep()
    with patch(
        "plat_agent.lifecycle.steps.intake.dispatch_sibling_agent",
        return_value=_make_ok_response("d", "r"),
    ):
        result = step.run(state, run_dir)

    assert result.status == "error"
    assert "canonical_deal.json" in (result.error_message or "")


def test_status_ok_but_manifest_missing_returns_error(tmp_path: Path) -> None:
    state = LifecycleState(deal_slug="d", run_id="r")
    deal_root = tmp_path / "deals" / "d"
    run_dir = deal_root / "outputs" / "r"
    run_dir.mkdir(parents=True)
    (run_dir / "raw_inputs").mkdir()
    intake_dir = run_dir / "intake"
    intake_dir.mkdir()
    (intake_dir / "canonical_deal.json").write_text("{}")
    # No manifest.md

    step = IntakeStep()
    with patch(
        "plat_agent.lifecycle.steps.intake.dispatch_sibling_agent",
        return_value=_make_ok_response("d", "r"),
    ):
        result = step.run(state, run_dir)

    assert result.status == "error"
    assert "manifest.md" in (result.error_message or "")


def test_status_ok_with_all_artifacts_returns_ok(tmp_path: Path) -> None:
    """Happy path: ok response + both required artifacts on disk."""
    state = LifecycleState(deal_slug="d", run_id="r")
    deal_root = tmp_path / "deals" / "d"
    run_dir = deal_root / "outputs" / "r"
    run_dir.mkdir(parents=True)
    (run_dir / "raw_inputs").mkdir()
    intake_dir = run_dir / "intake"
    intake_dir.mkdir()
    (intake_dir / "canonical_deal.json").write_text("{}")
    (intake_dir / "manifest.md").write_text("# manifest")
    (intake_dir / "intake_punchlist.md").write_text("")

    step = IntakeStep()
    with patch(
        "plat_agent.lifecycle.steps.intake.dispatch_sibling_agent",
        return_value=_make_ok_response("d", "r"),
    ):
        result = step.run(state, run_dir)
    assert result.status == "ok"


def test_input_hash_stable_across_calls(tmp_path: Path) -> None:
    state = LifecycleState(deal_slug="d", run_id="r")
    deal_root = tmp_path / "deals" / "d"
    run_dir = deal_root / "outputs" / "r"
    run_dir.mkdir(parents=True)
    raw_inputs = run_dir / "raw_inputs"
    raw_inputs.mkdir()
    (raw_inputs / "OM.pdf").write_bytes(b"%PDF-1.4 hello")
    (raw_inputs / "RR.xlsx").write_bytes(b"PKxlsx")

    step = IntakeStep()
    h1 = step._compute_input_hash(run_dir)
    h2 = step._compute_input_hash(run_dir)
    assert h1 == h2
    assert len(h1) == 64  # sha256 hex


def test_input_hash_changes_when_raw_inputs_change(tmp_path: Path) -> None:
    state = LifecycleState(deal_slug="d", run_id="r")
    deal_root = tmp_path / "deals" / "d"
    run_dir = deal_root / "outputs" / "r"
    run_dir.mkdir(parents=True)
    raw_inputs = run_dir / "raw_inputs"
    raw_inputs.mkdir()
    (raw_inputs / "OM.pdf").write_bytes(b"v1")

    step = IntakeStep()
    h1 = step._compute_input_hash(run_dir)
    (raw_inputs / "OM.pdf").write_bytes(b"v2_different")
    h2 = step._compute_input_hash(run_dir)
    assert h1 != h2


def test_input_hash_recurses_into_subdirectories(tmp_path: Path) -> None:
    """Some data rooms have nested folders (e.g. /financials/T12.xlsx)."""
    state = LifecycleState(deal_slug="d", run_id="r")
    deal_root = tmp_path / "deals" / "d"
    run_dir = deal_root / "outputs" / "r"
    run_dir.mkdir(parents=True)
    raw_inputs = run_dir / "raw_inputs"
    (raw_inputs / "financials").mkdir(parents=True)
    (raw_inputs / "financials" / "T12.xlsx").write_bytes(b"t12")

    step = IntakeStep()
    h1 = step._compute_input_hash(run_dir)
    (raw_inputs / "financials" / "T12.xlsx").write_bytes(b"t12_modified")
    h2 = step._compute_input_hash(run_dir)
    assert h1 != h2


def test_input_hash_when_raw_inputs_missing(tmp_path: Path) -> None:
    """Empty / missing raw_inputs/ should hash to a stable value (not crash)."""
    deal_root = tmp_path / "deals" / "d"
    run_dir = deal_root / "outputs" / "r"
    run_dir.mkdir(parents=True)
    # No raw_inputs/ directory at all

    step = IntakeStep()
    h = step._compute_input_hash(run_dir)
    assert isinstance(h, str)
    assert len(h) == 64


import json


def test_provenance_written_on_ok(tmp_path: Path) -> None:
    state = LifecycleState(deal_slug="d", run_id="r")
    deal_root = tmp_path / "deals" / "d"
    run_dir = deal_root / "outputs" / "r"
    run_dir.mkdir(parents=True)
    (run_dir / "raw_inputs").mkdir()
    (run_dir / "raw_inputs" / "OM.pdf").write_bytes(b"x")
    intake_dir = run_dir / "intake"
    intake_dir.mkdir()
    (intake_dir / "canonical_deal.json").write_text("{}")
    (intake_dir / "manifest.md").write_text("")
    (intake_dir / "intake_punchlist.md").write_text("")

    step = IntakeStep()
    with patch(
        "plat_agent.lifecycle.steps.intake.dispatch_sibling_agent",
        return_value=_make_ok_response("d", "r"),
    ):
        step.run(state, run_dir)

    prov_path = intake_dir / "_provenance.json"
    assert prov_path.exists()
    payload = json.loads(prov_path.read_text())
    assert "input_hash" in payload
    assert payload["contract_version"] == "1.0.0"
    assert payload["status"] == "ok"


def test_complete_marker_written_on_ok(tmp_path: Path) -> None:
    state = LifecycleState(deal_slug="d", run_id="r")
    deal_root = tmp_path / "deals" / "d"
    run_dir = deal_root / "outputs" / "r"
    run_dir.mkdir(parents=True)
    (run_dir / "raw_inputs").mkdir()
    intake_dir = run_dir / "intake"
    intake_dir.mkdir()
    (intake_dir / "canonical_deal.json").write_text("{}")
    (intake_dir / "manifest.md").write_text("")
    (intake_dir / "intake_punchlist.md").write_text("")

    step = IntakeStep()
    with patch(
        "plat_agent.lifecycle.steps.intake.dispatch_sibling_agent",
        return_value=_make_ok_response("d", "r"),
    ):
        step.run(state, run_dir)

    complete = intake_dir / "_complete"
    assert complete.exists()
    payload = json.loads(complete.read_text())
    assert payload["step"] == "intake"
    assert "canonical_deal.json" in payload["file_manifest"]
    assert "manifest.md" in payload["file_manifest"]


def test_complete_marker_NOT_written_on_error(tmp_path: Path) -> None:
    """Error path must leave no _complete marker — is_satisfied must rerun."""
    state = LifecycleState(deal_slug="d", run_id="r")
    deal_root = tmp_path / "deals" / "d"
    run_dir = deal_root / "outputs" / "r"
    run_dir.mkdir(parents=True)
    (run_dir / "raw_inputs").mkdir()

    response = BridgeResponseV1(
        status="error",
        deal_slug="d", run_id="r", agent_name="deal-intake",
        error=BridgeError(code="x", message="kaboom", recoverable=False),
    )
    step = IntakeStep()
    with patch(
        "plat_agent.lifecycle.steps.intake.dispatch_sibling_agent",
        return_value=response,
    ):
        result = step.run(state, run_dir)

    assert result.status == "error"
    assert not (run_dir / "intake" / "_complete").exists()


def test_complete_marker_written_on_blocked_with_partial_output(tmp_path: Path) -> None:
    """Blocked status with partial output: per spec §4.1, blocker means
    'partial valid output exists'. _complete IS written so cache check sees
    them — the punchlist (uncleared) is what forces the rerun under §4.4 #3."""
    state = LifecycleState(deal_slug="d", run_id="r")
    deal_root = tmp_path / "deals" / "d"
    run_dir = deal_root / "outputs" / "r"
    run_dir.mkdir(parents=True)
    (run_dir / "raw_inputs").mkdir()
    intake_dir = run_dir / "intake"
    intake_dir.mkdir()
    (intake_dir / "canonical_deal.json").write_text("{}")
    (intake_dir / "manifest.md").write_text("")
    (intake_dir / "intake_punchlist.md").write_text("")

    response = BridgeResponseV1(
        status="needs_analyst_input",
        deal_slug="d", run_id="r", agent_name="deal-intake",
        payload={
            "canonical_deal_json_relative": "outputs/r/intake/canonical_deal.json",
            "manifest_relative": "outputs/r/intake/manifest.md",
            "punchlist_relative": "outputs/r/intake/intake_punchlist.md",
            "cohorts_identified": 2,
            "documents_classified": {},
        },
        error=BridgeError(
            code="missing_input", message="missing T12", recoverable=True,
            details={"blockers": [{"id": "missing_t12", "description": "x"}]},
        ),
    )
    step = IntakeStep()
    with patch(
        "plat_agent.lifecycle.steps.intake.dispatch_sibling_agent",
        return_value=response,
    ):
        result = step.run(state, run_dir)

    assert result.status == "blocked"
    assert (intake_dir / "_complete").exists()


from plat_agent.lifecycle.state import BlockerItem


def _stage_satisfied_intake_run(tmp_path: Path) -> tuple[LifecycleState, Path]:
    """Stage a complete intake run on disk: artifacts, provenance, marker,
    state with steps_completed=['intake'], and an empty punchlist."""
    state = LifecycleState(
        deal_slug="d",
        run_id="r",
        steps_completed=["intake"],
    )
    deal_root = tmp_path / "deals" / "d"
    run_dir = deal_root / "outputs" / "r"
    run_dir.mkdir(parents=True)
    raw_inputs = run_dir / "raw_inputs"
    raw_inputs.mkdir()
    (raw_inputs / "OM.pdf").write_bytes(b"hello")
    intake_dir = run_dir / "intake"
    intake_dir.mkdir()
    (intake_dir / "canonical_deal.json").write_text("{}")
    (intake_dir / "manifest.md").write_text("")
    (intake_dir / "intake_punchlist.md").write_text("")
    write_punchlist_json(run_dir, [])

    step = IntakeStep()
    h = step._compute_input_hash(run_dir)
    step._commit_step_artifacts(run_dir, input_hash=h, status="ok")
    return state, run_dir


def test_is_satisfied_true_for_clean_completed_step(tmp_path: Path) -> None:
    state, run_dir = _stage_satisfied_intake_run(tmp_path)
    step = IntakeStep()
    assert step.is_satisfied(state, run_dir) is True


def test_is_satisfied_false_when_raw_inputs_changed(tmp_path: Path) -> None:
    state, run_dir = _stage_satisfied_intake_run(tmp_path)
    # Mutate raw inputs after the run committed → input_hash now differs
    (run_dir / "raw_inputs" / "OM.pdf").write_bytes(b"DIFFERENT")
    step = IntakeStep()
    assert step.is_satisfied(state, run_dir) is False


def test_is_satisfied_false_when_uncleared_blocker_exists(tmp_path: Path) -> None:
    state, run_dir = _stage_satisfied_intake_run(tmp_path)
    write_punchlist_json(run_dir, [
        BlockerItem(step="intake", id="missing_t12", description="x", cleared=False),
    ])
    step = IntakeStep()
    assert step.is_satisfied(state, run_dir) is False


def test_is_satisfied_false_when_step_not_in_completed(tmp_path: Path) -> None:
    state, run_dir = _stage_satisfied_intake_run(tmp_path)
    state.steps_completed = []  # never completed
    step = IntakeStep()
    assert step.is_satisfied(state, run_dir) is False


def test_is_satisfied_false_when_complete_marker_missing(tmp_path: Path) -> None:
    state, run_dir = _stage_satisfied_intake_run(tmp_path)
    (run_dir / "intake" / "_complete").unlink()
    step = IntakeStep()
    assert step.is_satisfied(state, run_dir) is False


def test_intake_accepts_real_sibling_artifact_paths(tmp_path: Path) -> None:
    """The real deal-intake.md contract puts canonical_deal.json under
    runs/deals/<slug>/standardized/ and the manifest at deal-root
    deal_manifest.md, NOT under outputs/<run>/intake/.

    Per the DealIntakeResponse contract, IntakeStep must read paths from
    the response payload and verify them under deal_root, not require a
    hardcoded run-scoped layout. Verifies fix for known-issues-v1 P1 #1.
    """
    state = LifecycleState(deal_slug="d", run_id="r")
    deal_root = tmp_path / "deals" / "d"
    run_dir = deal_root / "outputs" / "r"
    run_dir.mkdir(parents=True)
    (run_dir / "raw_inputs").mkdir()

    # Sibling-emitted artifacts at REAL paths (NOT outputs/r/intake/).
    standardized = deal_root / "standardized"
    standardized.mkdir(parents=True)
    (standardized / "canonical_deal.json").write_text(
        '{"metadata":{"address":"123 Main"},"unit_cohorts":[]}'
    )
    (deal_root / "deal_manifest.md").write_text("# Manifest\n")
    (deal_root / "intake_punchlist.md").write_text("# Punchlist\n")

    real_response = BridgeResponseV1(
        status="ok",
        deal_slug="d",
        run_id="r",
        agent_name="deal-intake",
        payload={
            "canonical_deal_json_relative": "standardized/canonical_deal.json",
            "manifest_relative": "deal_manifest.md",
            "punchlist_relative": "intake_punchlist.md",
            "cohorts_identified": 1,
            "documents_classified": {},
        },
    )

    step = IntakeStep()
    with patch(
        "plat_agent.lifecycle.steps.intake.dispatch_sibling_agent",
        return_value=real_response,
    ):
        result = step.run(state, run_dir)

    assert result.status == "ok", result.error_message
    # Mirror: downstream steps read run_dir/intake/canonical_deal.json
    assert (run_dir / "intake" / "canonical_deal.json").exists()
    assert (run_dir / "intake" / "manifest.md").exists()
    # Original artifacts at deal-root paths still exist
    assert (deal_root / "deal_manifest.md").exists()


def test_intake_errors_when_sibling_canonical_missing(tmp_path: Path) -> None:
    """If the sibling claims status=ok but the canonical_deal.json at the
    response-declared path is missing, surface as an error."""
    state = LifecycleState(deal_slug="d", run_id="r")
    deal_root = tmp_path / "deals" / "d"
    run_dir = deal_root / "outputs" / "r"
    run_dir.mkdir(parents=True)
    (run_dir / "raw_inputs").mkdir()
    # Manifest + punchlist exist; canonical does NOT.
    (deal_root / "deal_manifest.md").write_text("# Manifest\n")
    (deal_root / "intake_punchlist.md").write_text("# Punchlist\n")

    response = BridgeResponseV1(
        status="ok",
        deal_slug="d",
        run_id="r",
        agent_name="deal-intake",
        payload={
            "canonical_deal_json_relative": "standardized/canonical_deal.json",
            "manifest_relative": "deal_manifest.md",
            "punchlist_relative": "intake_punchlist.md",
            "cohorts_identified": 1,
            "documents_classified": {},
        },
    )
    step = IntakeStep()
    with patch(
        "plat_agent.lifecycle.steps.intake.dispatch_sibling_agent",
        return_value=response,
    ):
        result = step.run(state, run_dir)

    assert result.status == "error"
    assert "canonical_deal_json_relative" in (result.error_message or "")


def test_intake_step_end_to_end_happy_path(tmp_path: Path) -> None:
    """Full lifecycle of a single IntakeStep invocation.

    Verifies:
      1. _build_request is called with the right deal_slug / run_id / payload.
      2. dispatch_sibling_agent receives payload_model=DealIntakeResponse.
      3. status=ok response → StepResult.ok.
      4. punchlist.json + punchlist.md regenerated (empty for ok).
      5. _provenance.json carries input_hash + contract_version.
      6. _complete marker written with the right manifest.
      7. is_satisfied() returns True on a fresh second invocation
         (cache hit — no rerun needed).
    """
    from plat_agent.contracts.domain.intake import DealIntakeResponse
    from plat_agent.lifecycle.complete_marker import read_complete_marker

    state = LifecycleState(deal_slug="project_essex", run_id="run_002")
    deal_root = tmp_path / "deals" / "project_essex"
    run_dir = deal_root / "outputs" / "run_002"
    run_dir.mkdir(parents=True)
    raw_inputs = run_dir / "raw_inputs"
    raw_inputs.mkdir()
    (raw_inputs / "OM_Final.pdf").write_bytes(b"%PDF-1.4 om")
    (raw_inputs / "RR_2026_04.xlsx").write_bytes(b"PK\x03\x04 rent roll")
    (raw_inputs / "T12_2026.xlsx").write_bytes(b"PK\x03\x04 t12")

    intake_dir = run_dir / "intake"
    intake_dir.mkdir()
    # Simulate what the deal-intake agent would write:
    (intake_dir / "canonical_deal.json").write_text(
        '{"metadata":{"address":"123 Main"},"unit_cohorts":[]}'
    )
    (intake_dir / "manifest.md").write_text(
        "# Project Essex Manifest\n\n## Loaded\n- OM_Final.pdf\n- RR_2026_04.xlsx\n"
    )
    (intake_dir / "intake_punchlist.md").write_text(
        "# Intake punchlist\n\n- target_monthly_rent for cohorts\n"
    )

    captured = {}

    def fake_dispatch(repo, request, *, payload_model=None, **kwargs):
        captured["request"] = request
        captured["payload_model"] = payload_model
        return BridgeResponseV1(
            status="ok",
            deal_slug=request.deal_slug,
            run_id=request.run_id,
            agent_name="deal-intake",
            payload={
                "canonical_deal_json_relative": "outputs/run_002/intake/canonical_deal.json",
                "manifest_relative": "outputs/run_002/intake/manifest.md",
                "punchlist_relative": "outputs/run_002/intake/intake_punchlist.md",
                "cohorts_identified": 3,
                "documents_classified": {
                    "OM_Final.pdf": "om",
                    "RR_2026_04.xlsx": "rent_roll",
                    "T12_2026.xlsx": "t12",
                },
            },
            artifacts=[
                ArtifactRef(
                    relative_path="outputs/run_002/intake/canonical_deal.json",
                    kind="json",
                    description="Canonical deal JSON",
                ),
                ArtifactRef(
                    relative_path="outputs/run_002/intake/manifest.md",
                    kind="md",
                    description="Doc manifest",
                ),
            ],
        )

    step = IntakeStep()

    # First invocation: real run
    with patch(
        "plat_agent.lifecycle.steps.intake.dispatch_sibling_agent",
        side_effect=fake_dispatch,
    ):
        result = step.run(state, run_dir)

    # 1+2+3: assertions on the request envelope and dispatch boundary
    assert captured["payload_model"] is DealIntakeResponse
    assert captured["request"].agent_name == "deal-intake"
    assert captured["request"].deal_slug == "project_essex"
    assert captured["request"].run_id == "run_002"
    assert result.status == "ok"
    assert result.blockers == []

    # 4: punchlist artifacts written (empty)
    assert (run_dir / "punchlist.json").exists()
    assert (run_dir / "punchlist.md").exists()
    blockers = read_punchlist_json(run_dir)
    assert blockers == []

    # 5: provenance carries cache-validity inputs
    prov = json.loads((intake_dir / "_provenance.json").read_text())
    assert "input_hash" in prov
    assert prov["contract_version"] == "1.0.0"
    assert prov["status"] == "ok"

    # 6: _complete marker manifest is correct
    marker = read_complete_marker(intake_dir)
    assert marker is not None
    assert marker.step == "intake"
    assert "canonical_deal.json" in marker.file_manifest
    assert "manifest.md" in marker.file_manifest
    assert "_provenance.json" in marker.file_manifest

    # 7: is_satisfied returns True on a second pass (cache hit) once the
    # state is updated to reflect the completion.
    state.steps_completed.append("intake")
    assert step.is_satisfied(state, run_dir) is True

    # And False if the analyst re-drops a different OM (input_hash invalidates)
    (raw_inputs / "OM_Final.pdf").write_bytes(b"%PDF-1.4 NEW VERSION")
    assert step.is_satisfied(state, run_dir) is False


def test_intake_step_handles_none_payload_gracefully(tmp_path: Path) -> None:
    """V1.4 — when deal-intake returns payload=None (status='ok' or
    'needs_analyst_input'), IntakeStep MUST NOT crash. Instead it should:
      * write an empty canonical_deal.json placeholder
      * emit a single ``empty_payload_response`` blocker
      * merge the blocker into punchlist.json
      * return StepResult(status='blocked')

    Willow Court regression — pre-V1.4 this path crashed pydantic strict-
    validation in the dispatcher before the step's graceful-degradation
    branch could fire.
    """
    state = LifecycleState(deal_slug="willow_court", run_id="run_001")
    deal_root = tmp_path / "deals" / "willow_court"
    run_dir = deal_root / "outputs" / "run_001"
    run_dir.mkdir(parents=True)
    (run_dir / "raw_inputs").mkdir()

    response = BridgeResponseV1(
        status="needs_analyst_input",
        deal_slug="willow_court",
        run_id="run_001",
        agent_name="deal-intake",
        payload=None,
        error=BridgeError(
            code="om_extraction_unavailable",
            message="OM PDF unparseable; no canonical body produced.",
            recoverable=True,
        ),
    )

    step = IntakeStep()
    with patch(
        "plat_agent.lifecycle.steps.intake.dispatch_sibling_agent",
        return_value=response,
    ):
        result = step.run(state, run_dir)

    assert result.status == "blocked"
    assert len(result.blockers) == 1
    blocker = result.blockers[0]
    assert blocker.step == "intake"
    assert blocker.id == "empty_payload_response"
    # Placeholder canonical exists so downstream judgment.py won't FNF.
    canonical = run_dir / "intake" / "canonical_deal.json"
    assert canonical.exists()


def test_intake_step_none_payload_status_ok_also_blocks(tmp_path: Path) -> None:
    """The agent claiming status='ok' but payload=None should ALSO trigger
    graceful degradation (not silently produce an 'ok' StepResult).
    """
    state = LifecycleState(deal_slug="d", run_id="r")
    deal_root = tmp_path / "deals" / "d"
    run_dir = deal_root / "outputs" / "r"
    run_dir.mkdir(parents=True)
    (run_dir / "raw_inputs").mkdir()

    response = BridgeResponseV1(
        status="ok",
        deal_slug="d",
        run_id="r",
        agent_name="deal-intake",
        payload=None,
    )

    step = IntakeStep()
    with patch(
        "plat_agent.lifecycle.steps.intake.dispatch_sibling_agent",
        return_value=response,
    ):
        result = step.run(state, run_dir)

    assert result.status == "blocked"
    assert result.blockers[0].id == "empty_payload_response"
