"""Cohort-collision regression tests for schema_mapper.

Wave 2 Task 2.1 of the federation splicer audit (2026-04-26).

The Legacy Park root cause: ``schema_mapper.build_renovation_programs`` used
to hardcode ``output_cohort = f"{target}_renovated"`` with no awareness of
existing canonical cohorts. Real Legacy Park canonicals already carry rent-
roll-derived ``*_renovated`` subtotals (``beal_renovated``, ``bradford_
renovated``, ``essex_renovated``) -- the splice silently overwrote them,
producing the 47% vs 27% IRR gap.

These tests enforce that:
  * Generated output_cohorts never collide with existing unit_cohorts.
  * Generated output_cohorts never collide with prior renovation_programs.
  * Merge raises on attempts to splice colliding cohorts.
  * _resolve_cohort_id prefers explicit beds/baths over fragile regex.
"""

import json
from pathlib import Path

import pytest

from plat_agent.models import DealInputs, UnitMixEntry, UnitTypeResult
from plat_agent.schema_mapper import (
    _resolve_cohort_id,
    build_renovation_programs,
    merge_renovation_programs,
)

# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------

SAMPLE_CANONICAL = Path(
    "/path/to/projects/multifamily-underwriting/runs/deals/"
    "demo_at_legacy_park/outputs/run_001/value_add_base_inputs_32m.json"
)


def _load_legacy_park() -> dict:
    with open(SAMPLE_CANONICAL) as f:
        return json.load(f)


def _passing_result(sqft, bedrooms, bathrooms, count) -> UnitTypeResult:
    return UnitTypeResult(
        sqft=sqft, bedrooms=bedrooms, bathrooms=bathrooms, count=count,
        cost_estimate_low=12000, cost_estimate_high=16800,
        roi_pct=18.0, roi_passes=True,
        renovation_program={
            "renovation_cost_per_unit": 14000,
            "rent_premium_monthly": 200,
            "downtime_days": 21,
            "strategy": "renovation",
            "start_month": "2026-06",
            "monthly_pace": 5,
        },
        roi_result={"roi_pct": 18.0, "clears_threshold": True},
    )


def _legacy_park_inputs(unit_mix: list[UnitMixEntry]) -> DealInputs:
    return DealInputs(
        property_id="demo_at_legacy_park",
        total_units=sum(u.count for u in unit_mix),
        start_month="2026-06",
        monthly_pace=5,
        unit_mix=unit_mix,
    )


# Cohort metadata mirroring the 5 active Legacy Park source cohorts.
# beds/baths are unknown in the canonical, so we attach them via a
# wrapper canonical when we need explicit-fields behavior.
SAMPLE_COHORTS = [
    # (cohort_id_hint, sqft, count, bedrooms, bathrooms)
    ("beal",       530.0, 47, 1, 1),
    ("bradford",   630.0, 57, 1, 1),
    ("centennial", 896.0, 19, 2, 2),
    ("essex",     1052.0, 31, 2, 2),
    ("kelly",      896.0, 25, 2, 2),
]


# ---------------------------------------------------------------------------
# build_renovation_programs collision behavior
# ---------------------------------------------------------------------------

def test_existing_renovated_cohort_triggers_postreno_rename():
    """Real Legacy Park canonical: 3 of 5 source cohorts already have
    ``*_renovated`` siblings. All 5 generated output_cohorts must be
    unique and must not collide with existing unit_cohorts.

    Because the canonical has no explicit beds/baths and the cohort_ids
    don't match the legacy ``\\d+B`` regex, target_cohort resolution falls
    through to the synthetic id. The collision concern is the OUTPUT
    cohort: even synthetic targets must produce non-colliding outputs
    when merged into a canonical that already uses a ``_renovated``
    suffix.
    """
    base = _load_legacy_park()
    # Force resolution to use cohort short-names by injecting beds/baths.
    enriched = {
        **base,
        "unit_cohorts": [
            {**c, "bedrooms": _bb_for(c["cohort_id"])[0],
             "bathrooms": _bb_for(c["cohort_id"])[1]}
            for c in base["unit_cohorts"]
        ],
    }

    unit_mix = [
        UnitMixEntry(sqft=sqft, bedrooms=br, bathrooms=ba, count=count,
                     current_monthly_rent=900, target_monthly_rent=1100)
        for (_cid, sqft, count, br, ba) in SAMPLE_COHORTS
    ]
    results = [
        _passing_result(sqft=sqft, bedrooms=br, bathrooms=ba, count=count)
        for (_cid, sqft, count, br, ba) in SAMPLE_COHORTS
    ]

    programs = build_renovation_programs(
        _legacy_park_inputs(unit_mix), results, base_deal_inputs=enriched,
    )

    assert len(programs) == 5
    output_ids = [p["output_cohort"] for p in programs]
    # All unique among themselves
    assert len(set(output_ids)) == 5, output_ids
    # None collide with existing unit_cohorts
    existing_ids = {c["cohort_id"] for c in enriched["unit_cohorts"]}
    for oid in output_ids:
        assert oid not in existing_ids, f"{oid} collides with unit_cohort"
    # Naming convention: every generated output uses _postreno
    for oid in output_ids:
        assert "_postreno" in oid, oid


