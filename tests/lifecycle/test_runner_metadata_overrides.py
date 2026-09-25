"""V1.3 runner tests — `--address` / `--market` analyst overrides.

These overrides patch canonical_deal.json metadata after intake completes
(so the file is on disk) and before comps runs (so CompsStep sees the
patched values). The canonical patch path lives in
`runner._patch_canonical_metadata`.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

from plat_agent.lifecycle.cache import compute_input_hash
from plat_agent.lifecycle.defaults import CONTRACT_VERSION
from plat_agent.lifecycle.protocol import StepResult
from plat_agent.lifecycle.runner import run_lifecycle, _derive_deal_slug


def _make_data_room(tmp_path: Path) -> Path:
    src = tmp_path / "data_room"
    src.mkdir()
    (src / "OM.pdf").write_bytes(b"x")
    return src


def _ok_step(name: str) -> MagicMock:
    m = MagicMock()
    m.name = name
    m.run.return_value = StepResult(status="ok")
    m.is_satisfied.return_value = False
    return m


def _property_tax_policy(
    mills: float = 25.31,
    *,
    source_locator: str = "underwrite-deal:property_tax_millage",
) -> dict:
    return {
        "millage_rate_mills": mills,
        "assessment_ratio": 1.0,
        "source": "analyst",
        "source_locator": source_locator,
        "analyst_override": False,
    }


def _approved_ratio_policy(mills: float = 24.90) -> dict:
    return {
        "millage_rate_mills": mills,
        "assessment_ratio": 0.7,
        "source": "composite_evidence",
        "source_locator": (
            "millage=county_tax_notice:raw_inputs/notice.pdf, page 2;"
            "assessment_ratio=cad_rate_table:raw_inputs/CAD.pdf, page 3"
        ),
        "analyst_override": True,
    }


def _with_property_tax_policy(canonical: dict, mills: float = 25.31) -> dict:
    payload = json.loads(json.dumps(canonical))
    summary = payload.setdefault("metadata", {}).setdefault("property_summary", {})
    summary["property_tax_policy"] = _property_tax_policy(mills)
    return payload


def _stub_intake_run(canonical_seed: dict):
    """Build a side_effect that writes a canonical_deal.json on intake run.

    Mirrors the real IntakeStep behavior of dropping the canonical JSON
    into outputs/<run_id>/intake/ during run().
    """
    def run(state, run_dir):
        intake_dir = run_dir / "intake"
        intake_dir.mkdir(parents=True, exist_ok=True)
        (intake_dir / "canonical_deal.json").write_text(
            json.dumps(canonical_seed)
        )
        return StepResult(status="ok")
    return run


def _build_minimal_steps(
    intake_canonical: dict,
    *,
    include_tax_policy: bool = True,
) -> dict:
    """Six step adapters mocked just enough for run_lifecycle to terminate."""
    steps = {n: _ok_step(n) for n in [
        "intake", "comps", "judgment", "underwriting", "memo", "crm"
    ]}
    canonical = (
        _with_property_tax_policy(intake_canonical)
        if include_tax_policy
        else intake_canonical
    )
    steps["intake"].run.side_effect = _stub_intake_run(canonical)

    def stub_judgment(state, run_dir):
        jd = run_dir / "judgment"
        jd.mkdir(exist_ok=True)
        (jd / "positioning.json").write_text(json.dumps({
            "positioning": {"confidence": 0.85},
            "leverage": {"source": "v1_hardcoded"},
            "blockers": [],
        }))
        return StepResult(status="ok")
    steps["judgment"].run.side_effect = stub_judgment

    def stub_uw(state, run_dir):
        uw = run_dir / "underwriting"
        uw.mkdir(exist_ok=True)
        (uw / "deal_summary.json").write_text(json.dumps({
            "metrics": {"irr": {"levered_irr": 0.15},
                        "dscr": {"minimum_dscr": 1.25}, "coc": {"cash_on_cash_year_1": 0.08}}}))
        return StepResult(status="ok")
    steps["underwriting"].run.side_effect = stub_uw
    return steps


def test_address_override_patches_canonical_metadata(tmp_path: Path) -> None:
    """--address writes through to canonical_deal.json metadata.address
    after intake completes."""
    src = _make_data_room(tmp_path)
    project_root = tmp_path / "project_root"
    project_root.mkdir()

    # Intake produces a canonical_deal.json with NO address (analyst supplies via CLI).
    intake_canonical = {
        "metadata": {"market": "dallas_tx"},
        "unit_cohorts": [],
    }
    steps = _build_minimal_steps(intake_canonical)

    result = run_lifecycle(
        src,
        deal_slug="d",
        project_root=project_root,
        steps=steps,
        address="1234 Foo St, Plano, TX 75024",
    )

    canonical_path = (
        project_root / "runs" / "deals" / "d" / "outputs"
        / result.run_id / "intake" / "canonical_deal.json"
    )
    payload = json.loads(canonical_path.read_text())
    assert payload["metadata"]["address"] == "1234 Foo St, Plano, TX 75024"
    # The pre-existing market field is preserved.
    assert payload["metadata"]["market"] == "dallas_tx"


def test_market_override_patches_canonical_metadata(tmp_path: Path) -> None:
    """--market writes through to canonical_deal.json metadata.market."""
    src = _make_data_room(tmp_path)
    project_root = tmp_path / "project_root"
    project_root.mkdir()

    # Intake produces a canonical_deal.json with NO market.
    intake_canonical = {
        "metadata": {"address": "5678 Bar Ave, Frisco, TX 75035"},
        "unit_cohorts": [],
    }
    steps = _build_minimal_steps(intake_canonical)

    result = run_lifecycle(
        src,
        deal_slug="d2",
        project_root=project_root,
        steps=steps,
        market="DFW",
    )

    canonical_path = (
        project_root / "runs" / "deals" / "d2" / "outputs"
        / result.run_id / "intake" / "canonical_deal.json"
    )
    payload = json.loads(canonical_path.read_text())
    assert payload["metadata"]["market"] == "DFW"
    assert payload["metadata"]["address"] == "5678 Bar Ave, Frisco, TX 75035"


def test_no_override_leaves_canonical_metadata_unchanged(tmp_path: Path) -> None:
    """When no --address / --market supplied, intake's canonical_deal.json
    is not rewritten."""
    src = _make_data_room(tmp_path)
    project_root = tmp_path / "project_root"
    project_root.mkdir()

    intake_canonical = {
        "metadata": {
            "address": "100 Original St, Dallas, TX 75201",
            "market": "dallas_tx",
        },
        "unit_cohorts": [],
    }
    steps = _build_minimal_steps(intake_canonical)

    result = run_lifecycle(
        src,
        deal_slug="d3",
        project_root=project_root,
        steps=steps,
    )

    canonical_path = (
        project_root / "runs" / "deals" / "d3" / "outputs"
        / result.run_id / "intake" / "canonical_deal.json"
    )
    payload = json.loads(canonical_path.read_text())
    assert payload["metadata"]["address"] == "100 Original St, Dallas, TX 75201"
    assert payload["metadata"]["market"] == "dallas_tx"


def test_both_overrides_applied_simultaneously(tmp_path: Path) -> None:
    """Supplying both --address and --market patches both fields."""
    src = _make_data_room(tmp_path)
    project_root = tmp_path / "project_root"
    project_root.mkdir()

    intake_canonical = {
        "metadata": {},
        "unit_cohorts": [],
    }
    steps = _build_minimal_steps(intake_canonical)

    result = run_lifecycle(
        src,
        deal_slug="d4",
        project_root=project_root,
        steps=steps,
        address="9999 Combo Way, Austin, TX 78701",
        market="austin_tx",
    )

    canonical_path = (
        project_root / "runs" / "deals" / "d4" / "outputs"
        / result.run_id / "intake" / "canonical_deal.json"
    )
    payload = json.loads(canonical_path.read_text())
    assert payload["metadata"]["address"] == "9999 Combo Way, Austin, TX 78701"
    assert payload["metadata"]["market"] == "austin_tx"


def test_override_skipped_when_intake_did_not_produce_canonical(
    tmp_path: Path,
) -> None:
    """If intake hard-errored before producing canonical_deal.json, the
    patch is silently skipped (the comps step's own missing_subject_metadata
    fallback fires when canonical_deal.json shows up empty/missing)."""
    src = _make_data_room(tmp_path)
    project_root = tmp_path / "project_root"
    project_root.mkdir()

    # Intake "runs" but never writes canonical_deal.json (and reports error).
    steps = _build_minimal_steps({"metadata": {}, "unit_cohorts": []})
    def failing_intake(state, run_dir):
        # No artifacts written.
        return StepResult(status="error", error_message="boom")
    steps["intake"].run.side_effect = failing_intake

    # Should not raise even though canonical_deal.json doesn't exist.
    run_lifecycle(
        src,
        deal_slug="d5",
        project_root=project_root,
        steps=steps,
        address="anywhere",
        market="DFW",
    )


def test_override_applied_on_resume(tmp_path: Path) -> None:
    src = _make_data_room(tmp_path)
    project_root = tmp_path / "project_root"
    project_root.mkdir()

    intake_canonical = {
        "metadata": {
            "address": "100 Original St, Dallas, TX 75201",
            "market": "dallas_tx",
        },
        "unit_cohorts": [],
    }
    steps = _build_minimal_steps(intake_canonical)
    first = run_lifecycle(
        src,
        deal_slug="resume_me",
        project_root=project_root,
        steps=steps,
    )

    resume_steps = _build_minimal_steps(intake_canonical)
    for step in resume_steps.values():
        step.is_satisfied.return_value = True

    run_lifecycle(
        src,
        deal_slug="resume_me",
        project_root=project_root,
        steps=resume_steps,
        resume_run_id=first.run_id,
        address="200 Updated Ave, Plano, TX 75024",
        market="plano_tx",
    )

    canonical_path = (
        project_root / "runs" / "deals" / "resume_me" / "outputs"
        / first.run_id / "intake" / "canonical_deal.json"
    )
    payload = json.loads(canonical_path.read_text())
    assert payload["metadata"]["address"] == "200 Updated Ave, Plano, TX 75024"
    assert payload["metadata"]["market"] == "plano_tx"


def _run_dir(project_root: Path, deal_slug: str, run_id: str) -> Path:
    return (
        project_root / "runs" / "deals" / deal_slug / "outputs" / run_id
    )


def test_property_tax_cli_answer_is_persisted_before_fresh_downstream_work(
    tmp_path: Path,
) -> None:
    src = _make_data_room(tmp_path)
    project_root = tmp_path / "project_root"
    project_root.mkdir()
    steps = _build_minimal_steps(
        {"metadata": {}, "unit_cohorts": []},
        include_tax_policy=False,
    )
    seen_policy: list[dict] = []

    def comps_run(state, run_dir):
        canonical = json.loads(
            (run_dir / "intake" / "canonical_deal.json").read_text()
        )
        seen_policy.append(
            canonical["metadata"]["property_summary"]["property_tax_policy"]
        )
        return StepResult(status="ok")

    steps["comps"].run.side_effect = comps_run

    result = run_lifecycle(
        src,
        deal_slug="fresh_tax",
        project_root=project_root,
        steps=steps,
        millage_rate="25.31",
    )

    assert result.exit_code == 0
    expected = _property_tax_policy(
        source_locator="plat lifecycle:--millage-rate"
    )
    assert seen_policy == [expected]
    persisted = json.loads(
        (_run_dir(project_root, "fresh_tax", result.run_id)
         / "intake" / "canonical_deal.json").read_text()
    )
    assert (
        persisted["metadata"]["property_summary"]["property_tax_policy"]
        == expected
    )


def test_missing_or_conflicting_tax_blocks_before_judgment_and_persists_resume(
    tmp_path: Path,
) -> None:
    src = _make_data_room(tmp_path)
    project_root = tmp_path / "project_root"
    project_root.mkdir()
    canonical = {
        "metadata": {
            "property_summary": {
                "property_tax_evidence_candidates": [
                    {
                        "millage_rate_mills": 24.9,
                        "source": "offering_memorandum",
                        "source_locator": "raw_inputs/OM.pdf, page 18",
                    },
                    {
                        "millage_rate_mills": 25.31,
                        "source": "county_tax_notice",
                        "source_locator": "raw_inputs/notice.pdf, page 2",
                    },
                ]
            }
        }
    }
    steps = _build_minimal_steps(canonical, include_tax_policy=False)

    result = run_lifecycle(
        src,
        deal_slug="blocked_tax",
        project_root=project_root,
        steps=steps,
    )

    run_dir = _run_dir(project_root, "blocked_tax", result.run_id)
    state = json.loads((run_dir / "_lifecycle_state.json").read_text())
    punchlist_json = json.loads((run_dir / "punchlist.json").read_text())
    punchlist_md = (run_dir / "punchlist.md").read_text()
    exact_command = (
        f"plat lifecycle {src.resolve()} --resume "
        f"blocked_tax/{result.run_id} --millage-rate <mills>"
    )

    assert result.status == "needs_analyst_input"
    assert result.exit_code == 2
    assert state["status"] == "needs_analyst_input"
    tax_blocker = next(
        item for item in state["blockers"]
        if item["id"] == "missing_property_tax_millage"
    )
    assert tax_blocker["field"] == (
        "metadata.property_summary.property_tax_policy.millage_rate_mills"
    )
    assert tax_blocker["unit"] == "mills per $1,000 of assessed value"
    assert tax_blocker["resolution_hint"] == exact_command
    assert punchlist_json["blockers"][-1]["resolution_hint"] == exact_command
    assert exact_command in punchlist_md
    steps["judgment"].run.assert_not_called()
    steps["underwriting"].run.assert_not_called()


def test_changed_valid_millage_rewrites_before_cache_and_invalidates_downstream(
    tmp_path: Path,
) -> None:
    src = _make_data_room(tmp_path)
    project_root = tmp_path / "project_root"
    project_root.mkdir()
    initial_steps = _build_minimal_steps(
        {"metadata": {}, "unit_cohorts": []},
    )
    first = run_lifecycle(
        src,
        deal_slug="changed_tax",
        project_root=project_root,
        steps=initial_steps,
        millage_rate="24.90",
    )
    run_dir = _run_dir(project_root, "changed_tax", first.run_id)
    before = (run_dir / "intake" / "canonical_deal.json").read_bytes()
    old_comps_hash = compute_input_hash([
        run_dir / "intake" / "canonical_deal.json"
    ])
    comps_dir = run_dir / "comps"
    comps_dir.mkdir(exist_ok=True)
    (comps_dir / "_provenance.json").write_text(json.dumps({
        "status": "ok",
        "input_hash": old_comps_hash,
        "contract_version": CONTRACT_VERSION,
        "written_at": "2026-01-01T00:00:00+00:00",
    }))

    resume_steps = _build_minimal_steps(
        {"metadata": {}, "unit_cohorts": []},
    )
    cache_observations: list[tuple[str, float, list[str]]] = []
    for name, step in resume_steps.items():
        def satisfied(state, current_run_dir, *, _name=name):
            policy = json.loads(
                (current_run_dir / "intake" / "canonical_deal.json").read_text()
            )["metadata"]["property_summary"]["property_tax_policy"]
            cache_observations.append(
                (_name, policy["millage_rate_mills"], list(state.steps_completed))
            )
            return _name in state.steps_completed
        step.is_satisfied.side_effect = satisfied

    resumed = run_lifecycle(
        src,
        deal_slug="changed_tax",
        project_root=project_root,
        steps=resume_steps,
        resume_run_id=first.run_id,
        millage_rate="25.31",
    )

    after = (run_dir / "intake" / "canonical_deal.json").read_bytes()
    assert resumed.exit_code == 0
    assert before != after
    assert [item[0] for item in cache_observations[:3]] == [
        "intake", "comps", "judgment",
    ]
    assert all(item[1] == 25.31 for item in cache_observations[:3])
    assert all(
        item[2] == ["intake", "comps"]
        for item in cache_observations[:3]
    )
    resume_steps["intake"].is_satisfied.assert_called_once()
    resume_steps["comps"].is_satisfied.assert_called_once()
    resume_steps["intake"].run.assert_not_called()
    resume_steps["comps"].run.assert_not_called()
    resume_steps["judgment"].run.assert_called_once()
    resume_steps["underwriting"].run.assert_called_once()
    state = json.loads((run_dir / "_lifecycle_state.json").read_text())
    assert state["steps_completed"] == [
        "intake", "comps", "judgment", "underwriting", "memo", "crm",
    ]
    rebased = json.loads((comps_dir / "_provenance.json").read_text())
    assert rebased["input_hash"] == compute_input_hash([
        run_dir / "intake" / "canonical_deal.json"
    ])
    assert rebased["written_at"] == "2026-01-01T00:00:00+00:00"


def test_tax_change_still_honors_stale_comps_cache_check(
    tmp_path: Path,
) -> None:
    src = _make_data_room(tmp_path)
    project_root = tmp_path / "project_root"
    project_root.mkdir()
    first = run_lifecycle(
        src,
        deal_slug="stale_tax_cache",
        project_root=project_root,
        steps=_build_minimal_steps({"metadata": {}, "unit_cohorts": []}),
        millage_rate="24.90",
    )
    resume_steps = _build_minimal_steps(
        {"metadata": {}, "unit_cohorts": []},
    )
    resume_steps["intake"].is_satisfied.return_value = True
    resume_steps["comps"].is_satisfied.return_value = False
    for name in ("judgment", "underwriting", "memo", "crm"):
        resume_steps[name].is_satisfied.side_effect = (
            lambda state, run_dir, _name=name: _name in state.steps_completed
        )

    run_lifecycle(
        src,
        deal_slug="stale_tax_cache",
        project_root=project_root,
        steps=resume_steps,
        resume_run_id=first.run_id,
        millage_rate="25.31",
    )

    resume_steps["intake"].is_satisfied.assert_called_once()
    resume_steps["comps"].is_satisfied.assert_called_once()
    resume_steps["comps"].run.assert_called_once()
    resume_steps["judgment"].run.assert_called_once()


def test_simultaneous_address_and_tax_change_does_not_rebase_comps_provenance(
    tmp_path: Path,
) -> None:
    src = _make_data_room(tmp_path)
    project_root = tmp_path / "project_root"
    project_root.mkdir()
    first = run_lifecycle(
        src,
        deal_slug="address_and_tax",
        project_root=project_root,
        steps=_build_minimal_steps({
            "metadata": {"address": "100 Old St"},
            "unit_cohorts": [],
        }),
        millage_rate="24.90",
    )
    run_dir = _run_dir(project_root, "address_and_tax", first.run_id)
    old_comps_hash = compute_input_hash([
        run_dir / "intake" / "canonical_deal.json"
    ])
    comps_dir = run_dir / "comps"
    comps_dir.mkdir(exist_ok=True)
    (comps_dir / "_provenance.json").write_text(json.dumps({
        "status": "ok",
        "input_hash": old_comps_hash,
        "contract_version": CONTRACT_VERSION,
        "written_at": "2026-01-01T00:00:00+00:00",
    }))
    resume_steps = _build_minimal_steps(
        {"metadata": {}, "unit_cohorts": []},
    )
    resume_steps["intake"].is_satisfied.return_value = True
    resume_steps["comps"].is_satisfied.return_value = False
    for name in ("judgment", "underwriting", "memo", "crm"):
        resume_steps[name].is_satisfied.side_effect = (
            lambda state, run_dir, _name=name: _name in state.steps_completed
        )

    run_lifecycle(
        src,
        deal_slug="address_and_tax",
        project_root=project_root,
        steps=resume_steps,
        resume_run_id=first.run_id,
        address="200 New Ave",
        millage_rate="25.31",
    )

    provenance = json.loads((comps_dir / "_provenance.json").read_text())
    assert provenance["input_hash"] == old_comps_hash
    resume_steps["comps"].is_satisfied.assert_called_once()
    resume_steps["comps"].run.assert_called_once()


def test_identical_millage_keeps_canonical_bytes_and_completed_steps(
    tmp_path: Path,
) -> None:
    src = _make_data_room(tmp_path)
    project_root = tmp_path / "project_root"
    project_root.mkdir()
    initial_steps = _build_minimal_steps(
        {"metadata": {}, "unit_cohorts": []},
    )
    first = run_lifecycle(
        src,
        deal_slug="same_tax",
        project_root=project_root,
        steps=initial_steps,
        millage_rate="25.31",
    )
    run_dir = _run_dir(project_root, "same_tax", first.run_id)
    before_bytes = (run_dir / "intake" / "canonical_deal.json").read_bytes()
    before_completed = json.loads(
        (run_dir / "_lifecycle_state.json").read_text()
    )["steps_completed"]

    resume_steps = _build_minimal_steps(
        {"metadata": {}, "unit_cohorts": []},
    )
    for name, step in resume_steps.items():
        step.is_satisfied.side_effect = (
            lambda state, run_dir, _name=name: _name in state.steps_completed
        )

    run_lifecycle(
        src,
        deal_slug="same_tax",
        project_root=project_root,
        steps=resume_steps,
        resume_run_id=first.run_id,
        millage_rate="25.310",
    )

    assert (
        run_dir / "intake" / "canonical_deal.json"
    ).read_bytes() == before_bytes
    assert json.loads(
        (run_dir / "_lifecycle_state.json").read_text()
    )["steps_completed"] == before_completed
    for step in resume_steps.values():
        step.run.assert_not_called()


def test_invalid_resume_with_metadata_override_preserves_entire_canonical_bytes(
    tmp_path: Path,
) -> None:
    src = _make_data_room(tmp_path)
    project_root = tmp_path / "project_root"
    project_root.mkdir()
    first = run_lifecycle(
        src,
        deal_slug="invalid_tax",
        project_root=project_root,
        steps=_build_minimal_steps({"metadata": {}, "unit_cohorts": []}),
        millage_rate="25.31",
    )
    run_dir = _run_dir(project_root, "invalid_tax", first.run_id)
    before = (run_dir / "intake" / "canonical_deal.json").read_bytes()
    resume_steps = _build_minimal_steps(
        {"metadata": {}, "unit_cohorts": []},
    )

    result = run_lifecycle(
        src,
        deal_slug="invalid_tax",
        project_root=project_root,
        steps=resume_steps,
        resume_run_id=first.run_id,
        address="200 New Ave",
        millage_rate="0",
    )

    assert result.status == "needs_analyst_input"
    assert result.exit_code == 2
    assert (run_dir / "intake" / "canonical_deal.json").read_bytes() == before
    assert any(
        blocker["id"] == "invalid_property_tax_millage"
        and blocker["submitted_value"] == "0"
        for blocker in json.loads(
            (run_dir / "_lifecycle_state.json").read_text()
        )["blockers"]
    )
    for step in resume_steps.values():
        step.is_satisfied.assert_not_called()
    resume_steps["judgment"].run.assert_not_called()
    resume_steps["underwriting"].run.assert_not_called()


def test_malformed_policy_and_unresolved_evidence_block_before_valuation(
    tmp_path: Path,
) -> None:
    cases = [
        (
            {
                "metadata": {
                    "property_summary": {
                        "property_tax_policy": {
                            **_property_tax_policy(),
                            "millage_rate_mills": 0,
                        },
                        "property_tax_evidence_candidates": [
                            {
                                "millage_rate_mills": 25.31,
                                "source": "county_tax_notice",
                                "source_locator": "raw_inputs/notice.pdf, page 2",
                            }
                        ],
                    }
                },
                "unit_cohorts": [],
            },
            "invalid_property_tax_millage",
        ),
        (
            {
                "metadata": {
                    "property_summary": {
                        "property_tax_policy": {
                            **_property_tax_policy(),
                            "assessment_ratio": 0.7,
                            "analyst_override": False,
                        },
                        "property_tax_evidence_candidates": [
                            {
                                "millage_rate_mills": 25.31,
                                "source": "county_tax_notice",
                                "source_locator": "raw_inputs/notice.pdf, page 2",
                            }
                        ],
                    }
                },
                "unit_cohorts": [],
            },
            "unsupported_property_tax_assessment_override",
        ),
        (
            {
                "metadata": {
                    "property_summary": {
                        "property_tax_evidence_candidates": [
                            {
                                "millage_rate_mills": 25.31,
                                "source": "county_tax_notice",
                                "source_locator": "raw_inputs/notice.pdf, page 2",
                            }
                        ],
                        "property_tax_evidence_locations": [
                            "raw_inputs/notice.pdf, page 2",
                            "raw_inputs/OM.pdf, page 18, label Combined millage",
                        ],
                    }
                },
                "unit_cohorts": [],
            },
            "missing_property_tax_millage",
        ),
    ]
    for index, (canonical, expected_blocker) in enumerate(cases):
        src = tmp_path / f"data_room_{index}"
        src.mkdir()
        (src / "OM.pdf").write_bytes(b"x")
        project_root = tmp_path / f"project_root_{index}"
        project_root.mkdir()
        steps = _build_minimal_steps(canonical, include_tax_policy=False)

        result = run_lifecycle(
            src,
            deal_slug=f"blocked_tax_{index}",
            project_root=project_root,
            steps=steps,
        )

        run_dir = _run_dir(
            project_root, f"blocked_tax_{index}", result.run_id
        )
        assert result.status == "needs_analyst_input"
        assert result.exit_code == 2
        assert (
            run_dir / "intake" / "canonical_deal.json"
        ).read_bytes() == json.dumps(canonical).encode()
        blockers = json.loads(
            (run_dir / "_lifecycle_state.json").read_text()
        )["blockers"]
        assert any(blocker["id"] == expected_blocker for blocker in blockers)
        steps["comps"].run.assert_not_called()
        steps["judgment"].run.assert_not_called()
        steps["underwriting"].run.assert_not_called()


def test_cli_millage_preserves_approved_ratio_before_resumed_downstream_work(
    tmp_path: Path,
) -> None:
    src = _make_data_room(tmp_path)
    project_root = tmp_path / "project_root"
    project_root.mkdir()
    canonical = {
        "metadata": {
            "property_summary": {
                "property_tax_policy": _approved_ratio_policy(),
            }
        },
        "unit_cohorts": [],
    }
    first = run_lifecycle(
        src,
        deal_slug="ratio_tax",
        project_root=project_root,
        steps=_build_minimal_steps(canonical, include_tax_policy=False),
    )
    run_dir = _run_dir(project_root, "ratio_tax", first.run_id)
    resume_steps = _build_minimal_steps(canonical, include_tax_policy=False)
    for name, step in resume_steps.items():
        step.is_satisfied.side_effect = (
            lambda state, current_run_dir, _name=name:
            _name in state.steps_completed
        )
    seen_policies: list[dict] = []
    judgment_side_effect = resume_steps["judgment"].run.side_effect

    def record_judgment(state, current_run_dir):
        persisted = json.loads(
            (current_run_dir / "intake" / "canonical_deal.json").read_text()
        )
        seen_policies.append(
            persisted["metadata"]["property_summary"]["property_tax_policy"]
        )
        return judgment_side_effect(state, current_run_dir)

    resume_steps["judgment"].run.side_effect = record_judgment

    resumed = run_lifecycle(
        src,
        deal_slug="ratio_tax",
        project_root=project_root,
        steps=resume_steps,
        resume_run_id=first.run_id,
        millage_rate="25.31",
    )

    expected = {
        "millage_rate_mills": 25.31,
        "assessment_ratio": 0.7,
        "source": "composite_evidence",
        "source_locator": (
            "millage=plat lifecycle:--millage-rate;"
            "assessment_ratio=cad_rate_table:raw_inputs/CAD.pdf, page 3"
        ),
        "analyst_override": True,
    }
    assert resumed.exit_code == 0
    assert seen_policies == [expected]
    persisted = json.loads(
        (run_dir / "intake" / "canonical_deal.json").read_text()
    )
    assert (
        persisted["metadata"]["property_summary"]["property_tax_policy"]
        == expected
    )
    resume_steps["intake"].is_satisfied.assert_called_once()
    resume_steps["comps"].is_satisfied.assert_called_once()
    resume_steps["intake"].run.assert_not_called()
    resume_steps["comps"].run.assert_not_called()
    resume_steps["judgment"].run.assert_called_once()
    resume_steps["underwriting"].run.assert_called_once()


def test_valid_cli_repairs_malformed_policy_and_clears_exact_resume_blocker(
    tmp_path: Path,
) -> None:
    cases = [
        (
            {
                **_property_tax_policy(),
                "millage_rate_mills": 0,
            },
            "invalid_property_tax_millage",
        ),
        (
            {
                **_property_tax_policy(),
                "assessment_ratio": 0.7,
                "analyst_override": False,
            },
            "unsupported_property_tax_assessment_override",
        ),
    ]
    for index, (policy, blocker_id) in enumerate(cases):
        src = tmp_path / f"repair_data_room_{index}"
        src.mkdir()
        (src / "OM.pdf").write_bytes(b"x")
        project_root = tmp_path / f"repair_project_root_{index}"
        project_root.mkdir()
        slug = f"repair_tax_{index}"
        malformed = {
            "metadata": {
                "property_summary": {"property_tax_policy": policy}
            },
            "unit_cohorts": [],
        }
        first = run_lifecycle(
            src,
            deal_slug=slug,
            project_root=project_root,
            steps=_build_minimal_steps(
                malformed,
                include_tax_policy=False,
            ),
        )
        assert first.exit_code == 2
        run_dir = _run_dir(project_root, slug, first.run_id)
        canonical_path = run_dir / "intake" / "canonical_deal.json"
        before = canonical_path.read_bytes()
        initial_state = json.loads(
            (run_dir / "_lifecycle_state.json").read_text()
        )
        blocker = next(
            item for item in initial_state["blockers"]
            if item["id"] == blocker_id
        )
        assert blocker["resolution_hint"] == (
            f"plat lifecycle {src.resolve()} "
            f"--resume {slug}/{first.run_id} --millage-rate <mills>"
        )
        resume_steps = _build_minimal_steps(
            malformed,
            include_tax_policy=False,
        )
        resume_steps["intake"].is_satisfied.return_value = True

        resumed = run_lifecycle(
            src,
            deal_slug=slug,
            project_root=project_root,
            steps=resume_steps,
            resume_run_id=first.run_id,
            millage_rate="25.31",
        )

        assert resumed.exit_code == 0
        assert canonical_path.read_bytes() != before
        canonical = json.loads(canonical_path.read_text())
        assert canonical["metadata"]["property_summary"][
            "property_tax_policy"
        ] == {
            "millage_rate_mills": 25.31,
            "assessment_ratio": 1.0,
            "source": "analyst",
            "source_locator": "plat lifecycle:--millage-rate",
            "analyst_override": False,
        }
        state = json.loads(
            (run_dir / "_lifecycle_state.json").read_text()
        )
        assert not any(
            item["id"] in {
                "invalid_property_tax_millage",
                "unsupported_property_tax_assessment_override",
            }
            for item in state["blockers"]
        )
        punchlist = json.loads((run_dir / "punchlist.json").read_text())
        assert not any(
            item["id"] in {
                "invalid_property_tax_millage",
                "unsupported_property_tax_assessment_override",
            }
            for item in punchlist["blockers"]
        )
        resume_steps["intake"].run.assert_not_called()
        resume_steps["judgment"].run.assert_called_once()
        resume_steps["underwriting"].run.assert_called_once()


def test_derive_deal_slug_strips_data_room_suffix(tmp_path: Path) -> None:
    data_room = tmp_path / "laurel_heights_cityview_data_room"
    data_room.mkdir()

    assert _derive_deal_slug(data_room) == "laurel_heights_cityview"
