"""Tests for plat_agent.rent_validation.validate_target_rents."""

import pytest

from plat_agent.contracts.domain.market_study import (
    CompEntry,
    ProposedRent,
    RentValidationRequest,
    cohort_key,
)
from plat_agent.rent_validation import (
    COMP_COUNT_HIGH_CONFIDENCE,
    MIN_COMPS_FOR_VERDICT,
    validate_target_rents,
)


def _comp(rent: float, br=2, ba=1.0, sqft=850.0, name="C") -> CompEntry:
    return CompEntry(
        property_name=name, bedrooms=br, bathrooms=ba, sqft=sqft,
        asking_rent=rent, rent_per_sqft=round(rent / sqft, 2),
        source="test",
    )


def _request(proposed_rent: float, comps: list[CompEntry], br=2, ba=1.0, sqft=850.0):
    cohort = {cohort_key(br, ba, int(sqft)): comps}
    return RentValidationRequest(
        proposed_rents=[ProposedRent(
            bedrooms=br, bathrooms=ba, sqft=sqft,
            proposed_target_monthly_rent=proposed_rent,
        )],
        comps_by_cohort=cohort,
    )


class TestVerdictBands:
    def test_within_band(self):
        # Comps: 900, 1000, 1100, 1200, 1300 → p25=1000, p75=1200
        comps = [_comp(r) for r in [900, 1000, 1100, 1200, 1300]]
        resp = validate_target_rents(_request(1100, comps))
        f = resp.findings[0]
        assert f.verdict == "within_band"
        assert f.comp_count == 5
        assert f.comp_p25 == 1000
        assert f.comp_p75 == 1200

    def test_above_band(self):
        comps = [_comp(r) for r in [900, 1000, 1100, 1200, 1300]]
        resp = validate_target_rents(_request(1400, comps))
        f = resp.findings[0]
        assert f.verdict == "above_band"

    def test_below_band(self):
        comps = [_comp(r) for r in [900, 1000, 1100, 1200, 1300]]
        resp = validate_target_rents(_request(800, comps))
        f = resp.findings[0]
        assert f.verdict == "below_band"

    def test_at_p25_is_within(self):
        comps = [_comp(r) for r in [900, 1000, 1100, 1200, 1300]]
        resp = validate_target_rents(_request(1000, comps))
        assert resp.findings[0].verdict == "within_band"

    def test_at_p75_is_within(self):
        comps = [_comp(r) for r in [900, 1000, 1100, 1200, 1300]]
        resp = validate_target_rents(_request(1200, comps))
        assert resp.findings[0].verdict == "within_band"


class TestInsufficientComps:
    def test_below_min_returns_no_verdict(self):
        comps = [_comp(1000), _comp(1100)]  # 2 comps < MIN=3
        resp = validate_target_rents(_request(1050, comps))
        f = resp.findings[0]
        assert f.verdict == "insufficient_comps"
        assert f.confidence == "none"
        assert f.comp_p25 is None

    def test_zero_comps_returns_no_verdict(self):
        # Empty cohort dict — no cohort match, no fallback
        req = RentValidationRequest(
            proposed_rents=[ProposedRent(
                bedrooms=2, bathrooms=1.0, sqft=850.0,
                proposed_target_monthly_rent=1100,
            )],
            comps_by_cohort={},
        )
        resp = validate_target_rents(req)
        assert resp.findings[0].verdict == "insufficient_comps"
        assert resp.findings[0].comp_count == 0

    def test_at_min_returns_verdict(self):
        comps = [_comp(1000), _comp(1100), _comp(1200)]  # exactly 3
        resp = validate_target_rents(_request(1100, comps))
        assert resp.findings[0].verdict == "within_band"


class TestConfidence:
    def test_high_confidence_when_many_comps(self):
        comps = [_comp(1000 + i * 50) for i in range(COMP_COUNT_HIGH_CONFIDENCE)]
        resp = validate_target_rents(_request(1100, comps))
        assert resp.findings[0].confidence == "high"

    def test_medium_confidence_when_few_comps(self):
        # 3 comps: medium (between MIN and HIGH thresholds)
        comps = [_comp(1000), _comp(1100), _comp(1200)]
        resp = validate_target_rents(_request(1100, comps))
        assert resp.findings[0].confidence == "medium"


class TestCohortMatching:
    def test_exact_key_match(self):
        comps = [_comp(r, br=2, ba=1.0, sqft=850) for r in [1000, 1100, 1200]]
        # cohort key built from request matches
        resp = validate_target_rents(_request(1100, comps, br=2, ba=1.0, sqft=850))
        assert resp.findings[0].comp_count == 3

    def test_fallback_when_sqft_differs(self):
        """Request asks for 2BR/1BA/900sf, comps live under 2BR/1BA/850sf."""
        comps = [_comp(r, br=2, ba=1.0, sqft=850) for r in [1000, 1100, 1200]]
        cohort = {cohort_key(2, 1.0, 850): comps}
        req = RentValidationRequest(
            proposed_rents=[ProposedRent(
                bedrooms=2, bathrooms=1.0, sqft=900.0,
                proposed_target_monthly_rent=1100,
            )],
            comps_by_cohort=cohort,
        )
        resp = validate_target_rents(req)
        # Fallback by (bedrooms, bathrooms) should pool the 3 comps
        assert resp.findings[0].comp_count == 3
        assert resp.findings[0].verdict == "within_band"

    def test_fallback_filters_by_bedrooms(self):
        """A 1BR request must NOT pool 2BR comps even by fallback."""
        cohort = {
            cohort_key(2, 1.0, 850): [_comp(r, br=2) for r in [1100, 1200, 1300]],
        }
        req = RentValidationRequest(
            proposed_rents=[ProposedRent(
                bedrooms=1, bathrooms=1.0, sqft=600.0,
                proposed_target_monthly_rent=900,
            )],
            comps_by_cohort=cohort,
        )
        resp = validate_target_rents(req)
        assert resp.findings[0].comp_count == 0
        assert resp.findings[0].verdict == "insufficient_comps"


class TestMultiCohort:
    def test_independent_findings_per_cohort(self):
        cohort = {
            cohort_key(1, 1.0, 600): [_comp(r, br=1, sqft=600) for r in [800, 900, 1000]],
            cohort_key(2, 1.0, 850): [_comp(r, br=2, sqft=850) for r in [1100, 1200, 1300]],
        }
        req = RentValidationRequest(
            proposed_rents=[
                ProposedRent(bedrooms=1, bathrooms=1.0, sqft=600,
                             proposed_target_monthly_rent=900),  # within
                ProposedRent(bedrooms=2, bathrooms=1.0, sqft=850,
                             proposed_target_monthly_rent=1500),  # above
            ],
            comps_by_cohort=cohort,
        )
        resp = validate_target_rents(req)
        assert len(resp.findings) == 2
        assert resp.findings[0].verdict == "within_band"
        assert resp.findings[1].verdict == "above_band"
        assert "above p75" in resp.summary


class TestSummaryAndShape:
    def test_finding_has_required_fields(self):
        comps = [_comp(r) for r in [1000, 1100, 1200]]
        f = validate_target_rents(_request(1100, comps)).findings[0]
        assert f.cohort_key == "2BR_1.0BA_850sf"
        assert f.proposed_rent == 1100
        assert isinstance(f.message, str) and f.message

    def test_empty_request_summary(self):
        req = RentValidationRequest(proposed_rents=[], comps_by_cohort={})
        resp = validate_target_rents(req)
        assert resp.findings == []
        assert "No proposed rents" in resp.summary
