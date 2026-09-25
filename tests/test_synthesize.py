"""Tests for plat_agent.synthesize.synthesize_from_federated_artifacts.

Includes both unit tests with fabricated artifacts and an integration test
against the live federated fixtures under
multifamily-underwriting/runs/deals/demo_at_legacy_park/outputs/run_002_federation/.
The integration test is skipped if the live fixture is missing.
"""

import json
from pathlib import Path

import pytest

from plat_agent.synthesize import (
    DEFAULT_ROI_THRESHOLD_PCT,
    SANITY_FLAG_CATALOG,
    _recompute_roi,
    _translate_federated_sanity_flag,
    synthesize_from_federated_artifacts,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_deal_tree(
    tmp_path: Path,
    *,
    run_id: str = "run_001",
    renovation_programs: list[dict] | None = None,
    property_estimate: dict | None = None,
    deal_summary: dict | None = None,
    underwriting_provenance: dict | None = None,
) -> Path:
    """Materialize a federated deal tree under tmp_path. Returns deal_root."""
    deal_root = tmp_path / "fakedeal"
    run_dir = deal_root / "outputs" / run_id
    cm_dir = run_dir / "costmodel"
    uw_dir = run_dir / "underwriting"
    cm_dir.mkdir(parents=True, exist_ok=True)
    uw_dir.mkdir(parents=True, exist_ok=True)

    if renovation_programs is not None:
        (cm_dir / "renovation_programs.json").write_text(json.dumps(renovation_programs))
    if property_estimate is not None:
        (cm_dir / "property_estimate.json").write_text(json.dumps(property_estimate))
    if deal_summary is not None:
        (uw_dir / "deal_summary.json").write_text(json.dumps(deal_summary))
    if underwriting_provenance is not None:
        (uw_dir / "_provenance.json").write_text(json.dumps(underwriting_provenance))
    return deal_root


# ---------------------------------------------------------------------------
# _recompute_roi
# ---------------------------------------------------------------------------

class TestRecomputeRoi:
    def test_passing_program(self):
        # rent_lift = $200/mo; cost = $10000 → ROI = 200*12/10000*100 = 24%
        program = {
            "renovation_cost_per_unit": 10_000,
            "current_monthly_rent": 800,
            "target_monthly_rent": 1_000,
        }
        roi = _recompute_roi(program, threshold_pct=15.0)
        assert roi["roi_passes"] is True
        assert roi["roi_pct"] == 24.0
        assert roi["roi_gap_pp"] == 9.0
        assert roi["monthly_rent_lift"] == 200
        assert roi["cost_reduction_needed_to_pass"] is None
        assert roi["rent_increase_needed_to_pass"] is None

    def test_failing_program_emits_path_to_pass(self):
        # rent_lift = $50/mo; cost = $10000 → ROI = 50*12/10000*100 = 6%; need 15%
        program = {
            "renovation_cost_per_unit": 10_000,
            "current_monthly_rent": 800,
            "target_monthly_rent": 850,
        }
        roi = _recompute_roi(program, threshold_pct=15.0)
        assert roi["roi_passes"] is False
        assert roi["roi_pct"] == 6.0
        assert roi["roi_gap_pp"] == -9.0
        # required_cost = 600/0.15 = 4000 → cost_reduction = 10000-4000 = 6000
        assert roi["cost_reduction_needed_to_pass"] == 6000
        # needed_annual = 10000*0.15 = 1500 → needed_monthly_lift = 125 →
        # needed_target = 800+125 = 925 → rent_increase = 925-850 = 75
        assert roi["rent_increase_needed_to_pass"] == 75

    def test_zero_cost_returns_failure_with_no_path(self):
        program = {
            "renovation_cost_per_unit": 0,
            "current_monthly_rent": 800,
            "target_monthly_rent": 1_000,
        }
        roi = _recompute_roi(program, threshold_pct=15.0)
        assert roi["roi_passes"] is False
        assert roi["roi_pct"] == 0.0
        assert roi["cost_reduction_needed_to_pass"] is None
        assert roi["rent_increase_needed_to_pass"] is None

    def test_non_positive_lift_skips_cost_reduction(self):
        # If target <= current, cost reduction can't help (no annual lift)
        program = {
            "renovation_cost_per_unit": 10_000,
            "current_monthly_rent": 1_000,
            "target_monthly_rent": 1_000,
        }
        roi = _recompute_roi(program, threshold_pct=15.0)
        assert roi["roi_passes"] is False
        assert roi["cost_reduction_needed_to_pass"] is None
        # rent_increase still defined (raise rent to make ROI clear)
        assert roi["rent_increase_needed_to_pass"] is not None


# ---------------------------------------------------------------------------
# _translate_federated_sanity_flag + catalog
# ---------------------------------------------------------------------------

class TestSanityFlagTranslation:
    def test_known_hard_flag_with_metric(self):
        f = _translate_federated_sanity_flag(
            "cap_rate_outside_4_7_band",
            {"going_in_cap": 0.03},
        )
        assert f["metric"] == "going_in_cap_rate"
        assert f["severity"] == "error"
        assert f["value"] == 0.03
        assert "3.00%" in f["message"]
        assert f["flag_id"] == "cap_rate_outside_4_7_band"

    def test_cap_rate_flag_picks_exit_when_only_exit_out_of_band(self):
        # Live demo_at_legacy_park case: going_in=6.00% (in band), exit=7.25% (out)
        f = _translate_federated_sanity_flag(
            "cap_rate_outside_4_7_band",
            {"going_in_cap": 0.06, "exit_cap": 0.0725},
        )
        assert f["metric"] == "exit_cap_rate"
        assert f["value"] == 0.0725
        assert "7.25%" in f["message"]

    def test_cap_rate_flag_picks_going_in_when_only_going_in_out(self):
        f = _translate_federated_sanity_flag(
            "cap_rate_outside_4_7_band",
            {"going_in_cap": 0.03, "exit_cap": 0.055},
        )
        assert f["metric"] == "going_in_cap_rate"
        assert f["value"] == 0.03

    def test_cap_rate_flag_prefers_exit_when_both_out(self):
        f = _translate_federated_sanity_flag(
            "cap_rate_outside_4_7_band",
            {"going_in_cap": 0.03, "exit_cap": 0.10},
        )
        assert f["metric"] == "exit_cap_rate"
        assert f["value"] == 0.10

    def test_known_warn_flag_with_metric(self):
        f = _translate_federated_sanity_flag(
            "dscr_below_1_20",
            {"min_dscr": 1.15},
        )
        assert f["severity"] == "warning"
        assert f["value"] == 1.15
        assert "1.15x" in f["message"]

    def test_unknown_flag_returns_passthrough(self):
        f = _translate_federated_sanity_flag("never_heard_of_it", {})
        assert f["metric"] == "unknown"
        assert f["severity"] == "warning"
        assert "never_heard_of_it" in f["message"]
        assert f["flag_id"] == "never_heard_of_it"

    def test_missing_metric_value_yields_na(self):
        f = _translate_federated_sanity_flag("dscr_below_1_20", {})
        assert f["value"] is None
        assert "n/a" in f["message"]

    def test_catalog_covers_all_documented_bands(self):
        """deal-memo-writer.md §5 lists 8 flag IDs. Catalog must cover them."""
        expected = {
            "cap_rate_outside_4_7_band",
            "dscr_below_1_10_likely_covenant_breach",
            "irr_outside_expected_band",
            "dscr_below_1_20",
            "dscr_below_1_30_lender_refi_floor",
            "irr_below_12pct_hurdle",
            "equity_multiple_low",
            "partnership_irr_below_lp_hurdle",
        }
        assert expected <= set(SANITY_FLAG_CATALOG.keys())


# ---------------------------------------------------------------------------
# synthesize_from_federated_artifacts — fail-soft + composition
# ---------------------------------------------------------------------------

class TestSynthesizeFailSoft:
    def test_all_artifacts_missing(self, tmp_path):
        # Build empty run dir
        run_id = "run_001"
        deal_root = tmp_path / "d"
        (deal_root / "outputs" / run_id / "costmodel").mkdir(parents=True)
        (deal_root / "outputs" / run_id / "underwriting").mkdir(parents=True)
        out = synthesize_from_federated_artifacts(deal_root, run_id)
        assert len(out["data_gaps"]) == 4  # all four expected sources missing
        assert out["binding_constraints"] == []
        assert out["recommended_changes"] == []
        assert "Insufficient federated artifacts" in out["headline"]

    def test_only_costmodel_present(self, tmp_path):
        deal_root = _build_deal_tree(
            tmp_path,
            renovation_programs=[
                {
                    "cohort_id": "beal", "unit_count": 47,
                    "renovation_cost_per_unit": 10_000,
                    "current_monthly_rent": 800,
                    "target_monthly_rent": 850,  # fails 15% gate
                },
            ],
            property_estimate={"risk_flags": []},
        )
        out = synthesize_from_federated_artifacts(deal_root, "run_001")
        # Two gaps for the two missing underwriting files
        assert len(out["data_gaps"]) == 2
        # ROI failure surfaces
        assert any("beal" in c for c in out["binding_constraints"])
        assert any("raise target rent" in r or "cut renovation cost" in r
                   for r in out["recommended_changes"])
        assert "ROI threshold" in out["headline"]


class TestSynthesizeFullComposition:
    def test_passing_deal(self, tmp_path):
        deal_root = _build_deal_tree(
            tmp_path,
            renovation_programs=[
                {
                    "cohort_id": "alpha", "unit_count": 30,
                    "renovation_cost_per_unit": 10_000,
                    "current_monthly_rent": 800,
                    "target_monthly_rent": 1_000,  # 24% ROI; passes
                },
            ],
            property_estimate={"risk_flags": []},
            deal_summary={"metrics": {"irr": {"levered_irr": 0.18}}},
            underwriting_provenance={
                "verdict": "pass", "sanity_flags": [],
                "metrics_extracted": {"levered_irr": 0.18, "going_in_cap": 0.06},
            },
        )
        out = synthesize_from_federated_artifacts(deal_root, "run_001")
        assert out["data_gaps"] == []
        assert out["binding_constraints"] == []
        assert out["recommended_changes"] == []
        assert "ready to advance" in out["headline"].lower()

    def test_underwriting_fail_with_sanity_flags(self, tmp_path):
        deal_root = _build_deal_tree(
            tmp_path,
            renovation_programs=[
                {
                    "cohort_id": "alpha", "unit_count": 30,
                    "renovation_cost_per_unit": 10_000,
                    "current_monthly_rent": 800,
                    "target_monthly_rent": 1_000,  # passes ROI
                },
            ],
            property_estimate={"risk_flags": [{"message": "Pre-1990 plumbing"}]},
            deal_summary={"metrics": {"irr": {"levered_irr": 0.4698}}},
            underwriting_provenance={
                "verdict": "fail",
                "sanity_flags": ["cap_rate_outside_4_7_band", "irr_outside_expected_band"],
                "metrics_extracted": {
                    "going_in_cap": 0.06, "levered_irr": 0.4698,
                },
            },
        )
        out = synthesize_from_federated_artifacts(deal_root, "run_001")
        # Sanity flags translated into structured constraints
        joined = " | ".join(out["binding_constraints"])
        assert "[ERROR]" in joined
        assert "Cap rate" in joined
        assert "Levered IRR" in joined
        # Risk flag surfaces
        assert any("Pre-1990" in c for c in out["binding_constraints"])
        # Cap-rate remediation present
        assert any("NOI" in r for r in out["recommended_changes"])

    def test_partial_roi_failure_lists_failing_cohorts(self, tmp_path):
        deal_root = _build_deal_tree(
            tmp_path,
            renovation_programs=[
                {"cohort_id": "alpha", "unit_count": 20,
                 "renovation_cost_per_unit": 10_000,
                 "current_monthly_rent": 800, "target_monthly_rent": 1_000},  # passes
                {"cohort_id": "bravo", "unit_count": 10,
                 "renovation_cost_per_unit": 10_000,
                 "current_monthly_rent": 800, "target_monthly_rent": 850},   # fails
            ],
            property_estimate={"risk_flags": []},
        )
        out = synthesize_from_federated_artifacts(deal_root, "run_001")
        # Only failing cohort surfaces in constraints
        joined = " | ".join(out["binding_constraints"])
        assert "bravo" in joined
        assert "alpha" not in joined
        assert "1 of 2" in out["headline"] or "ROI threshold" in out["headline"]


class TestRoiThresholdOverride:
    def test_higher_threshold_makes_more_failures(self, tmp_path):
        # 24% ROI passes default 15% but fails at 30%
        program = {
            "cohort_id": "alpha", "unit_count": 1,
            "renovation_cost_per_unit": 10_000,
            "current_monthly_rent": 800, "target_monthly_rent": 1_000,
        }
        deal_root = _build_deal_tree(
            tmp_path, renovation_programs=[program],
            property_estimate={"risk_flags": []},
        )
        out_default = synthesize_from_federated_artifacts(deal_root, "run_001")
        out_strict = synthesize_from_federated_artifacts(
            deal_root, "run_001", threshold_pct=30.0,
        )
        assert out_default["binding_constraints"] == []
        assert any("alpha" in c for c in out_strict["binding_constraints"])


# ---------------------------------------------------------------------------
# Live integration test against demo_at_legacy_park/run_002_federation
# ---------------------------------------------------------------------------

LIVE_DEAL_ROOT = Path(
    "/path/to/projects/multifamily-underwriting"
    "/runs/deals/demo_at_legacy_park"
)
LIVE_RUN_ID = "run_002_federation"


@pytest.mark.skipif(
    not (LIVE_DEAL_ROOT / "outputs" / LIVE_RUN_ID).exists(),
    reason="live federated fixture not present",
)
class TestLiveFederatedFixture:
    def test_synthesizes_against_real_artifacts(self):
        out = synthesize_from_federated_artifacts(LIVE_DEAL_ROOT, LIVE_RUN_ID)
        # Shape check
        assert set(out) == {"headline", "binding_constraints", "recommended_changes", "data_gaps"}
        assert isinstance(out["headline"], str) and out["headline"]
        assert isinstance(out["binding_constraints"], list)
        # The live fixture has cap_rate_outside_4_7_band + irr_outside_expected_band
        # — both HARD. Expect at least two ERROR-tagged binding constraints.
        errors = [c for c in out["binding_constraints"] if "[ERROR]" in c]
        assert len(errors) >= 2, f"expected >=2 ERROR constraints, got {errors}"
        # Cap rate remediation should suggest NOI verification
        assert any("NOI" in r for r in out["recommended_changes"])
