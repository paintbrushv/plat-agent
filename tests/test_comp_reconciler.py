"""Wave 6 Task 6.2 — comp-reconciler tests.

Covers:
  - `comp_evidence_missing` HARD gate logic in market-context-orchestrator.
  - `comp_disagreement_<cohort>` advisory flag (>10% divergence).
  - In-tolerance divergence emits no flag.
  - `CompReconciliationResult` payload shape.

Per Wave 6 Q2 (CONSOLIDATED_FIX_PLAN.md): disagreement is advisory only —
HARD warn but allow analyst override. The only HARD block in this domain
is `comp_evidence_missing`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from plat_agent.comp_reconciler import (
    check_comp_evidence_gate,
    collect_premium_cohort_ids,
    compute_comp_reconciliation,
)
from plat_agent.contracts.domain.market_study import (
    CompReconciliationEntry,
    CompReconciliationResult,
    cohort_key,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _canonical_with_premium(
    *,
    cohort_id: str = "essex",
    bedrooms: int = 2,
    bathrooms: float = 1.0,
    sqft: int = 977,
    market_rent: float = 1200.0,
    rent_premium_monthly: float = 200.0,
) -> dict:
    """A minimal canonical dict carrying one cohort with a non-zero premium."""
    return {
        "unit_cohorts": [
            {
                "cohort_id": cohort_id,
                "bedrooms": bedrooms,
                "bathrooms": bathrooms,
                "sqft": sqft,
                "market_rent": market_rent,
            }
        ],
        "market_rent_curve": {
            cohort_id: [market_rent, market_rent, market_rent],
        },
        "renovation_programs": [
            {
                "program_id": "p1",
                "target_cohort": cohort_id,
                "rent_premium_monthly": rent_premium_monthly,
            }
        ],
        "revenue_programs": [],
    }


def _comps_payload(
    *,
    cohort_id: str = "essex",
    bedrooms: int = 2,
    bathrooms: float = 1.0,
    sqft: int = 977,
    rents: tuple[float, ...] = (1280.0, 1280.0, 1280.0),
) -> dict:
    """A comps.json-shaped dict matching `CompFinderResponse`."""
    return {
        "comps_by_cohort": {
            cohort_key(bedrooms, bathrooms, sqft): [
                {
                    "property_name": f"Comp {i}",
                    "bedrooms": bedrooms,
                    "bathrooms": bathrooms,
                    "sqft": sqft,
                    "asking_rent": float(r),
                    "rent_per_sqft": float(r) / sqft,
                    "source": "test_fixture",
                }
                for i, r in enumerate(rents)
            ],
        },
        "comps_relative": "market_study/comps.json",
        "methodology_notes": [],
    }


# ---------------------------------------------------------------------------
# 1. Comp-evidence gate (HARD block)
# ---------------------------------------------------------------------------


def test_comp_evidence_missing_blocks_advance(tmp_path: Path) -> None:
    """Canonical has rent_premium > 0, comps.json missing → block=True with
    cohort_ids_lacking_evidence populated. The orchestrator translates this
    into BridgeError(code='comp_evidence_missing', recoverable=True)."""
    canonical = _canonical_with_premium(cohort_id="essex", rent_premium_monthly=200.0)
    comps_path = tmp_path / "outputs" / "run_001" / "market_study" / "comps.json"
    # File intentionally NOT created.

    decision = check_comp_evidence_gate(canonical, comps_path)

    assert decision["block"] is True
    assert decision["comps_json_exists"] is False
    assert decision["premium_cohorts"] == ["essex"]
    assert decision["cohort_ids_lacking_evidence"] == ["essex"]
    assert decision["reconciliation_required"] is False


def test_comp_evidence_missing_with_revenue_program_premium(tmp_path: Path) -> None:
    """Premium encoded ONLY on revenue_programs (analyst-side encoding) —
    gate must still block when comps.json is missing.

    Uses the real canonical shape: program_id starts with ``reno_premium_``,
    cohort lives on ``eligible_units``, premium amount lives on ``price_value``.
    """
    canonical = {
        "unit_cohorts": [
            {"cohort_id": "beal", "bedrooms": 0, "bathrooms": 1.0, "sqft": 530, "market_rent": 1100.0}
        ],
        "market_rent_curve": {"beal": [1100.0]},
        "renovation_programs": [],
        "revenue_programs": [
            {
                "program_id": "reno_premium_beal",
                "eligible_units": "beal",
                "price_value": 130.0,
                "pricing_type": "$/unit",
            },
        ],
    }
    comps_path = tmp_path / "comps.json"
    decision = check_comp_evidence_gate(canonical, comps_path)
    assert decision["block"] is True
    assert "beal" in decision["cohort_ids_lacking_evidence"]


# ---------------------------------------------------------------------------
# 2. Comp-evidence present → advance, no block
# ---------------------------------------------------------------------------


def test_comp_evidence_present_advances(tmp_path: Path) -> None:
    """comps.json exists → no block, reconciliation_required=True so the
    orchestrator emits the comp_reconciliation_required sanity flag.
    """
    canonical = _canonical_with_premium()
    # Write the cohort-grouped structured file (the actual federation contract
    # output) with at least one populated cohort. The legacy comps.json path
    # is also accepted as a fallback but only if it has populated comps_by_cohort.
    comps_path = tmp_path / "comps.json"
    cohort_grouped_path = tmp_path / "comps_cohort_grouped.json"
    cohort_grouped_path.write_text(
        '{"comps_by_cohort": {"2BR_1BA_977sf": '
        '[{"property_name": "X", "bedrooms": 2, "bathrooms": 1.0, "sqft": 977, '
        '"asking_rent": 1300}]}}'
    )

    decision = check_comp_evidence_gate(canonical, comps_path)

    assert decision["block"] is False
    assert decision["comps_json_exists"] is True
    assert decision["premium_cohorts"] == ["essex"]
    assert decision["reconciliation_required"] is True
    assert decision["cohort_ids_lacking_evidence"] == []


def test_no_premium_cohorts_skips_gate(tmp_path: Path) -> None:
    """No cohort carries a non-zero rent premium → gate is trivially
    satisfied; no comp evidence required, no reconciliation needed.
    """
    canonical = _canonical_with_premium(rent_premium_monthly=0.0)
    comps_path = tmp_path / "comps.json"
    decision = check_comp_evidence_gate(canonical, comps_path)
    assert decision["block"] is False
    assert decision["premium_cohorts"] == []
    assert decision["reconciliation_required"] is False


# ---------------------------------------------------------------------------
# 3. Disagreement classification — within tolerance vs >10%
# ---------------------------------------------------------------------------


def test_disagreement_within_10pct_no_flag() -> None:
    """canonical_market_rent=1200, comp_p50=1280 → divergence ≈ 6.67% (<= 10%)
    → no `comp_disagreement_<cohort>` flag."""
    canonical = _canonical_with_premium(market_rent=1200.0)
    comps = _comps_payload(rents=(1280.0, 1280.0, 1280.0))

    result, flags = compute_comp_reconciliation(canonical, comps)

    assert flags == [], f"expected no advisory flags, got {flags}"
    assert len(result.market_rent_calibration) == 1
    entry = result.market_rent_calibration[0]
    assert entry.cohort_id == "essex"
    assert entry.canonical_market_rent == pytest.approx(1200.0)
    assert entry.comp_p50_rent == pytest.approx(1280.0)
    # 6.6666...%
    assert entry.divergence_pct == pytest.approx((80.0 / 1200.0) * 100.0)
    assert abs(entry.divergence_pct) <= result.divergence_threshold_pct
    assert entry.recommendation == "within tolerance"


def test_disagreement_above_10pct_flags_advisory() -> None:
    """canonical_market_rent=1200, comp_p50=1400 → divergence ≈ 16.67% (>10%)
    → `comp_disagreement_essex` flag emitted; result remains advisory_only.

    Critical assertion: the function returns a result + flag list — it
    does NOT raise, does NOT block. Per Wave 6 Q2 (advisory mode).
    """
    canonical = _canonical_with_premium(market_rent=1200.0)
    comps = _comps_payload(rents=(1400.0, 1400.0, 1400.0))

    result, flags = compute_comp_reconciliation(canonical, comps)

    assert flags == ["comp_disagreement_essex"]
    assert result.advisory_only is True
    assert len(result.market_rent_calibration) == 1
    entry = result.market_rent_calibration[0]
    # 200/1200 = 16.6666...%
    assert entry.divergence_pct == pytest.approx((200.0 / 1200.0) * 100.0)
    assert entry.divergence_pct > result.divergence_threshold_pct
    assert entry.recommendation == "review and consider upward adjustment"
    assert entry.comp_count == 3


def test_negative_divergence_above_threshold_flags_downward() -> None:
    """Comp p50 < canonical by >10% → 'review and consider downward
    adjustment' recommendation, flag still emitted.
    """
    canonical = _canonical_with_premium(market_rent=1500.0)
    comps = _comps_payload(rents=(1200.0, 1200.0, 1200.0))

    result, flags = compute_comp_reconciliation(canonical, comps)

    assert flags == ["comp_disagreement_essex"]
    entry = result.market_rent_calibration[0]
    assert entry.divergence_pct < -result.divergence_threshold_pct
    assert entry.recommendation == "review and consider downward adjustment"


# ---------------------------------------------------------------------------
# 4. CompReconciliationResult payload shape
# ---------------------------------------------------------------------------


def test_calibration_payload_shape() -> None:
    """The full payload must match the `CompReconciliationResult` Pydantic
    model — round-trip through model_dump_json/model_validate_json.
    """
    canonical = _canonical_with_premium(market_rent=1200.0)
    comps = _comps_payload(rents=(1400.0, 1400.0, 1400.0))

    result, _ = compute_comp_reconciliation(canonical, comps)

    # Type check.
    assert isinstance(result, CompReconciliationResult)
    assert all(isinstance(e, CompReconciliationEntry) for e in result.market_rent_calibration)

    # Shape contract.
    payload = result.model_dump(mode="json")
    assert set(payload.keys()) == {
        "market_rent_calibration",
        "divergence_threshold_pct",
        "advisory_only",
        "cohorts_lacking_comp_evidence",
    }
    assert payload["advisory_only"] is True
    assert payload["divergence_threshold_pct"] == 10.0
    assert isinstance(payload["market_rent_calibration"], list)
    assert payload["cohorts_lacking_comp_evidence"] == []

    entry = payload["market_rent_calibration"][0]
    assert set(entry.keys()) == {
        "cohort_id",
        "canonical_market_rent",
        "comp_p50_rent",
        "comp_count",
        "divergence_pct",
        "recommendation",
    }
    assert entry["cohort_id"] == "essex"
    assert isinstance(entry["canonical_market_rent"], (int, float))
    assert isinstance(entry["comp_p50_rent"], (int, float))
    assert isinstance(entry["comp_count"], int)
    assert isinstance(entry["divergence_pct"], (int, float))
    assert isinstance(entry["recommendation"], str)

    # Round-trip
    rebuilt = CompReconciliationResult.model_validate_json(result.model_dump_json())
    assert rebuilt == result


def test_cohorts_lacking_comp_evidence_in_payload() -> None:
    """When a cohort has rent_premium > 0 but no matching comp set in
    comps.json (different cohort_key), the cohort_id is recorded in
    `cohorts_lacking_comp_evidence` — surfaced for analyst follow-up.
    The function does NOT raise on the missing-match.
    """
    canonical = _canonical_with_premium(
        cohort_id="essex",
        bedrooms=2,
        bathrooms=1.0,
        sqft=977,
        market_rent=1200.0,
    )
    # comp set keyed under a DIFFERENT cohort_key (3BR / different sqft).
    comps = _comps_payload(bedrooms=3, bathrooms=2.0, sqft=1318, rents=(1500.0,))

    result, flags = compute_comp_reconciliation(canonical, comps)

    assert "essex" in result.cohorts_lacking_comp_evidence
    assert result.market_rent_calibration == []
    # No advisory flag on a cohort we couldn't match (nothing to disagree with).
    assert flags == []


# ---------------------------------------------------------------------------
# 5. Premium-cohort detection helper (defensive coverage)
# ---------------------------------------------------------------------------


def test_collect_premium_cohorts_dedups_across_paths() -> None:
    """Same cohort encoded both ways → returned once.

    Real canonical shape: revenue_programs[i].program_id starts with
    ``reno_premium_``; eligible_units carries the cohort; price_value carries
    the premium.
    """
    canonical = {
        "unit_cohorts": [{"cohort_id": "essex", "bedrooms": 2, "bathrooms": 1.0, "sqft": 977}],
        "revenue_programs": [
            {
                "program_id": "reno_premium_essex",
                "eligible_units": "essex",
                "price_value": 200.0,
                "pricing_type": "$/unit",
            }
        ],
        "renovation_programs": [
            {"program_id": "p1", "target_cohort": "essex", "rent_premium_monthly": 200.0}
        ],
    }
    assert collect_premium_cohort_ids(canonical) == ["essex"]


def test_collect_premium_cohorts_ignores_zero_and_missing() -> None:
    """Zero / null rent_premium → cohort excluded."""
    canonical = {
        "renovation_programs": [
            {"program_id": "p1", "target_cohort": "a", "rent_premium_monthly": 0.0},
            {"program_id": "p2", "target_cohort": "b", "rent_premium_monthly": None},
            {"program_id": "p3", "target_cohort": "c", "rent_premium_monthly": 50.0},
        ],
        "revenue_programs": [
            {
                "program_id": "reno_premium_d",
                "eligible_units": "d",
                "price_value": 0.0,
                "pricing_type": "$/unit",
            },
            {
                "program_id": "reno_premium_e",
                "eligible_units": "e",
                "price_value": 75.0,
                "pricing_type": "$/unit",
            },
            # Non-reno program (e.g. other_income) MUST be ignored even if
            # price_value > 0 and eligible_units != "ALL".
            {
                "program_id": "other_income_stable",
                "eligible_units": "ALL",
                "price_value": 42_000.0,
                "pricing_type": "$/asset",
            },
        ],
    }
    assert collect_premium_cohort_ids(canonical) == ["c", "e"]


def test_collect_premium_cohorts_smoke_real_legacy_park_canonical() -> None:
    """Defense-in-depth: load the actual Legacy Park analyst canonical and assert
    the gate identifies the 5 reno_premium_* cohorts. This is the test that
    would have caught the prior dict-key-iteration bug, since the real schema
    uses program_id strings (not dict keys named ``reno_premium_*``).
    """
    import json
    from pathlib import Path as _Path

    legacy_park_path = _Path(
        "/path/to/projects/multifamily-underwriting/runs/deals/"
        "demo_at_legacy_park/outputs/run_001/value_add_base_inputs_32m.json"
    )
    if not legacy_park_path.exists():
        pytest.skip(
            "Legacy Park canonical not present (sibling repo not co-located). "
            "Run with multifamily-underwriting checked out alongside plat-agent."
        )
    canonical = json.loads(legacy_park_path.read_text())
    cohorts = collect_premium_cohort_ids(canonical)
    expected = {"beal", "bradford", "centennial", "essex", "kelly"}
    assert set(cohorts) == expected, (
        f"Expected the 5 Legacy Park reno_premium_* cohorts, got {cohorts}. "
        f"This is the smoke test for the dict-key-iteration bug."
    )
