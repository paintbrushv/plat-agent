"""Wave 5 Task 5.1 — deal-memo-writer contract tests.

The deal-memo-writer is an LLM agent (Sonnet); its prompt at
`.claude/agents/deal-memo-writer.md` is the production source of truth.
These tests run a Python *simulator* of that contract (`tests/memo_simulator`)
which implements the same rules deterministically. The simulator is the
witness — if it disagrees with the prompt on a fixture, either the prompt is
ambiguous or the simulator is wrong; both are fixable in this PR.

Audit findings exercised:
- M-1 cross-run reconciliation (test_cross_run_disagreement_flag)
- M-2 sanity bands inline (test_sanity_band_emitted_per_metric)
- M-3 partnership IRR present (test_partnership_irr_in_metrics_table)
- M-4 staleness/idempotency (test_memo_header_includes_required_fields,
                             test_staleness_check_prevents_silent_overwrite)
- M-7 cohort_id collision defense-in-depth (test_cohort_collision_surfaces_in_risk_flags)
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tests.memo_simulator import compose_memo


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------

def _build_run_layout(
    tmp_path: Path,
    *,
    run_id: str = "run_001",
    deal_summary: dict | None = None,
    provenance: dict | None = None,
    canonical: dict | None = None,
) -> Path:
    """Lay out outputs/<run_id>/{intake,underwriting}/ with the supplied
    JSONs. Returns deal_root."""
    deal_root = tmp_path / "deal"
    run_root = deal_root / "outputs" / run_id
    (run_root / "underwriting").mkdir(parents=True)
    (run_root / "intake").mkdir(parents=True)
    if deal_summary is not None:
        (run_root / "underwriting" / "deal_summary.json").write_text(
            json.dumps(deal_summary)
        )
    if provenance is not None:
        (run_root / "underwriting" / "_provenance.json").write_text(
            json.dumps(provenance)
        )
    if canonical is not None:
        (run_root / "intake" / "canonical_deal.json").write_text(
            json.dumps(canonical)
        )
    return deal_root


def _baseline_provenance(**overrides) -> dict:
    base = {
        "engine_version": "0.4.2",
        "schema_version": "v1.3",
        "inputs_hash_sha256": "a" * 64,
        "generated_at_utc": "2026-04-26T12:00:00Z",
        "validator_status": "PASS",
        "feasibility_verdict": "pass",
        "feasibility_sanity_flags": [],
        "feasibility_reasons": [],
    }
    base.update(overrides)
    return base


def _baseline_canonical(**overrides) -> dict:
    base = {
        "unit_cohorts": [
            {
                "cohort_id": "a1",
                "bedrooms": 1,
                "bathrooms": 1,
                "count": 60,
                "current_monthly_rent": 1500.0,
                "target_monthly_rent": 1700.0,
            },
            {
                "cohort_id": "b1",
                "bedrooms": 2,
                "bathrooms": 2,
                "count": 40,
                "current_monthly_rent": 1900.0,
                "target_monthly_rent": 2100.0,
            },
        ]
    }
    base.update(overrides)
    return base


def _deal_summary(*, irr=0.18, em=1.7, dscr_min=1.35, going_in=0.055,
                  exit_cap=0.06, partnership_irr=0.14,
                  partnership_em=1.5) -> dict:
    return {
        "metrics": {
            "irr": {"levered_irr": irr, "unlevered_irr": irr - 0.02},
            "equity_multiple": {"levered_em": em, "unlevered_em": em - 0.05},
            "dscr": {"minimum_dscr": dscr_min, "average_dscr": dscr_min + 0.15},
            "yields": {"going_in_cap_rate": going_in, "exit_cap_rate": exit_cap},
        },
        "fund_waterfall": {
            "summary": {
                "partnership_irr": partnership_irr,
                "partnership_equity_multiple": partnership_em,
                "gp_total_distribution": 1_200_000.0,
                "lp_total_distribution": 18_500_000.0,
            }
        },
    }


# ---------------------------------------------------------------------------
# M-4 — header includes required fields
# ---------------------------------------------------------------------------

def test_memo_header_includes_required_fields(tmp_path: Path) -> None:
    """Audit M-4: header MUST carry engine_version, schema_version,
    inputs_hash_sha256_short (12 chars), and generated_at_utc."""
    prov = _baseline_provenance(
        inputs_hash_sha256="deadbeef" + "0" * 56  # 12-char short = 'deadbeef0000'
    )
    deal_root = _build_run_layout(
        tmp_path,
        deal_summary=_deal_summary(),
        provenance=prov,
        canonical=_baseline_canonical(),
    )

    fixed_now = datetime(2026, 4, 26, 18, 30, 0, tzinfo=timezone.utc)
    result = compose_memo(
        deal_slug="test_deal",
        run_id="run_001",
        deal_root=deal_root,
        canonical=_baseline_canonical(),
        deal_summary=_deal_summary(),
        provenance=prov,
        composition_time_utc=fixed_now,
    )

    assert result.header["engine_version"] == "0.4.2"
    assert result.header["schema_version"] == "v1.3"
    # First 12 chars of inputs_hash_sha256 — defends against "show only 8" prompt drift.
    assert result.header["inputs_hash_sha256_short"] == "deadbeef0000"
    assert len(result.header["inputs_hash_sha256_short"]) == 12
    # generated_at_utc parses as ISO 8601
    parsed = datetime.fromisoformat(
        result.header["generated_at_utc"].replace("Z", "+00:00")
    )
    assert parsed == fixed_now
    # No stale_provenance flag because all required fields are present.
    assert "stale_provenance" not in result.structural_integrity_flags


def test_memo_header_emits_stale_provenance_when_fields_missing(
    tmp_path: Path,
) -> None:
    """If the runner failed to persist engine_version/inputs_hash, the memo
    composer surfaces it as `stale_provenance` — never silently omits."""
    incomplete = {
        # engine_version, schema_version, inputs_hash_sha256 all missing
        "feasibility_verdict": "marginal",
        "feasibility_sanity_flags": [],
        "feasibility_reasons": [],
        "validator_status": "PASS",
        "generated_at_utc": "2026-04-26T12:00:00Z",
    }
    deal_root = _build_run_layout(
        tmp_path,
        deal_summary=_deal_summary(),
        provenance=incomplete,
        canonical=_baseline_canonical(),
    )
    result = compose_memo(
        deal_slug="test_deal",
        run_id="run_001",
        deal_root=deal_root,
        canonical=_baseline_canonical(),
        deal_summary=_deal_summary(),
        provenance=incomplete,
    )
    assert result.header["engine_version"] == "not_available"
    assert result.header["inputs_hash_sha256_short"] == "not_available"
    assert "stale_provenance" in result.structural_integrity_flags


# ---------------------------------------------------------------------------
# M-2 — sanity bands per metric
# ---------------------------------------------------------------------------

def test_sanity_band_emitted_per_metric(tmp_path: Path) -> None:
    """A deal with cap_rate=0.039 (below HARD lower bound 0.04) MUST surface
    `cap_rate_outside_4_7_band` as a FLAG verdict on the going_in_cap row."""
    prov = _baseline_provenance(
        feasibility_sanity_flags=[],  # provenance is sparse — exercise the FALLBACK path
    )
    summary = _deal_summary(going_in=0.039, exit_cap=0.045)
    deal_root = _build_run_layout(
        tmp_path,
        deal_summary=summary,
        provenance=prov,
        canonical=_baseline_canonical(),
    )
    result = compose_memo(
        deal_slug="test_deal",
        run_id="run_001",
        deal_root=deal_root,
        canonical=_baseline_canonical(),
        deal_summary=summary,
        provenance=prov,
    )

    # FLAG row present
    going_in_row = next(
        r for r in result.metrics_table_rows if r["metric"] == "going_in_cap_raw"
    )
    assert going_in_row["verdict"] == "FLAG"
    assert going_in_row["flag"] == "cap_rate_outside_4_7_band"

    # And the flag bubbles up to the memo's sanity_flags
    assert "cap_rate_outside_4_7_band" in result.sanity_flags


def test_sanity_band_uses_provenance_flags_as_authoritative(tmp_path: Path) -> None:
    """When `_provenance.json.feasibility_sanity_flags` carries flags
    (Wave 4 standardized), those are PRIMARY — the composer cites them
    verbatim instead of re-deriving from bands."""
    prov = _baseline_provenance(
        feasibility_verdict="fail",
        feasibility_sanity_flags=[
            "cap_rate_outside_4_7_band",
            "irr_outside_expected_band",
        ],
        feasibility_reasons=["going_in_cap=0.081 > 0.07", "levered_irr=0.4698 > 0.30"],
    )
    summary = _deal_summary(going_in=0.081, irr=0.4698)
    deal_root = _build_run_layout(
        tmp_path,
        deal_summary=summary,
        provenance=prov,
        canonical=_baseline_canonical(),
    )
    result = compose_memo(
        deal_slug="demo_at_legacy_park",
        run_id="run_002_federation",
        deal_root=deal_root,
        canonical=_baseline_canonical(),
        deal_summary=summary,
        provenance=prov,
    )
    # Verbatim primary flags surface in sanity_flags
    assert "cap_rate_outside_4_7_band" in result.sanity_flags
    assert "irr_outside_expected_band" in result.sanity_flags
    # Fail verdict from provenance forces "pass" recommendation
    assert result.recommendation == "pass"


# ---------------------------------------------------------------------------
# M-1 — cross-run reconciliation
# ---------------------------------------------------------------------------

def test_cross_run_disagreement_flag(tmp_path: Path) -> None:
    """The Legacy Park counterfactual: run_001 produced IRR 26.74%, run_002
    produced IRR 46.98% on the same canonical. The composer MUST emit
    `cross_run_disagreement` and refuse a single-run verdict."""
    deal_root = tmp_path / "deal"
    # Lay down run_001 (the prior, federation-shaped) with IRR=0.2674
    prior_root = deal_root / "outputs" / "run_001" / "underwriting"
    prior_root.mkdir(parents=True)
    (prior_root / "deal_summary.json").write_text(json.dumps(
        _deal_summary(irr=0.2674, em=2.1)
    ))
    # And run_002 (current) with IRR=0.4698
    current_root = deal_root / "outputs" / "run_002" / "underwriting"
    current_root.mkdir(parents=True)
    current_summary = _deal_summary(irr=0.4698, em=3.4)
    (current_root / "deal_summary.json").write_text(json.dumps(current_summary))
    (deal_root / "outputs" / "run_002" / "intake").mkdir(parents=True)
    (deal_root / "outputs" / "run_002" / "intake" / "canonical_deal.json").write_text(
        json.dumps(_baseline_canonical())
    )

    prov = _baseline_provenance(
        feasibility_verdict="fail",
        feasibility_sanity_flags=["irr_outside_expected_band"],
    )
    (current_root / "_provenance.json").write_text(json.dumps(prov))

    result = compose_memo(
        deal_slug="demo_at_legacy_park",
        run_id="run_002",
        deal_root=deal_root,
        canonical=_baseline_canonical(),
        deal_summary=current_summary,
        provenance=prov,
    )

    # The disagreement flag fires
    assert "cross_run_disagreement" in result.structural_integrity_flags
    assert "cross_run_disagreement" in result.sanity_flags
    # Cross-run table populated with the prior run side-by-side
    assert len(result.cross_run_table) >= 1
    flagged_rows = [r for r in result.cross_run_table if r["triggers_disagreement"]]
    assert len(flagged_rows) >= 1
    # Delta is correctly computed: 0.4698 - 0.2674 = 0.2024 > 0.05 threshold
    assert flagged_rows[0]["irr_delta"] == pytest.approx(0.2024)
    # Recommendation is downgraded — never plain "pursue" with structural flags
    assert result.recommendation in {"pursue-with-conditions", "pass"}


def test_cross_run_no_disagreement_when_within_tolerance(tmp_path: Path) -> None:
    """Two prior runs within 5pp IRR / 0.3x EM should NOT trigger the flag —
    the threshold is the audit-specified delta."""
    deal_root = tmp_path / "deal"
    prior_root = deal_root / "outputs" / "run_001" / "underwriting"
    prior_root.mkdir(parents=True)
    (prior_root / "deal_summary.json").write_text(json.dumps(
        _deal_summary(irr=0.18, em=1.7)
    ))
    current_root = deal_root / "outputs" / "run_002" / "underwriting"
    current_root.mkdir(parents=True)
    current_summary = _deal_summary(irr=0.20, em=1.8)
    (current_root / "deal_summary.json").write_text(json.dumps(current_summary))

    prov = _baseline_provenance()
    (current_root / "_provenance.json").write_text(json.dumps(prov))

    result = compose_memo(
        deal_slug="quiet_deal",
        run_id="run_002",
        deal_root=deal_root,
        canonical=_baseline_canonical(),
        deal_summary=current_summary,
        provenance=prov,
    )
    assert "cross_run_disagreement" not in result.structural_integrity_flags


# ---------------------------------------------------------------------------
# M-3 — partnership IRR in metrics table
# ---------------------------------------------------------------------------

def test_partnership_irr_in_metrics_table(tmp_path: Path) -> None:
    """fund_waterfall.summary.{partnership_irr,partnership_equity_multiple,
    gp_total_distribution,lp_total_distribution} must each have a metrics row.
    Partnership IRR < 0.08 must trigger `partnership_irr_below_lp_hurdle`."""
    summary = _deal_summary(partnership_irr=0.0193, partnership_em=1.09)
    prov = _baseline_provenance()
    deal_root = _build_run_layout(
        tmp_path,
        deal_summary=summary,
        provenance=prov,
        canonical=_baseline_canonical(),
    )
    result = compose_memo(
        deal_slug="first_street",
        run_id="run_002",
        deal_root=deal_root,
        canonical=_baseline_canonical(),
        deal_summary=summary,
        provenance=prov,
    )
    metrics_by_name = {r["metric"]: r for r in result.metrics_table_rows}
    # All four required rows present
    for name in (
        "partnership_irr",
        "partnership_equity_multiple",
        "gp_total_distribution",
        "lp_total_distribution",
    ):
        assert name in metrics_by_name, f"missing required metric row: {name}"

    # Values are correctly extracted
    assert metrics_by_name["partnership_irr"]["value"] == pytest.approx(0.0193)
    assert metrics_by_name["partnership_equity_multiple"]["value"] == pytest.approx(1.09)
    assert metrics_by_name["gp_total_distribution"]["value"] == 1_200_000.0
    assert metrics_by_name["lp_total_distribution"]["value"] == 18_500_000.0

    # Partnership IRR 1.93% < 8% → WARN partnership_irr_below_lp_hurdle
    p_row = metrics_by_name["partnership_irr"]
    assert p_row["verdict"] == "WARN"
    assert p_row["flag"] == "partnership_irr_below_lp_hurdle"
    # Partnership EM 1.09x < 1.5x → WARN equity_multiple_low
    pem_row = metrics_by_name["partnership_equity_multiple"]
    assert pem_row["verdict"] == "WARN"
    assert pem_row["flag"] == "equity_multiple_low"


# ---------------------------------------------------------------------------
# M-4 — staleness check
# ---------------------------------------------------------------------------

def test_staleness_check_prevents_silent_overwrite(tmp_path: Path) -> None:
    """Existing memo's generated_at_utc < cited artifact mtime → composer
    refuses to overwrite UNLESS recompose=True."""
    prov = _baseline_provenance()
    summary = _deal_summary()
    deal_root = _build_run_layout(
        tmp_path,
        deal_summary=summary,
        provenance=prov,
        canonical=_baseline_canonical(),
    )
    run_root = deal_root / "outputs" / "run_001"

    # Write a prior memo dated 2026-01-01 (before our artifacts)
    prior_memo = run_root / "memo.md"
    prior_memo.write_text(
        "# Prior memo\n"
        "**Deal slug:** `test`\n"
        "**generated_at_utc:** `2026-01-01T00:00:00Z`\n"
    )
    # Now bump the deal_summary mtime to "now" so it's newer than the prior memo.
    ds_path = run_root / "underwriting" / "deal_summary.json"
    now_ts = time.time()
    os.utime(ds_path, (now_ts, now_ts))

    # Compose without recompose flag → must refuse
    result = compose_memo(
        deal_slug="test_deal",
        run_id="run_001",
        deal_root=deal_root,
        canonical=_baseline_canonical(),
        deal_summary=summary,
        provenance=prov,
        recompose=False,
    )
    assert result.refused_overwrite is True
    assert result.refusal_reason is not None
    assert "newer than prior memo" in result.refusal_reason

    # With recompose=True → proceeds
    result2 = compose_memo(
        deal_slug="test_deal",
        run_id="run_001",
        deal_root=deal_root,
        canonical=_baseline_canonical(),
        deal_summary=summary,
        provenance=prov,
        recompose=True,
    )
    assert result2.refused_overwrite is False
    assert result2.refusal_reason is None


def test_staleness_check_passes_when_no_prior_memo(tmp_path: Path) -> None:
    """First-time composition has no prior memo to be stale relative to."""
    prov = _baseline_provenance()
    summary = _deal_summary()
    deal_root = _build_run_layout(
        tmp_path,
        deal_summary=summary,
        provenance=prov,
        canonical=_baseline_canonical(),
    )
    result = compose_memo(
        deal_slug="test_deal",
        run_id="run_001",
        deal_root=deal_root,
        canonical=_baseline_canonical(),
        deal_summary=summary,
        provenance=prov,
    )
    assert result.refused_overwrite is False


def test_source_artifacts_footer_lists_cited_files(tmp_path: Path) -> None:
    """§9 footer must list every cited file with its mtime — input to the next
    staleness check."""
    prov = _baseline_provenance()
    summary = _deal_summary()
    deal_root = _build_run_layout(
        tmp_path,
        deal_summary=summary,
        provenance=prov,
        canonical=_baseline_canonical(),
    )
    result = compose_memo(
        deal_slug="test_deal",
        run_id="run_001",
        deal_root=deal_root,
        canonical=_baseline_canonical(),
        deal_summary=summary,
        provenance=prov,
    )
    paths = {Path(row["path"]).name for row in result.source_artifacts_footer}
    assert "deal_summary.json" in paths
    assert "_provenance.json" in paths
    assert "canonical_deal.json" in paths
    # Each entry has a parseable ISO 8601 mtime
    for row in result.source_artifacts_footer:
        datetime.fromisoformat(row["mtime_utc"])


# ---------------------------------------------------------------------------
# M-7 — cohort collision defense in depth
# ---------------------------------------------------------------------------

def test_cohort_collision_surfaces_in_risk_flags(tmp_path: Path) -> None:
    """If unit_cohorts has two entries with cohort_id='beal_renovated',
    the §2 table must print BOTH with #1/#2 suffixes (never collapse) and
    `cohort_id_collision` must surface in §1 / §7."""
    canonical = {
        "unit_cohorts": [
            {
                "cohort_id": "beal_renovated",
                "bedrooms": 1,
                "bathrooms": 1,
                "count": 30,
                "current_monthly_rent": 900.0,
                "target_monthly_rent": 1100.0,
            },
            {
                "cohort_id": "beal_renovated",  # COLLISION
                "bedrooms": 1,
                "bathrooms": 1,
                "count": 12,
                "current_monthly_rent": 950.0,
                "target_monthly_rent": 1150.0,
            },
            {
                "cohort_id": "essex",
                "bedrooms": 2,
                "bathrooms": 2,
                "count": 40,
                "current_monthly_rent": 1100.0,
                "target_monthly_rent": 1300.0,
            },
        ]
    }
    prov = _baseline_provenance()
    summary = _deal_summary()
    deal_root = _build_run_layout(
        tmp_path,
        deal_summary=summary,
        provenance=prov,
        canonical=canonical,
    )
    result = compose_memo(
        deal_slug="demo_at_legacy_park",
        run_id="run_002_federation",
        deal_root=deal_root,
        canonical=canonical,
        deal_summary=summary,
        provenance=prov,
    )
    # Flag surfaces in structural integrity AND risk flags
    assert "cohort_id_collision" in result.structural_integrity_flags
    assert "cohort_id_collision" in result.sanity_flags
    # Both duplicate rows print with #1, #2 suffixes — never collapsed
    display_ids = [r["display_cohort_id"] for r in result.cohort_table_rows]
    assert "beal_renovated #1" in display_ids
    assert "beal_renovated #2" in display_ids
    # The non-duplicate row is unchanged
    assert "essex" in display_ids
    # Total row count == input count (no merging)
    assert len(result.cohort_table_rows) == 3
    # Recommendation downgraded due to structural flag
    assert result.recommendation in {"pursue-with-conditions", "pass"}


def test_cohort_table_no_suffix_when_no_collision(tmp_path: Path) -> None:
    """No collision → no #1/#2 suffix added (don't add noise)."""
    canonical = _baseline_canonical()
    prov = _baseline_provenance()
    summary = _deal_summary()
    deal_root = _build_run_layout(
        tmp_path,
        deal_summary=summary,
        provenance=prov,
        canonical=canonical,
    )
    result = compose_memo(
        deal_slug="clean_deal",
        run_id="run_001",
        deal_root=deal_root,
        canonical=canonical,
        deal_summary=summary,
        provenance=prov,
    )
    assert "cohort_id_collision" not in result.structural_integrity_flags
    display_ids = {r["display_cohort_id"] for r in result.cohort_table_rows}
    assert display_ids == {"a1", "b1"}
