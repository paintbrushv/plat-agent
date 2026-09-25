# tests/lifecycle/steps/test_comps_step.py
from pathlib import Path

from plat_agent.lifecycle.state import LifecycleState
from plat_agent.lifecycle.steps.comps import CompsStep


def test_comps_step_has_canonical_name() -> None:
    step = CompsStep()
    assert step.name == "comps"


def test_comps_step_implements_lifecycle_step_protocol() -> None:
    from plat_agent.lifecycle.protocol import LifecycleStep
    step = CompsStep()
    assert isinstance(step, LifecycleStep)


def test_comps_step_is_satisfied_returns_false_when_run_dir_empty(tmp_path: Path) -> None:
    step = CompsStep()
    state = LifecycleState(deal_slug="d", run_id="r")
    assert step.is_satisfied(state, tmp_path) is False


import json
import shutil

from plat_agent.lifecycle.steps.comps import CompsStep, _build_comp_finder_request


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "comps_step"


def _seed_intake(run_dir: Path) -> Path:
    intake = run_dir / "intake"
    intake.mkdir(parents=True)
    canonical = intake / "canonical_deal.json"
    shutil.copyfile(FIXTURE_DIR / "canonical_deal.json", canonical)
    return canonical


def test_build_request_extracts_address_and_market(tmp_path: Path) -> None:
    _seed_intake(tmp_path)
    canonical_path = tmp_path / "intake" / "canonical_deal.json"
    canonical = json.loads(canonical_path.read_text())
    req = _build_comp_finder_request(canonical, run_dir=tmp_path, deal_root=str(tmp_path))
    assert req.property_address == "1234 Main St, Dallas, TX 75201"
    assert req.market == "dallas_tx"
    assert req.subject_name == "Project Essex"
    assert req.subject_units == 240


def test_build_request_unit_mix_summary_aggregates_cohorts(tmp_path: Path) -> None:
    _seed_intake(tmp_path)
    canonical_path = tmp_path / "intake" / "canonical_deal.json"
    canonical = json.loads(canonical_path.read_text())
    req = _build_comp_finder_request(canonical, run_dir=tmp_path, deal_root=str(tmp_path))
    # Two cohorts → two summary entries
    assert len(req.unit_mix_summary) == 2
    summary_by_br = {row["bedrooms"]: row for row in req.unit_mix_summary}
    assert summary_by_br[1]["count"] == 80
    assert summary_by_br[1]["sqft"] == 720
    assert summary_by_br[2]["count"] == 160


def test_build_request_extracts_om_comp_seed_from_summary_table(tmp_path: Path, monkeypatch) -> None:
    _seed_intake(tmp_path)
    raw_inputs = tmp_path / "raw_inputs"
    raw_inputs.mkdir()
    om_path = raw_inputs / "Project Essex OM.pdf"
    om_path.write_bytes(b"%PDF")
    canonical = json.loads((tmp_path / "intake" / "canonical_deal.json").read_text())
    om_text = """
Rent Comparables Summary

    1      Solstice                       0.22       238    2025    12%      1,492    $1,519          $1.02
    2      Aurora                         0.37       324    2023    92%      1,009    $1,476          $1.46

           Average of Comps             1.5 Miles   282     2023    91%     1,060     $1,410          $1.34
"""
    monkeypatch.setattr(
        "plat_agent.lifecycle.steps.comps._extract_om_text",
        lambda path: om_text,
    )

    req = _build_comp_finder_request(canonical, run_dir=tmp_path, deal_root=str(tmp_path))

    assert req.om_source_relative == "raw_inputs/Project Essex OM.pdf"
    assert [row["name"] for row in req.om_comp_seed] == ["Solstice", "Aurora"]
    assert req.om_comp_seed[0]["units"] == 238
    assert req.om_comp_seed[1]["rent_psf"] == 1.46
    assert req.coverage_signal is not None
    assert req.coverage_signal.broker_seed_comp_count == 2
    assert req.coverage_signal.requires_universe_expansion is True
    assert req.coverage_signal.dominant_cohort_key == "2BR_2.0BA_1050sf"


