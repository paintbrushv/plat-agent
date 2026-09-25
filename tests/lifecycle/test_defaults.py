from plat_agent.lifecycle.defaults import (
    CONTRACT_VERSION,
    DEFAULT_LEVERAGE_V1,
    LEVERAGE_SOURCE_V1_HARDCODED,
    JUDGMENT_ENGINE_V1,
    DEFAULT_TTL_DAYS,
    VALIDATION_THRESHOLDS,
    derive_recommendation,
)


def test_contract_version_is_v1() -> None:
    assert CONTRACT_VERSION == "1.0.0"


def test_v1_leverage_defaults() -> None:
    assert DEFAULT_LEVERAGE_V1["ltv"] == 0.65
    assert DEFAULT_LEVERAGE_V1["rate"] == 0.0575
    assert DEFAULT_LEVERAGE_V1["amort_years"] == 30
    assert DEFAULT_LEVERAGE_V1["io_months"] == 12
    assert DEFAULT_LEVERAGE_V1["source"] == LEVERAGE_SOURCE_V1_HARDCODED


def test_judgment_engine_v1_constant() -> None:
    assert JUDGMENT_ENGINE_V1 == "deterministic_v1"


def test_validation_thresholds_yellow_red() -> None:
    assert VALIDATION_THRESHOLDS["yellow_pct"] == 0.15
    assert VALIDATION_THRESHOLDS["red_pct"] == 0.25


def test_default_ttl_comps_7_days() -> None:
    assert DEFAULT_TTL_DAYS["comps"] == 7
    assert DEFAULT_TTL_DAYS.get("judgment") is None  # no default TTL


def test_recommendation_proceed() -> None:
    """V1.5: PROCEED requires CoC >= 7% AND min_dscr >= 1.20 AND no blockers."""
    rec, conf = derive_recommendation(
        levered_irr=0.15, min_dscr=1.25, blocker_count=0,
        comps_unavailable=False, judgment_confidence=0.85,
        leverage_source="agency_default",
        cash_on_cash_year_1=0.08,
    )
    assert rec == "PROCEED"
    assert 0.0 <= conf <= 1.0


def test_recommendation_needs_data_on_blocker() -> None:
    rec, _ = derive_recommendation(
        levered_irr=0.15, min_dscr=1.25, blocker_count=1,
        comps_unavailable=False, judgment_confidence=0.85,
        leverage_source="agency_default",
        cash_on_cash_year_1=0.08,
    )
    assert rec == "NEEDS_DATA"


def test_recommendation_needs_data_on_no_comps() -> None:
    rec, _ = derive_recommendation(
        levered_irr=0.15, min_dscr=1.25, blocker_count=0,
        comps_unavailable=True, judgment_confidence=0.85,
        leverage_source="agency_default",
        cash_on_cash_year_1=0.08,
    )
    assert rec == "NEEDS_DATA"


def test_recommendation_decline_on_low_coc() -> None:
    """V1.5: CoC below 7% target → DECLINE."""
    rec, _ = derive_recommendation(
        levered_irr=0.15, min_dscr=1.25, blocker_count=0,
        comps_unavailable=False, judgment_confidence=0.85,
        leverage_source="agency_default",
        cash_on_cash_year_1=0.04,  # 4% < 7% target
    )
    assert rec == "DECLINE"


def test_recommendation_decline_on_low_dscr() -> None:
    rec, _ = derive_recommendation(
        levered_irr=0.15, min_dscr=1.0, blocker_count=0,
        comps_unavailable=False, judgment_confidence=0.85,
        leverage_source="agency_default",
        cash_on_cash_year_1=0.08,
    )
    assert rec == "DECLINE"


def test_recommendation_needs_data_when_coc_missing() -> None:
    """V1.5: missing cash_on_cash_year_1 (engine didn't surface) → NEEDS_DATA."""
    rec, _ = derive_recommendation(
        levered_irr=0.15, min_dscr=1.25, blocker_count=0,
        comps_unavailable=False, judgment_confidence=0.85,
        leverage_source="agency_default",
        cash_on_cash_year_1=None,
    )
    assert rec == "NEEDS_DATA"


def test_recommendation_confidence_clamped_for_v1_hardcoded_leverage() -> None:
    # Per §6.A.1: hardcoded leverage caps recommendation_confidence at 0.6
    _, conf = derive_recommendation(
        levered_irr=0.15, min_dscr=1.25, blocker_count=0,
        comps_unavailable=False, judgment_confidence=0.99,
        leverage_source=LEVERAGE_SOURCE_V1_HARDCODED,
        cash_on_cash_year_1=0.08,
    )
    assert conf <= 0.6


def test_recommendation_confidence_capped_for_agency_dscr_leverage() -> None:
    """V1.5: agency_dscr_constrained caps recommendation_confidence at 0.85."""
    from plat_agent.lifecycle.defaults import LEVERAGE_SOURCE_AGENCY_DSCR
    _, conf = derive_recommendation(
        levered_irr=0.15, min_dscr=1.25, blocker_count=0,
        comps_unavailable=False, judgment_confidence=0.99,
        leverage_source=LEVERAGE_SOURCE_AGENCY_DSCR,
        cash_on_cash_year_1=0.10,  # well above 7%
    )
    assert conf <= 0.85


def test_recommendation_when_metrics_absent() -> None:
    # Per §2.3 edge case: if underwriting fails, recommendation = NEEDS_DATA
    # and recommendation_confidence = judgment_confidence (no metrics-headroom factor)
    rec, conf = derive_recommendation(
        levered_irr=None, min_dscr=None, blocker_count=0,
        comps_unavailable=False, judgment_confidence=0.7,
        leverage_source="agency_default",
        cash_on_cash_year_1=None,
    )
    assert rec == "NEEDS_DATA"
    assert conf == 0.7