def _bb_for(cohort_id: str) -> tuple[int, int]:
    """Helper: bedrooms/bathrooms inferred from cohort_id base name."""
    base = cohort_id.split("_")[0]
    table = {
        "beal":       (1, 1),
        "bradford":   (1, 1),
        "centennial": (2, 2),
        "essex":      (2, 2),
        "kelly":      (2, 2),
    }
    return table.get(base, (0, 0))


def test_no_existing_renovated_cohorts_uses_natural_postreno_name():
    """Clean canonical with no ``*_renovated`` cohorts -- output_cohorts
    are simply ``<target>_postreno`` without disambiguation suffixes."""
    clean_canonical = {
        "unit_cohorts": [
            {"cohort_id": "alpha", "sqft": 700, "unit_count": 10,
             "bedrooms": 1, "bathrooms": 1},
            {"cohort_id": "beta", "sqft": 900, "unit_count": 8,
             "bedrooms": 2, "bathrooms": 2},
        ],
    }
    unit_mix = [
        UnitMixEntry(sqft=700, bedrooms=1, bathrooms=1, count=10,
                     current_monthly_rent=900, target_monthly_rent=1100),
        UnitMixEntry(sqft=900, bedrooms=2, bathrooms=2, count=8,
                     current_monthly_rent=1100, target_monthly_rent=1400),
    ]
    results = [
        _passing_result(sqft=700, bedrooms=1, bathrooms=1, count=10),
        _passing_result(sqft=900, bedrooms=2, bathrooms=2, count=8),
    ]
    programs = build_renovation_programs(
        _legacy_park_inputs(unit_mix), results, base_deal_inputs=clean_canonical,
    )
    output_ids = [p["output_cohort"] for p in programs]
    assert "alpha_postreno" in output_ids
    assert "beta_postreno" in output_ids


# ---------------------------------------------------------------------------
# merge_renovation_programs collision guards
# ---------------------------------------------------------------------------

def test_merge_blocks_collision_with_unit_cohorts():
    """Splicing a program whose output_cohort equals an existing
    unit_cohort.cohort_id must raise -- this is the original Legacy Park
    bug acted out at the merge step."""
    base = {
        "unit_cohorts": [
            {"cohort_id": "beal", "sqft": 530, "unit_count": 47,
             "bedrooms": 1, "bathrooms": 1},
        ],
        "renovation_programs": [],
    }
    bad_program = {
        "program_id": "reno_beal",
        "target_cohort": "beal",
        "output_cohort": "beal",  # collides
        "renovation_cost_per_unit": 14000,
    }
    with pytest.raises(ValueError, match="collides with existing unit_cohorts"):
        merge_renovation_programs(base, [bad_program])


def test_merge_allows_multiple_programs_sharing_output_with_same_target():
    """Two programs targeting the same source cohort and producing the
    same output cohort is allowed (e.g. analyst-defined + auto-generated
    that converged on the same naming).

    Note: dedup-by-program_id means program_ids must differ; the shared
    field is output_cohort + target_cohort.
    """
    base = {
        "unit_cohorts": [
            {"cohort_id": "beal", "bedrooms": 1, "bathrooms": 1,
             "sqft": 530, "unit_count": 47},
        ],
        "renovation_programs": [
            {
                "program_id": "reno_beal_A",
                "target_cohort": "beal",
                "output_cohort": "beal_postreno_premium",
            },
        ],
    }
    new = [{
        "program_id": "reno_beal_B",
        "target_cohort": "beal",
        "output_cohort": "beal_postreno_premium",  # same target+output OK
    }]
    merged = merge_renovation_programs(base, new)
    assert len(merged["renovation_programs"]) == 2