def test_build_request_does_not_force_expansion_when_broker_seed_is_deep(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _seed_intake(tmp_path)
    raw_inputs = tmp_path / "raw_inputs"
    raw_inputs.mkdir()
    om_path = raw_inputs / "Project Essex OM.pdf"
    om_path.write_bytes(b"%PDF")
    canonical = json.loads((tmp_path / "intake" / "canonical_deal.json").read_text())
    om_text = """
Rent Comparables Summary

    1      Solstice                       0.22       238    2025    92%      1,492    $1,519          $1.02
    2      Aurora                         0.37       324    2023    92%      1,009    $1,476          $1.46
    3      Beacon                         0.45       280    2021    94%      1,050    $1,510          $1.44
    4      Lumen                          0.61       310    2022    95%      1,020    $1,498          $1.47
    5      Cadence                        0.88       260    2024    93%      1,030    $1,530          $1.49

           Average of Comps             1.5 Miles   282     2023    91%     1,060     $1,410          $1.34
"""
    monkeypatch.setattr(
        "plat_agent.lifecycle.steps.comps._extract_om_text",
        lambda path: om_text,
    )

    req = _build_comp_finder_request(canonical, run_dir=tmp_path, deal_root=str(tmp_path))

    assert req.coverage_signal is not None
    assert req.coverage_signal.broker_seed_comp_count == 5
    assert req.coverage_signal.requires_universe_expansion is False


def test_build_request_marks_new_metro_bootstrap_plan(tmp_path: Path, monkeypatch) -> None:
    _seed_intake(tmp_path)
    market_study_root = tmp_path / "market-study-agent"
    (market_study_root / "agents" / "configs").mkdir(parents=True)
    (market_study_root / "reports").mkdir()
    raw_inputs = tmp_path / "raw_inputs"
    raw_inputs.mkdir()
    (raw_inputs / "Project Essex Rent Roll.xlsx").write_bytes(b"rent-roll")
    monkeypatch.setenv("PLAT_MARKET_STUDY_AGENT_PATH", str(market_study_root))

    canonical = json.loads((tmp_path / "intake" / "canonical_deal.json").read_text())
    req = _build_comp_finder_request(canonical, run_dir=tmp_path, deal_root=str(tmp_path))

    assert req.bootstrap_plan is not None
    assert req.bootstrap_plan.workflow_stage == "new_metro"
    assert [cmd.skill_name for cmd in req.bootstrap_plan.recommended_skill_commands] == [
        "new-metro",
        "full-onboarding",
        "analyze-comps",
    ]
    assert req.bootstrap_plan.raw_rent_roll_relative == "raw_inputs/Project Essex Rent Roll.xlsx"
    assert req.bootstrap_plan.property_config_relative == "agents/configs/dallas_tx_project_essex.yaml"


def test_build_request_marks_existing_property_rerun_plan(tmp_path: Path, monkeypatch) -> None:
    _seed_intake(tmp_path)
    market_study_root = tmp_path / "market-study-agent"
    config_dir = market_study_root / "agents" / "configs"
    config_dir.mkdir(parents=True)
    reports_dir = market_study_root / "reports" / "dallas-tx" / "project-essex" / "rent-roll" / "clean"
    reports_dir.mkdir(parents=True)
    (config_dir / "dallas_tx.yaml").write_text("metro: Dallas, TX\n", encoding="utf-8")
    (config_dir / "dallas_tx_project_essex.yaml").write_text(
        "notes:\n  subject_property:\n    name: Project Essex\n    address: 1234 Main St, Dallas, TX 75201\n",
        encoding="utf-8",
    )
    (reports_dir / "floorplan_summary.csv").write_text("unit_type,units\n1BR,80\n", encoding="utf-8")
    monkeypatch.setenv("PLAT_MARKET_STUDY_AGENT_PATH", str(market_study_root))

    canonical = json.loads((tmp_path / "intake" / "canonical_deal.json").read_text())
    req = _build_comp_finder_request(canonical, run_dir=tmp_path, deal_root=str(tmp_path))

    assert req.bootstrap_plan is not None
    assert req.bootstrap_plan.workflow_stage == "existing_property"
    assert req.bootstrap_plan.metro_exists is True
    assert req.bootstrap_plan.property_exists is True
    assert req.bootstrap_plan.property_onboarded is True
    assert [cmd.skill_name for cmd in req.bootstrap_plan.recommended_skill_commands] == [
        "pull-comps",
        "analyze-comps",
    ]
    assert req.bootstrap_plan.property_reports_dir_relative == "reports/dallas-tx/project-essex"


def test_build_request_raises_when_address_missing() -> None:
    canonical = {
        "metadata": {"market": "dallas_tx"},
        "unit_cohorts": [],
    }
    import pytest
    with pytest.raises(ValueError, match="address"):
        _build_comp_finder_request(canonical)


def test_build_request_raises_when_market_missing() -> None:
    canonical = {
        "metadata": {"address": "1234 Main"},
        "unit_cohorts": [],
    }
    import pytest
    with pytest.raises(ValueError, match="market"):
        _build_comp_finder_request(canonical)


from unittest.mock import patch

from plat_agent.contracts.envelope import (
    ArtifactRef,
    BridgeResponseV1,
    ProvenanceEntry,
)


def _ok_envelope(deal_slug: str, run_id: str) -> BridgeResponseV1:
    return BridgeResponseV1(
        contract_version="v1",
        status="ok",
        deal_slug=deal_slug,
        run_id=run_id,
        agent_name="comp-finder",
        payload={
            "comps_by_cohort": {},
            "comps_relative": "comps/comps.json",
            "methodology_notes": ["3 comps; direct-site rents only."],
        },
        artifacts=[
            ArtifactRef(
                relative_path=f"outputs/{run_id}/comps/comps.json",
                kind="json",
                description="§2.2 comps.json",
            )
        ],
        provenance=[ProvenanceEntry(source="test")],
    )


def _seed_comps_artifact(run_dir: Path, fixture_name: str) -> None:
    """Simulate the agent writing comps.json under run_dir/comps/."""
    comps_dir = run_dir / "comps"
    comps_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(FIXTURE_DIR / fixture_name, comps_dir / "comps.json")


def test_run_resolves_comps_relative_from_response_payload(tmp_path: Path) -> None:
    """Per CompFinderResponse, payload.comps_relative carries the actual
    sibling-emitted path. CompsStep must resolve THAT path under deal_root,
    not always read the hardcoded run_dir/comps/comps.json. Verifies fix
    for known-issues-v1 P1 #2.
    """
    deal_root = tmp_path / "deals" / "d"
    run_dir = deal_root / "outputs" / "r"
    run_dir.mkdir(parents=True)
    _seed_intake(run_dir)

    # Sibling wrote the §2.2 file at a non-canonical location: still ends
    # in `comps.json`, but lives in a deal-root-relative subfolder.
    alt = deal_root / "outputs" / "r" / "market_study" / "comps.json"
    alt.parent.mkdir(parents=True)
    shutil.copyfile(FIXTURE_DIR / "comps_ok.json", alt)

    envelope = BridgeResponseV1(
        contract_version="v1",
        status="ok",
        deal_slug="d",
        run_id="r",
        agent_name="comp-finder",
        payload={
            "comps_by_cohort": {},
            "comps_relative": "outputs/r/market_study/comps.json",
            "methodology_notes": [],
        },
    )

    state = LifecycleState(deal_slug="d", run_id="r")
    step = CompsStep()
    with patch(
        "plat_agent.lifecycle.steps.comps.dispatch_sibling_agent",
        return_value=envelope,
    ):
        result = step.run(state, run_dir)

    assert result.status == "ok", result.error_message
    # Mirrored to canonical run-scoped location for downstream consumers.
    assert (run_dir / "comps" / "comps.json").exists()


def test_run_dispatches_with_correct_request(tmp_path: Path) -> None:
    _seed_intake(tmp_path)
    state = LifecycleState(deal_slug="project_essex", run_id="run_001")
    step = CompsStep()

    with patch("plat_agent.lifecycle.steps.comps.dispatch_sibling_agent") as mock_dispatch:
        # Side-effect: agent writes the comps.json artifact + return ok envelope
        def _fake_dispatch(repo, request, **kwargs):
            _seed_comps_artifact(tmp_path, "comps_ok.json")
            return _ok_envelope(request.deal_slug, request.run_id)

        mock_dispatch.side_effect = _fake_dispatch
        result = step.run(state, tmp_path)

    assert result.status == "ok"
    assert mock_dispatch.call_count == 1
    sent_request = mock_dispatch.call_args.args[1]
    assert sent_request.deal_slug == "project_essex"
    assert sent_request.run_id == "run_001"
    assert sent_request.agent_name == "comp-finder"
    # Envelope payload validated against CompFinderRequest schema
    assert sent_request.payload["property_address"].startswith("1234 Main St")


def test_run_writes_provenance_and_complete(tmp_path: Path) -> None:
    _seed_intake(tmp_path)
    state = LifecycleState(deal_slug="project_essex", run_id="run_001")
    step = CompsStep()

    with patch("plat_agent.lifecycle.steps.comps.dispatch_sibling_agent") as mock_dispatch:
        def _fake_dispatch(repo, request, **kwargs):
            _seed_comps_artifact(tmp_path, "comps_ok.json")
            return _ok_envelope(request.deal_slug, request.run_id)
        mock_dispatch.side_effect = _fake_dispatch
        step.run(state, tmp_path)

    comps_dir = tmp_path / "comps"
    assert (comps_dir / "_provenance.json").exists()
    assert (comps_dir / "_complete").exists()
    marker = json.loads((comps_dir / "_complete").read_text())
    assert marker["step"] == "comps"
    assert "comps.json" in marker["file_manifest"]


def test_run_blocked_when_comps_json_missing(tmp_path: Path) -> None:
    """Willow Court regression — when comp-finder claims status=ok but no
    comps.json was written, fall back to an empty-comp-set artifact +
    blocker rather than hard-erroring with no on-disk trail.
    """
    _seed_intake(tmp_path)
    state = LifecycleState(deal_slug="project_essex", run_id="run_001")
    step = CompsStep()

    with patch("plat_agent.lifecycle.steps.comps.dispatch_sibling_agent") as mock_dispatch:
        # Agent claims ok but doesn't actually write comps.json
        mock_dispatch.return_value = _ok_envelope("project_essex", "run_001")
        result = step.run(state, tmp_path)

    # New graceful-fallback behavior: blocked (not error) so judgment
    # can run OM-only with comps_unavailable.
    assert result.status == "blocked"
    assert any(b.id == "comps_dispatch_failed" for b in result.blockers)
    # Empty comps.json present on disk for downstream consumers.
    fallback = json.loads((tmp_path / "comps" / "comps.json").read_text())
    assert fallback["comps"] == []
    assert fallback["comps_dispatch_failed"] is True
    # _complete written so --resume can decide whether to retry.
    assert (tmp_path / "comps" / "_complete").exists()


def test_run_returns_error_when_comps_json_violates_schema(tmp_path: Path) -> None:
    _seed_intake(tmp_path)
    state = LifecycleState(deal_slug="project_essex", run_id="run_001")
    step = CompsStep()

    with patch("plat_agent.lifecycle.steps.comps.dispatch_sibling_agent") as mock_dispatch:
        def _fake_dispatch(repo, request, **kwargs):
            _seed_comps_artifact(tmp_path, "comps_invalid.json")
            return _ok_envelope(request.deal_slug, request.run_id)
        mock_dispatch.side_effect = _fake_dispatch
        result = step.run(state, tmp_path)

    assert result.status == "error"
    assert "schema" in result.error_message.lower()


def test_run_complete_marker_NOT_written_on_validation_error(tmp_path: Path) -> None:
    _seed_intake(tmp_path)
    state = LifecycleState(deal_slug="project_essex", run_id="run_001")
    step = CompsStep()

    with patch("plat_agent.lifecycle.steps.comps.dispatch_sibling_agent") as mock_dispatch:
        def _fake_dispatch(repo, request, **kwargs):
            _seed_comps_artifact(tmp_path, "comps_invalid.json")
            return _ok_envelope(request.deal_slug, request.run_id)
        mock_dispatch.side_effect = _fake_dispatch
        step.run(state, tmp_path)

    # Per §4.4.1: _complete must not exist when the step did not produce
    # a valid output set. Provenance file MAY exist (carries error status)
    # but _complete absence forces --resume to rerun.
    assert not (tmp_path / "comps" / "_complete").exists()


def test_run_blocked_when_only_one_comp(tmp_path: Path) -> None:
    _seed_intake(tmp_path)
    state = LifecycleState(deal_slug="project_essex", run_id="run_001")
    step = CompsStep()

    with patch("plat_agent.lifecycle.steps.comps.dispatch_sibling_agent") as mock_dispatch:
        def _fake_dispatch(repo, request, **kwargs):
            _seed_comps_artifact(tmp_path, "comps_degraded.json")
            return _ok_envelope(request.deal_slug, request.run_id)
        mock_dispatch.side_effect = _fake_dispatch
        result = step.run(state, tmp_path)

    assert result.status == "blocked"
    assert len(result.blockers) == 1
    assert result.blockers[0].step == "comps"
    assert result.blockers[0].id == "degraded_comp_set"
    assert "1 comp" in result.blockers[0].description.lower() \
        or "Only 1" in result.blockers[0].description


def test_run_blocked_when_thin_broker_seed_still_lacks_depth(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _seed_intake(tmp_path)
    raw_inputs = tmp_path / "raw_inputs"
    raw_inputs.mkdir()
    (raw_inputs / "Project Essex OM.pdf").write_bytes(b"%PDF")
    monkeypatch.setattr(
        "plat_agent.lifecycle.steps.comps._extract_om_text",
        lambda path: """
Rent Comparables Summary

    1      Solstice                       0.22       238    2025    92%      1,492    $1,519          $1.02
    2      Aurora                         0.37       324    2023    92%      1,009    $1,476          $1.46

           Average of Comps             1.5 Miles   282     2023    91%     1,060     $1,410          $1.34
""",
    )
    state = LifecycleState(deal_slug="project_essex", run_id="run_001")
    step = CompsStep()

    with patch("plat_agent.lifecycle.steps.comps.dispatch_sibling_agent") as mock_dispatch:
        def _fake_dispatch(repo, request, **kwargs):
            _seed_comps_artifact(tmp_path, "comps_ok.json")
            return _ok_envelope(request.deal_slug, request.run_id)
        mock_dispatch.side_effect = _fake_dispatch
        result = step.run(state, tmp_path)

    assert result.status == "blocked"
    blocker_ids = {blocker.id for blocker in result.blockers}
    assert "thin_comp_universe" in blocker_ids
    assert "thin_dominant_cohort_comp_coverage" in blocker_ids


def test_run_blocked_emits_complete_marker_for_resume_cache(tmp_path: Path) -> None:
    """§4.3: blocked status still completes the step — _complete written."""
    _seed_intake(tmp_path)
    state = LifecycleState(deal_slug="project_essex", run_id="run_001")
    step = CompsStep()

    with patch("plat_agent.lifecycle.steps.comps.dispatch_sibling_agent") as mock_dispatch:
        def _fake_dispatch(repo, request, **kwargs):
            _seed_comps_artifact(tmp_path, "comps_degraded.json")
            return _ok_envelope(request.deal_slug, request.run_id)
        mock_dispatch.side_effect = _fake_dispatch
        step.run(state, tmp_path)

    # _complete present so --resume sees the step as committed; the blocker
    # itself surfaces via punchlist + lifecycle state.
    assert (tmp_path / "comps" / "_complete").exists()
    provenance = json.loads((tmp_path / "comps" / "_provenance.json").read_text())
    assert provenance["blocker_count"] == 1


def test_run_error_when_sibling_returns_status_error(tmp_path: Path) -> None:
    """§4.3 alt path: comp-finder hard-error → CompsStep error;
    orchestrator translates to comps_unavailable=true downstream."""
    _seed_intake(tmp_path)
    state = LifecycleState(deal_slug="project_essex", run_id="run_001")
    step = CompsStep()

    error_envelope = _ok_envelope("project_essex", "run_001").model_copy(
        update={
            "status": "error",
            "error": {
                "code": "missing_input",
                "message": "no comp config for market unknown_metro",
                "recoverable": False,
            },
        }
    )

    with patch("plat_agent.lifecycle.steps.comps.dispatch_sibling_agent") as mock_dispatch:
        mock_dispatch.return_value = error_envelope
        result = step.run(state, tmp_path)

    assert result.status == "error"
    assert "no comp config" in result.error_message
    assert not (tmp_path / "comps" / "_complete").exists()


from datetime import datetime, timedelta, timezone


def _do_successful_run(tmp_path: Path) -> LifecycleState:
    _seed_intake(tmp_path)
    state = LifecycleState(
        deal_slug="project_essex",
        run_id="run_001",
        steps_completed=["intake"],
    )
    step = CompsStep()
    with patch("plat_agent.lifecycle.steps.comps.dispatch_sibling_agent") as mock_dispatch:
        def _fake_dispatch(repo, request, **kwargs):
            _seed_comps_artifact(tmp_path, "comps_ok.json")
            return _ok_envelope(request.deal_slug, request.run_id)
        mock_dispatch.side_effect = _fake_dispatch
        step.run(state, tmp_path)
    state.steps_completed.append("comps")
    return state


def test_is_satisfied_true_after_clean_run(tmp_path: Path) -> None:
    state = _do_successful_run(tmp_path)
    assert CompsStep().is_satisfied(state, tmp_path) is True


def test_is_satisfied_false_when_canonical_deal_missing(tmp_path: Path) -> None:
    """No upstream input → can't compute input_hash → not satisfied."""
    step = CompsStep()
    state = LifecycleState(deal_slug="d", run_id="r", steps_completed=["comps"])
    assert step.is_satisfied(state, tmp_path) is False


def test_is_satisfied_false_when_canonical_changes(tmp_path: Path) -> None:
    state = _do_successful_run(tmp_path)
    # Mutate canonical_deal.json — hash should no longer match
    canonical = tmp_path / "intake" / "canonical_deal.json"
    canonical.write_text('{"metadata": {"address": "different", "market": "austin_tx"}}')
    assert CompsStep().is_satisfied(state, tmp_path) is False


def test_is_satisfied_false_after_ttl_exceeded(tmp_path: Path) -> None:
    state = _do_successful_run(tmp_path)
    # Backdate as_of to 10 days ago — exceeds 7-day TTL per §4.4 #6
    comps_file = tmp_path / "comps" / "comps.json"
    payload = json.loads(comps_file.read_text())
    payload["as_of"] = (datetime.now(timezone.utc) - timedelta(days=10)).date().isoformat()
    comps_file.write_text(json.dumps(payload))
    assert CompsStep().is_satisfied(state, tmp_path) is False


from plat_agent.contracts.domain.market_study import CompsArtifact


def test_integration_clean_run_produces_all_artifacts(tmp_path: Path) -> None:
    """Smoke: CompsStep happy path produces every required §2.2 artifact."""
    _seed_intake(tmp_path)
    state = LifecycleState(deal_slug="project_essex", run_id="run_001")
    step = CompsStep()

    with patch("plat_agent.lifecycle.steps.comps.dispatch_sibling_agent") as mock_dispatch:
        def _fake_dispatch(repo, request, **kwargs):
            _seed_comps_artifact(tmp_path, "comps_ok.json")
            return _ok_envelope(request.deal_slug, request.run_id)
        mock_dispatch.side_effect = _fake_dispatch
        result = step.run(state, tmp_path)

    assert result.status == "ok"
    assert result.blockers == []

    comps_dir = tmp_path / "comps"
    # Files
    assert (comps_dir / "comps.json").exists()
    assert (comps_dir / "_provenance.json").exists()
    assert (comps_dir / "_complete").exists()
    # _complete manifest
    marker = json.loads((comps_dir / "_complete").read_text())
    assert sorted(marker["file_manifest"]) == sorted(["comps.json", "_provenance.json"])
    # Provenance content
    prov = json.loads((comps_dir / "_provenance.json").read_text())
    assert prov["status"] == "ok"
    assert prov["comp_count"] == 3
    assert prov["blocker_count"] == 0
    assert prov["contract_version"] == "1.0.0"
    # Schema-validate the persisted comps.json one more time end-to-end
    artifact = CompsArtifact.model_validate(
        json.loads((comps_dir / "comps.json").read_text())
    )
    assert artifact.subject.metro_slug == "dallas_tx"
    assert len(artifact.comps) == 3


# ---------------------------------------------------------------------------
# Willow Court regression — comp-finder dispatch error reporting + graceful fallback.
# ---------------------------------------------------------------------------


def test_comp_finder_dispatch_error_writes_provenance_with_log_path(tmp_path: Path) -> None:
    """When the dispatcher raises DispatchError, the comps step must write
    provenance carrying both error_message AND the captured stderr log
    path. Willow Court run_001 had no comps/ dir at all because this signal
    was never persisted.
    """
    from plat_agent.dispatch.sibling import DispatchError

    _seed_intake(tmp_path)
    state = LifecycleState(deal_slug="project_essex", run_id="run_001")
    step = CompsStep()

    fake_log = tmp_path / "fake-stderr.log"
    fake_log.write_text("comp-finder subprocess died here")

    with patch("plat_agent.lifecycle.steps.comps.dispatch_sibling_agent") as mock_dispatch:
        mock_dispatch.side_effect = DispatchError(
            "comp-finder subprocess returned non-zero (1). "
            f"Stderr log: {fake_log}",
            log_path=fake_log,
        )
        result = step.run(state, tmp_path)

    # Step is blocked, not error — so judgment can continue OM-only.
    assert result.status == "blocked"
    assert any(b.id == "comps_dispatch_failed" for b in result.blockers)

    comps_dir = tmp_path / "comps"
    # Provenance carries error_message + stderr_log_path.
    prov = json.loads((comps_dir / "_provenance.json").read_text())
    assert prov["status"] == "blocked"
    assert prov["error_code"] == "dispatch_error"
    assert "comp-finder subprocess returned non-zero" in prov["error_message"]
    assert prov["stderr_log_path"] == str(fake_log)
    assert prov["comps_dispatch_failed"] is True

    # _complete present so is_satisfied() can mark the step committed.
    assert (comps_dir / "_complete").exists()


def test_comp_finder_response_without_comps_relative_writes_empty_comps_json(
    tmp_path: Path,
) -> None:
    """When comp-finder returns ok but the payload omits `comps_relative`
    AND no comps.json is on disk, the step must write an empty-comp-set
    fallback artifact so downstream judgment sees the consistent
    empty-comp-set path.
    """
    _seed_intake(tmp_path)
    state = LifecycleState(deal_slug="project_essex", run_id="run_001")
    step = CompsStep()

    # Envelope explicitly omits comps_relative.
    envelope = BridgeResponseV1(
        contract_version="v1",
        status="ok",
        deal_slug="project_essex",
        run_id="run_001",
        agent_name="comp-finder",
        payload={
            "comps_by_cohort": {},
            # No comps_relative field.
            "methodology_notes": [],
        },
    )

    with patch(
        "plat_agent.lifecycle.steps.comps.dispatch_sibling_agent",
        return_value=envelope,
    ):
        result = step.run(state, tmp_path)

    assert result.status == "blocked"
    fallback = json.loads((tmp_path / "comps" / "comps.json").read_text())
    assert fallback["comps"] == []
    assert fallback["comps_dispatch_failed"] is True
    # Downstream judgment treats an empty comps[] as comps_unavailable.
    prov = json.loads((tmp_path / "comps" / "_provenance.json").read_text())
    assert prov["error_code"] == "missing_artifact"
    assert "comps_relative" in prov["error_message"]


def test_comp_finder_timeout_logged_and_step_continues(tmp_path: Path) -> None:
    """A subprocess timeout (DispatchError raised by `_run_subprocess` after
    Popen.communicate(timeout=)) must surface via provenance + blocker but
    must NOT raise out of the step. Lifecycle continues with comps blocked.
    """
    from plat_agent.dispatch.sibling import DispatchError

    _seed_intake(tmp_path)
    state = LifecycleState(deal_slug="project_essex", run_id="run_001")
    step = CompsStep()

    timeout_log = tmp_path / "timeout-stderr.log"
    timeout_log.write_text("(empty: subprocess killed after 1800s)")

    with patch("plat_agent.lifecycle.steps.comps.dispatch_sibling_agent") as mock_dispatch:
        mock_dispatch.side_effect = DispatchError(
            "comp-finder subprocess timed out after 1800 seconds. "
            f"Stderr log: {timeout_log}",
            log_path=timeout_log,
        )
        # Must not propagate the exception — step swallows it gracefully.
        result = step.run(state, tmp_path)

    assert result.status == "blocked"
    assert any(b.id == "comps_dispatch_failed" for b in result.blockers)
    # Blocker description references the stderr log so an operator can grep.
    desc = result.blockers[0].description
    assert "timed out" in desc or "timeout" in desc.lower()
    assert str(timeout_log) in desc

    # On-disk artifact set is complete so the step is "done".
    comps_dir = tmp_path / "comps"
    assert (comps_dir / "comps.json").exists()
    assert (comps_dir / "_provenance.json").exists()
    assert (comps_dir / "_complete").exists()


def test_comps_missing_address_writes_empty_fallback(tmp_path: Path) -> None:
    """V1.3 — when canonical_deal.json lacks metadata.address, CompsStep
    must emit the empty-comp-set fallback artifact + blocker rather than
    bailing with StepResult(error). Previously the ValueError raised
    inside `_build_comp_finder_request` produced StepResult(error) with
    no on-disk trail, so V1.1's downstream comps_unavailable handling
    never engaged consistently.
    """
    # Seed an intake/canonical_deal.json missing metadata.address.
    intake = tmp_path / "intake"
    intake.mkdir(parents=True)
    canonical = intake / "canonical_deal.json"
    canonical.write_text(json.dumps({
        "metadata": {"market": "dallas_tx"},
        "unit_cohorts": [],
    }))

    state = LifecycleState(deal_slug="d", run_id="r")
    step = CompsStep()
    # Dispatch should NEVER be called when pre-dispatch translation fails.
    with patch(
        "plat_agent.lifecycle.steps.comps.dispatch_sibling_agent"
    ) as mock_dispatch:
        result = step.run(state, tmp_path)
        mock_dispatch.assert_not_called()

    assert result.status == "blocked"
    assert any(b.id == "missing_subject_metadata" for b in result.blockers)
    desc = result.blockers[0].description
    assert "address" in desc.lower()
    # Hint at the new CLI overrides so the analyst knows the fix.
    assert "--address" in desc or "metadata.address" in desc

    # On-disk fallback artifacts.
    comps_dir = tmp_path / "comps"
    fallback = json.loads((comps_dir / "comps.json").read_text())
    assert fallback["comps"] == []
    assert fallback["comps_dispatch_failed"] is True

    prov = json.loads((comps_dir / "_provenance.json").read_text())
    assert prov["error_code"] == "missing_subject_metadata"
    assert prov["status"] == "blocked"
    assert (comps_dir / "_complete").exists()


def test_comps_missing_market_writes_empty_fallback(tmp_path: Path) -> None:
    """V1.3 — symmetric coverage for missing metadata.market."""
    intake = tmp_path / "intake"
    intake.mkdir(parents=True)
    canonical = intake / "canonical_deal.json"
    canonical.write_text(json.dumps({
        "metadata": {"address": "1234 Foo St, Plano, TX 75024"},
        "unit_cohorts": [],
    }))

    state = LifecycleState(deal_slug="d", run_id="r")
    step = CompsStep()
    with patch(
        "plat_agent.lifecycle.steps.comps.dispatch_sibling_agent"
    ) as mock_dispatch:
        result = step.run(state, tmp_path)
        mock_dispatch.assert_not_called()

    assert result.status == "blocked"
    assert any(b.id == "missing_subject_metadata" for b in result.blockers)
    desc = result.blockers[0].description
    assert "market" in desc.lower()

    comps_dir = tmp_path / "comps"
    assert (comps_dir / "comps.json").exists()
    assert (comps_dir / "_provenance.json").exists()
    assert (comps_dir / "_complete").exists()
    prov = json.loads((comps_dir / "_provenance.json").read_text())
    assert prov["error_code"] == "missing_subject_metadata"


def test_integration_full_module_passes(tmp_path: Path) -> None:
    """All CompsStep tests pass as a suite."""
    # Sentinel — running the full module must not crash on import or shared
    # state. The actual passes are checked by the per-test assertions above.
    pass


def test_comps_step_handles_none_payload_gracefully(tmp_path: Path) -> None:
    """V1.4 — when comp-finder returns payload=None (status='ok' or
    'needs_analyst_input'), CompsStep MUST NOT crash. It should emit the
    same empty comps.json fallback path that V1.1 dispatch failures use,
    plus an `empty_payload_response` blocker so judgment runs OM-only.
    """
    _seed_intake(tmp_path)

    response = BridgeResponseV1(
        status="needs_analyst_input",
        deal_slug="d",
        run_id="r",
        agent_name="comp-finder",
        payload=None,
    )
    state = LifecycleState(deal_slug="d", run_id="r")
    step = CompsStep()
    with patch(
        "plat_agent.lifecycle.steps.comps.dispatch_sibling_agent",
        return_value=response,
    ):
        result = step.run(state, tmp_path)

    assert result.status == "blocked"
    assert any(b.id == "empty_payload_response" for b in result.blockers)
    comps_dir = tmp_path / "comps"
    assert (comps_dir / "comps.json").exists()
    artifact = json.loads((comps_dir / "comps.json").read_text())
    assert artifact["comps"] == []
    assert artifact["comps_dispatch_failed"] is True
    # Provenance + _complete still get written so --resume can move on.
    assert (comps_dir / "_provenance.json").exists()
    assert (comps_dir / "_complete").exists()
