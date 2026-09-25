from __future__ import annotations
import shlex

from decimal import Decimal
from pathlib import Path
import pytest

from plat_agent.lifecycle.property_tax import (
    PROPERTY_TAX_MILLAGE_QUESTION,
    PropertyTaxEvidenceCandidate,
    render_property_tax_resume_command,
    resolve_property_tax_gate,
)


def _canonical_with_candidates(*candidates: tuple[str, str, str]) -> dict:
    return {
        "metadata": {
            "property_summary": {
                "property_tax_evidence_candidates": [
                    {
                        "millage_rate_mills": value,
                        "source": source,
                        "source_locator": locator,
                    }
                    for value, source, locator in candidates
                ]
            }
        }
    }


def _policy(mills: float = 25.31) -> dict:
    return {
        "millage_rate_mills": mills,
        "assessment_ratio": 1.0,
        "source": "analyst",
        "source_locator": "underwrite-deal:property_tax_millage",
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


def test_question_uses_the_required_explicit_unit() -> None:
    assert PROPERTY_TAX_MILLAGE_QUESTION == (
        "What combined property-tax millage should be used? Enter mills per $1,000 "
        "of assessed value (for example, `25.31`)."
    )


def test_conflicting_labelled_candidates_require_current_analyst_answer() -> None:
    canonical = _canonical_with_candidates(
        ("25.31", "county_tax_notice", "raw_inputs/notice.pdf, page 2"),
        ("24.90", "offering_memorandum", "raw_inputs/OM.pdf, page 18"),
    )

    result = resolve_property_tax_gate(
        canonical,
        millage_rate=None,
        source_locator=None,
        data_room=Path("/tmp/Deal Room"),
        deal_slug="sample_deal",
        run_id="run_003",
    )

    assert result.policy is None
    assert result.canonical == canonical
    assert result.changed is False
    assert result.blocker is not None
    assert result.blocker.id == "missing_property_tax_millage"
    assert result.blocker.field == "metadata.property_summary.property_tax_policy.millage_rate_mills"
    assert result.blocker.unit == "mills per $1,000 of assessed value"
    assert result.blocker.evidence_locations == [
        "raw_inputs/OM.pdf, page 18",
        "raw_inputs/notice.pdf, page 2",
    ]
    assert "valuation has not run" in result.blocker.description
    assert result.blocker.resolution_hint == (
        f"plat lifecycle {shlex.quote(str(Path('/tmp/Deal Room').resolve()))} "
        "--resume sample_deal/run_003 --millage-rate <mills>"
    )


def test_cli_answer_wins_and_materializes_stable_default_policy() -> None:
    result = resolve_property_tax_gate(
        _canonical_with_candidates(
            ("24.90", "offering_memorandum", "raw_inputs/OM.pdf, page 18")
        ),
        millage_rate="25.31",
        source_locator="plat lifecycle:--millage-rate",
        data_room=Path("/tmp/Deal Room"),
        deal_slug="sample_deal",
        run_id="run_003",
    )

    assert result.blocker is None
    assert result.changed is True
    assert result.policy == {
        "millage_rate_mills": 25.31,
        "assessment_ratio": 1.0,
        "source": "analyst",
        "source_locator": "plat lifecycle:--millage-rate",
        "analyst_override": False,
    }
    assert result.canonical["metadata"]["property_summary"]["property_tax_policy"] == result.policy


def test_cli_answer_preserves_approved_ratio_with_exact_composite_provenance() -> None:
    canonical = {
        "metadata": {
            "property_summary": {
                "property_tax_policy": _approved_ratio_policy()
            }
        }
    }

    result = resolve_property_tax_gate(
        canonical,
        millage_rate="25.31",
        source_locator="plat lifecycle:--millage-rate",
        data_room=Path("/tmp/Deal Room"),
        deal_slug="sample_deal",
        run_id="run_003",
    )

    assert result.blocker is None
    assert result.changed is True
    assert result.policy == {
        "millage_rate_mills": 25.31,
        "assessment_ratio": 0.7,
        "source": "composite_evidence",
        "source_locator": (
            "millage=plat lifecycle:--millage-rate;"
            "assessment_ratio=cad_rate_table:raw_inputs/CAD.pdf, page 3"
        ),
        "analyst_override": True,
    }


def test_cli_answer_preserves_ratio_from_landed_migration_provenance() -> None:
    policy = _approved_ratio_policy()
    policy["source_locator"] = (
        "cad_rate_table:raw_inputs/CAD.pdf, page 3; "
        "county_tax_notice:raw_inputs/notice.pdf, page 2"
    )
    canonical = {
        "metadata": {"property_summary": {"property_tax_policy": policy}}
    }

    result = resolve_property_tax_gate(
        canonical,
        millage_rate="25.31",
        source_locator="plat lifecycle:--millage-rate",
        data_room=Path("/tmp/Deal Room"),
        deal_slug="sample_deal",
        run_id="run_003",
    )

    assert result.blocker is None
    assert result.policy is not None
    assert result.policy["assessment_ratio"] == 0.7
    assert result.policy["analyst_override"] is True
    assert result.policy["source"] == "composite_evidence"
    assert result.policy["source_locator"] == (
        "millage=plat lifecycle:--millage-rate;"
        "assessment_ratio=cad_rate_table:raw_inputs/CAD.pdf, page 3"
    )


def test_cli_answer_preserves_multiple_stable_ratio_evidence_locations() -> None:
    policy = _approved_ratio_policy()
    policy["source_locator"] = (
        "cad_rate_table:raw_inputs/A.pdf, page 3; "
        "cad_rate_table:raw_inputs/B.pdf, page 4; "
        "county_tax_notice:raw_inputs/notice.pdf, page 2"
    )
    canonical = {
        "metadata": {"property_summary": {"property_tax_policy": policy}}
    }

    result = resolve_property_tax_gate(
        canonical,
        millage_rate="25.31",
        source_locator="plat lifecycle:--millage-rate",
        data_room=Path("/tmp/Deal Room"),
        deal_slug="sample_deal",
        run_id="run_003",
    )

    assert result.blocker is None
    assert result.policy is not None
    assert result.policy["assessment_ratio"] == 0.7
    assert result.policy["analyst_override"] is True
    assert result.policy["source_locator"] == (
        "millage=plat lifecycle:--millage-rate;"
        "assessment_ratio=cad_rate_table:"
        "raw_inputs/A.pdf, page 3 | raw_inputs/B.pdf, page 4"
    )


def test_invalid_cli_value_preserves_submission_and_canonical() -> None:
    canonical = {
        "metadata": {
            "property_summary": {
                "property_tax_policy": {
                    **_policy(),
                    "assessment_ratio": 0.7,
                    "analyst_override": False,
                }
            }
        },
        "other": {"untouched": True},
    }

    result = resolve_property_tax_gate(
        canonical,
        millage_rate="0",
        source_locator="plat lifecycle:--millage-rate",
        data_room=Path("/tmp/Deal Room"),
        deal_slug="sample_deal",
        run_id="run_003",
    )

    assert result.blocker is not None
    assert result.blocker.id == "invalid_property_tax_millage"
    assert result.blocker.submitted_value == "0"
    assert result.policy is None
    assert result.canonical == canonical
    assert result.changed is False


@pytest.mark.parametrize(
    "submitted",
    ["1e400", "1e-400", "25.3100000000000000001"],
)
def test_non_json_roundtrippable_cli_value_is_invalid_without_mutation(
    submitted: str,
) -> None:
    canonical = {
        "metadata": {"property_summary": {"property_tax_policy": _policy()}},
        "other": {"untouched": True},
    }

    result = resolve_property_tax_gate(
        canonical,
        millage_rate=submitted,
        source_locator="plat lifecycle:--millage-rate",
        data_room=Path("/tmp/Deal Room"),
        deal_slug="sample_deal",
        run_id="run_003",
    )

    assert result.blocker is not None
    assert result.blocker.id == "invalid_property_tax_millage"
    assert result.blocker.submitted_value == submitted
    assert result.policy is None
    assert result.canonical == canonical
    assert result.changed is False


def test_existing_valid_canonical_policy_wins_over_conflicting_evidence() -> None:
    canonical = _canonical_with_candidates(
        ("25.31", "county_tax_notice", "raw_inputs/notice.pdf"),
        ("24.90", "offering_memorandum", "raw_inputs/OM.pdf"),
    )
    canonical["metadata"]["property_summary"]["property_tax_policy"] = _policy()

    result = resolve_property_tax_gate(
        canonical,
        millage_rate=None,
        source_locator=None,
        data_room=Path("/tmp/Deal Room"),
        deal_slug="sample_deal",
        run_id="run_003",
    )

    assert result.policy == _policy()
    assert result.blocker is None
    assert result.changed is False
    assert result.canonical == canonical


@pytest.mark.parametrize(
    ("policy", "expected_blocker"),
    [
        (
            {**_policy(), "millage_rate_mills": 0},
            "invalid_property_tax_millage",
        ),
        (
            {
                **_policy(),
                "assessment_ratio": 0.7,
                "analyst_override": False,
            },
            "unsupported_property_tax_assessment_override",
        ),
    ],
)
def test_present_malformed_policy_blocks_instead_of_falling_back_to_candidate(
    policy: dict,
    expected_blocker: str,
) -> None:
    canonical = _canonical_with_candidates(
        ("25.31", "county_tax_notice", "raw_inputs/notice.pdf, page 2")
    )
    canonical["metadata"]["property_summary"]["property_tax_policy"] = policy

    result = resolve_property_tax_gate(
        canonical,
        millage_rate=None,
        source_locator=None,
        data_room=Path("/tmp/Deal Room"),
        deal_slug="sample_deal",
        run_id="run_003",
    )

    assert result.policy is None
    assert result.blocker is not None
    assert result.blocker.id == expected_blocker
    assert result.canonical == canonical
    assert result.changed is False
    if expected_blocker == "unsupported_property_tax_assessment_override":
        assert (
            result.blocker.field
            == "metadata.property_summary.property_tax_policy.assessment_ratio"
        )
        assert result.blocker.unit == "decimal share of purchase price"
        assert result.blocker.submitted_value == "0.7"


@pytest.mark.parametrize(
    "policy",
    [
        {**_policy(), "millage_rate_mills": 0},
        {
            **_policy(),
            "assessment_ratio": 0.7,
            "analyst_override": False,
        },
    ],
)
def test_valid_current_cli_answer_repairs_malformed_canonical_policy(
    policy: dict,
) -> None:
    canonical = {
        "metadata": {
            "property_summary": {
                "property_tax_policy": policy,
                "property_tax_evidence_candidates": [
                    {
                        "millage_rate_mills": 24.9,
                        "source": "offering_memorandum",
                        "source_locator": "raw_inputs/OM.pdf, page 18",
                    }
                ],
            }
        },
        "other": {"untouched": True},
    }

    result = resolve_property_tax_gate(
        canonical,
        millage_rate="25.31",
        source_locator="plat lifecycle:--millage-rate",
        data_room=Path("/tmp/Deal Room"),
        deal_slug="sample_deal",
        run_id="run_003",
    )

    assert result.blocker is None
    assert result.changed is True
    assert result.policy == {
        "millage_rate_mills": 25.31,
        "assessment_ratio": 1.0,
        "source": "analyst",
        "source_locator": "plat lifecycle:--millage-rate",
        "analyst_override": False,
    }
    assert result.canonical["other"] == {"untouched": True}
    assert (
        result.canonical["metadata"]["property_summary"][
            "property_tax_policy"
        ]
        == result.policy
    )


def test_unresolved_evidence_prevents_singleton_candidate_auto_promotion() -> None:
    canonical = _canonical_with_candidates(
        ("25.31", "county_tax_notice", "raw_inputs/notice.pdf, page 2")
    )
    summary = canonical["metadata"]["property_summary"]
    summary["property_tax_evidence_locations"] = [
        "raw_inputs/notice.pdf, page 2",
        "raw_inputs/OM.pdf, page 18, label Combined property tax millage",
    ]

    result = resolve_property_tax_gate(
        canonical,
        millage_rate=None,
        source_locator=None,
        data_room=Path("/tmp/Deal Room"),
        deal_slug="sample_deal",
        run_id="run_003",
    )

    assert result.policy is None
    assert result.blocker is not None
    assert result.blocker.id == "missing_property_tax_millage"
    assert result.blocker.evidence_locations == [
        "raw_inputs/OM.pdf, page 18, label Combined property tax millage",
        "raw_inputs/notice.pdf, page 2",
    ]
    assert result.canonical == canonical


def test_equal_candidates_dedupe_by_decimal_and_have_stable_composite_provenance() -> None:
    candidate = PropertyTaxEvidenceCandidate(
        millage_rate_mills=Decimal("25.310"),
        source="offering_memorandum",
        source_locator="raw_inputs/z-om.pdf, property tax table",
    )
    assert candidate.millage_rate_mills == Decimal("25.310")
    canonical = _canonical_with_candidates(
        ("25.310", "offering_memorandum", "raw_inputs/z-om.pdf, property tax table"),
        ("25.31", "county_tax_notice", "raw_inputs/a-notice.pdf, page 2"),
    )

    result = resolve_property_tax_gate(
        canonical,
        millage_rate=None,
        source_locator=None,
        data_room=Path("/tmp/Deal Room"),
        deal_slug="sample_deal",
        run_id="run_003",
    )

    assert result.blocker is None
    assert result.policy == {
        "millage_rate_mills": 25.31,
        "assessment_ratio": 1.0,
        "source": "composite_evidence",
        "source_locator": (
            "county_tax_notice:raw_inputs/a-notice.pdf, page 2; "
            "offering_memorandum:raw_inputs/z-om.pdf, property tax table"
        ),
        "analyst_override": False,
    }


def test_identical_cli_answer_is_idempotent() -> None:
    canonical = {"metadata": {"property_summary": {"property_tax_policy": _policy()}}}

    result = resolve_property_tax_gate(
        canonical,
        millage_rate="25.310",
        source_locator="underwrite-deal:property_tax_millage",
        data_room=Path("/tmp/Deal Room"),
        deal_slug="sample_deal",
        run_id="run_003",
    )

    assert result.policy == _policy()
    assert result.changed is False
    assert result.canonical == canonical


def test_resume_command_quotes_shell_metacharacters() -> None:
    command = render_property_tax_resume_command(
        Path("/tmp/O'Brien Deal Room"),
        "deal with spaces",
        "run_003;echo nope",
    )

    expected_path = shlex.quote(str(Path("/tmp/O'Brien Deal Room").resolve()))
    assert command == (
        f"plat lifecycle {expected_path} "
        "--resume 'deal with spaces/run_003;echo nope' --millage-rate <mills>"
    )
