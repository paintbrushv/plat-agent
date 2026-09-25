"""Contract tests for `CompFinderRequest` vintage / class / renovation filters
and the canonical `cohort_key()` formatter.

Wave 6 Task 6.1 — see CONSOLIDATED_FIX_PLAN.md.
"""

from __future__ import annotations

import pytest

from plat_agent.contracts.domain.market_study import (
    COHORT_KEY_PATTERN,
    CompEntry,
    CompFinderRequest,
    CompFinderResponse,
    cohort_key,
)


# ---------------------------------------------------------------------------
# CompFinderRequest filter fields
# ---------------------------------------------------------------------------


def _base_request_kwargs() -> dict:
    return {
        "property_address": "123 Main St, Midland TX",
        "market": "midland_tx",
        "unit_mix_summary": [{"bedrooms": 2, "bathrooms": 1.5, "sqft": 977, "count": 24}],
    }


def test_comp_finder_request_with_vintage_filter() -> None:
    req = CompFinderRequest(**_base_request_kwargs(), year_built_min=1980, year_built_max=2010)
    assert req.year_built_min == 1980
    assert req.year_built_max == 2010


def test_comp_finder_request_with_class_tier() -> None:
    req = CompFinderRequest(**_base_request_kwargs(), class_tier="B")
    assert req.class_tier == "B"

    with pytest.raises(ValueError):
        CompFinderRequest(**_base_request_kwargs(), class_tier="D")  # type: ignore[arg-type]


def test_comp_finder_request_with_renovation_status() -> None:
    req = CompFinderRequest(**_base_request_kwargs(), renovation_status="light")
    assert req.renovation_status == "light"

    with pytest.raises(ValueError):
        CompFinderRequest(  # type: ignore[arg-type]
            **_base_request_kwargs(),
            renovation_status="full",
        )


def test_comp_finder_request_no_filters_default_none() -> None:
    """Backwards compat: every new filter defaults to None when omitted."""
    req = CompFinderRequest(**_base_request_kwargs())
    assert req.coverage_signal is None
    assert req.year_built_min is None
    assert req.year_built_max is None
    assert req.class_tier is None
    assert req.renovation_status is None
    # Pre-existing fields still defaulted.
    assert req.radius_miles == 2.0
    assert req.max_age_months == 18


# ---------------------------------------------------------------------------
# cohort_key() canonical formatter
# ---------------------------------------------------------------------------


def test_cohort_key_canonical_format() -> None:
    """Exact format string contract."""
    assert cohort_key(2, 1.5, 977) == "2BR_1.5BA_977sf"
    assert cohort_key(1, 1.0, 650) == "1BR_1.0BA_650sf"
    assert cohort_key(3, 2.0, 1250) == "3BR_2.0BA_1250sf"


def test_cohort_key_handles_int_and_float_sqft() -> None:
    """sqft passed as float should coerce to int (no decimal in key)."""
    # int sqft
    assert cohort_key(2, 1.0, 850) == "2BR_1.0BA_850sf"
    # float sqft truncates via int()
    assert cohort_key(2, 1.0, int(850.7)) == "2BR_1.0BA_850sf"
    # Direct float passes through int() -> truncate
    assert cohort_key(2, 1.0, int(977.99)) == "2BR_1.0BA_977sf"


def test_cohort_key_handles_half_baths() -> None:
    """Half- and quarter-baths must round to 1 decimal place."""
    assert cohort_key(2, 1.5, 977) == "2BR_1.5BA_977sf"
    assert cohort_key(2, 2.5, 1100) == "2BR_2.5BA_1100sf"
    assert cohort_key(1, 1.0, 600) == "1BR_1.0BA_600sf"


def test_cohort_key_matches_pattern_regex() -> None:
    """Outputs of cohort_key() must satisfy the public regex contract."""
    import re

    pat = re.compile(COHORT_KEY_PATTERN)
    for args in [(1, 1.0, 600), (2, 1.5, 977), (3, 2.0, 1250), (4, 3.5, 2400)]:
        key = cohort_key(*args)
        assert pat.match(key), f"cohort_key{args} -> {key!r} did not match {COHORT_KEY_PATTERN}"


def test_comp_finder_response_rejects_malformed_cohort_key() -> None:
    """CompFinderResponse should refuse non-canonical cohort keys."""
    entry = CompEntry(
        property_name="Test Prop",
        bedrooms=2,
        bathrooms=1.5,
        sqft=977,
        asking_rent=1500.0,
        rent_per_sqft=1.53,
        source="test",
    )

    # Canonical key passes.
    CompFinderResponse(
        comps_by_cohort={cohort_key(2, 1.5, 977): [entry]},
        comps_relative="comps.json",
    )

    # Inline-interpolated / lowercase / odd format must fail.
    with pytest.raises(ValueError):
        CompFinderResponse(
            comps_by_cohort={"2br_1.5ba_977sf": [entry]},
            comps_relative="comps.json",
        )
    with pytest.raises(ValueError):
        CompFinderResponse(
            comps_by_cohort={"2BR/1.5BA/977sf": [entry]},
            comps_relative="comps.json",
        )