def test_merge_blocks_multiple_programs_sharing_output_with_different_targets():
    """Two programs splicing into the same output_cohort from DIFFERENT
    source cohorts is a collision -- the engine cannot decide which
    upstream the output cohort inherits from."""
    base = {
        "unit_cohorts": [
            {"cohort_id": "beal", "bedrooms": 1, "bathrooms": 1,
             "sqft": 530, "unit_count": 47},
            {"cohort_id": "bradford", "bedrooms": 1, "bathrooms": 1,
             "sqft": 630, "unit_count": 57},
        ],
        "renovation_programs": [
            {
                "program_id": "reno_beal",
                "target_cohort": "beal",
                "output_cohort": "shared",
            },
        ],
    }
    new = [{
        "program_id": "reno_bradford",
        "target_cohort": "bradford",  # different source
        "output_cohort": "shared",     # same output -> collision
    }]
    with pytest.raises(ValueError, match="targeted by both"):
        merge_renovation_programs(base, new)


# ---------------------------------------------------------------------------
# _resolve_cohort_id behavior
# ---------------------------------------------------------------------------

def test_resolve_cohort_id_prefers_explicit_bedrooms():
    """When a cohort carries explicit bedrooms/bathrooms fields, those
    values are trusted directly -- the cohort_id regex is not invoked
    (so cohort_ids like "2BR_2BA" or "alpha" or "beta" all work)."""
    cohort_lookup = [
        # Explicit fields, weird id that the regex would NOT parse correctly
        {"cohort_id": "alpha", "bedrooms": 2, "bathrooms": 2,
         "sqft": 900, "unit_count": 8},
        {"cohort_id": "beta", "bedrooms": 1, "bathrooms": 1,
         "sqft": 700, "unit_count": 10},
    ]
    assert _resolve_cohort_id(2, 2, 900, 8, idx=0, cohort_lookup=cohort_lookup) == "alpha"
    assert _resolve_cohort_id(1, 1, 700, 10, idx=1, cohort_lookup=cohort_lookup) == "beta"


def test_resolve_cohort_id_raises_on_ambiguous():
    """Two candidate cohorts scoring identically on (count_diff, sqft_diff)
    -- silent first-match-wins was a HIGH-2 finding."""
    cohort_lookup = [
        {"cohort_id": "first", "bedrooms": 2, "bathrooms": 2,
         "sqft": 900, "unit_count": 10},
        {"cohort_id": "second", "bedrooms": 2, "bathrooms": 2,
         "sqft": 900, "unit_count": 10},
    ]
    with pytest.raises(ValueError, match="Ambiguous cohort match"):
        _resolve_cohort_id(2, 2, 900, 10, idx=0, cohort_lookup=cohort_lookup)


# ---------------------------------------------------------------------------
# End-to-end Legacy Park: build + merge + uniqueness invariant
# ---------------------------------------------------------------------------

def test_legacy_park_e2e_no_collision():
    pytest.skip(reason="requires a real-deal canonical sample that is not shipped in the public tree")
    """Full pipeline: load real Legacy Park canonical, build programs for
    all 5 source cohorts, merge into the canonical, assert that the
    union of unit_cohort ids and output_cohort ids has no duplicates."""
    base = _load_legacy_park()
    # Enrich with explicit beds/baths so the resolver returns named
    # cohort ids (otherwise the synthetic fallback kicks in and the
    # collision question is moot).
    enriched = {
        **base,
        "unit_cohorts": [
            {**c, "bedrooms": _bb_for(c["cohort_id"])[0],
             "bathrooms": _bb_for(c["cohort_id"])[1]}
            for c in base["unit_cohorts"]
        ],
    }

    unit_mix = [
        UnitMixEntry(sqft=sqft, bedrooms=br, bathrooms=ba, count=count,
                     current_monthly_rent=900, target_monthly_rent=1100)
        for (_cid, sqft, count, br, ba) in SAMPLE_COHORTS
    ]
    results = [
        _passing_result(sqft=sqft, bedrooms=br, bathrooms=ba, count=count)
        for (_cid, sqft, count, br, ba) in SAMPLE_COHORTS
    ]

    programs = build_renovation_programs(
        _legacy_park_inputs(unit_mix), results, base_deal_inputs=enriched,
    )
    assert len(programs) == 5

    merged = merge_renovation_programs(enriched, programs)

    cohort_ids = [c["cohort_id"] for c in merged["unit_cohorts"]]
    output_ids = [p["output_cohort"] for p in merged["renovation_programs"]]
    union = cohort_ids + output_ids

    # Critical: no overlap between any source cohort and any output cohort.
    assert len(set(union)) == len(union), (
        "cohort_id collision after splice: "
        f"duplicates = {[x for x in union if union.count(x) > 1]}"
    )

    # And specifically the three Legacy Park _renovated subtotals must
    # remain untouched.
    assert "beal_renovated" in cohort_ids
    assert "bradford_renovated" in cohort_ids
    assert "essex_renovated" in cohort_ids
    # And none of the new outputs equal them.
    assert "beal_renovated" not in output_ids
    assert "bradford_renovated" not in output_ids
    assert "essex_renovated" not in output_ids
