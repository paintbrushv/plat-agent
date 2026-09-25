"""Map plat-agent renovation results to the canonical deal schema v0.1.

The underwriting engine (multifamily-underwriting) expects renovation_programs
in a specific format with fields that plat-costmodel's bridge doesn't produce
(program_id, program_name, target_cohort, output_cohort, strategy enum).

This module bridges that gap:
  plat-costmodel bridge output -> canonical renovation_programs array

It also handles the strategy enum mapping:
  plat-costmodel: "renovation"
  underwriting:   "on_turnover" | "proactive"

Output cohort naming
--------------------
Federation-emitted output_cohort IDs use the ``_postreno`` suffix so they do
NOT collide with rent-roll-derived ``_renovated`` subtotals that already
appear in many canonical deal inputs (e.g. Legacy Park has ``beal_renovated``,
``bradford_renovated``, ``essex_renovated`` as actual occupancy cohorts).
"""

import hashlib
import re

from .models import DealInputs, UnitTypeResult


def _make_output_cohort(target: str, program_id: str, taken: set[str]) -> str:
    """Pick a deterministic, non-colliding output_cohort name.

    Tries a small ladder of candidates derived from the target cohort and
    program_id; the first one that does not appear in ``taken`` wins.
    """
    short_pid = program_id.split("_")[-1] if "_" in program_id else program_id
    candidates = [
        f"{target}_postreno",
        f"{target}_postreno_{short_pid}",
        f"{target}_postreno_v2",
        f"{target}_postreno_v3",
    ]
    for c in candidates:
        if c not in taken:
            return c
    raise ValueError(
        f"Cannot find non-colliding output_cohort for target={target} "
        f"taken={sorted(taken)}"
    )


def build_renovation_programs(
    inputs: DealInputs,
    unit_type_results: list[UnitTypeResult],
    strategy: str = "on_turnover",
    base_deal_inputs: dict | None = None,
) -> list[dict]:
    """Build canonical renovation_programs from plat-agent analysis results.

    Generates one program per unit type that passed the ROI gate.
    Each program maps to a unit_cohorts entry via target_cohort.

    When base_deal_inputs is provided, the target_cohort is resolved
    by matching unit type (bedrooms, bathrooms, sqft, count) to an
    existing cohort in base_deal_inputs["unit_cohorts"]. This ensures
    the generated programs reference valid cohort IDs. Falls back to
    synthesized IDs if no match is found.

    The output_cohort is generated to be non-colliding with both
    existing unit_cohorts and existing renovation_programs.output_cohort
    in base_deal_inputs.

    Args:
        inputs: Original deal inputs (for cohort ID generation).
        unit_type_results: Per-unit-type results from analyze_deal().
        strategy: Renovation strategy - "on_turnover" (wait for lease
                  expiry, limited by turnover rate) or "proactive"
                  (renovate at monthly_pace regardless of leases).
                  Default is "on_turnover" which is more conservative.
        base_deal_inputs: Optional canonical deal inputs. When provided,
                  used to resolve target_cohort to existing cohort IDs
                  and to ensure output_cohort uniqueness.

    Returns:
        List of renovation program dicts in canonical schema format.
    """
    if strategy not in ("on_turnover", "proactive"):
        raise ValueError(f"strategy must be 'on_turnover' or 'proactive', got '{strategy}'")

    # Build cohort lookup from base_deal_inputs for ID resolution
    cohort_lookup = _build_cohort_lookup(base_deal_inputs)

    # Track all already-claimed cohort identifiers so generated
    # output_cohorts cannot collide with either existing unit_cohorts
    # or with output_cohorts on prior renovation_programs.
    existing_cohort_ids: set[str] = set()
    if base_deal_inputs:
        existing_cohort_ids = {c["cohort_id"] for c in base_deal_inputs.get("unit_cohorts", [])}
        existing_cohort_ids |= {
            p["output_cohort"]
            for p in base_deal_inputs.get("renovation_programs", [])
            if "output_cohort" in p
        }

    programs = []

    for i, (unit_type, result) in enumerate(zip(inputs.unit_mix, unit_type_results)):
        if not result.roi_passes or result.renovation_program is None:
            continue

        bridge = result.renovation_program

        # Resolve cohort ID: prefer matching existing cohort, fall back to synthetic
        cohort_id = _resolve_cohort_id(
            unit_type.bedrooms, unit_type.bathrooms, unit_type.sqft, unit_type.count,
            i, cohort_lookup
        )

        program_id = f"reno_{cohort_id}"
        output_cohort = _make_output_cohort(cohort_id, program_id, existing_cohort_ids)
        existing_cohort_ids.add(output_cohort)

        program = {
            "program_id": program_id,
            "program_name": f"Renovation - {unit_type.bedrooms}BR/{unit_type.bathrooms}BA {unit_type.sqft:.0f}sf",
            "target_cohort": cohort_id,
            "output_cohort": output_cohort,
            "renovation_cost_per_unit": bridge.get("renovation_cost_per_unit"),
            "rent_premium_monthly": bridge.get("rent_premium_monthly"),
            "downtime_days": bridge.get("downtime_days", inputs.downtime_days),
            "strategy": strategy,
            "start_month": bridge.get("start_month", inputs.start_month),
            "monthly_pace": bridge.get("monthly_pace", inputs.monthly_pace),
        }

        # Optional fields
        if unit_type.count:
            program["max_units"] = unit_type.count

        programs.append(program)

    return programs


def merge_renovation_programs(
    base_deal_inputs: dict,
    renovation_programs: list[dict],
    base_unit_cohorts: list | None = None,
) -> dict:
    """Merge generated renovation_programs into existing deal inputs.

    Dedup is on (program_id) -- existing analyst-defined programs win.
    Additionally guards against:
      * output_cohort colliding with an existing unit_cohort.cohort_id
        (Legacy Park root cause: rent-roll-derived "*_renovated" subtotals
        that the federation must not overwrite).
      * Two programs sharing the same output_cohort while targeting
        different source cohorts (silent splice that would collapse
        distinct renovation streams into one).

    Args:
        base_deal_inputs: Full canonical deal inputs dict (mutated).
        renovation_programs: Programs from build_renovation_programs().
        base_unit_cohorts: Optional explicit unit_cohorts list. When
            omitted, the cohorts on base_deal_inputs are used.

    Returns:
        Updated deal inputs dict (mutated in place and returned).
    """
    if base_unit_cohorts is None:
        base_unit_cohorts = base_deal_inputs.get("unit_cohorts", [])
    base_unit_cohort_ids = {c["cohort_id"] for c in base_unit_cohorts}

    existing = list(base_deal_inputs.get("renovation_programs", []))
    seen_program_ids = {p["program_id"] for p in existing}
    seen_target_pairs = {
        (p.get("target_cohort"), p.get("output_cohort")) for p in existing
    }
    seen_output_cohorts = {p["output_cohort"] for p in existing if "output_cohort" in p}

    merged = list(existing)
    for new in renovation_programs:
        if new["program_id"] in seen_program_ids:
            # Existing wins: analyst's prior config takes precedence.
            continue

        new_output = new.get("output_cohort")
        new_target = new.get("target_cohort")

        if new_output is not None and new_output in base_unit_cohort_ids:
            raise ValueError(
                f"renovation_programs[{new['program_id']}].output_cohort='{new_output}' "
                f"collides with existing unit_cohorts.cohort_id"
            )

        if new_output is not None and new_output in seen_output_cohorts:
            existing_with_same_output = next(
                p for p in merged if p.get("output_cohort") == new_output
            )
            if existing_with_same_output.get("target_cohort") != new_target:
                raise ValueError(
                    f"output_cohort '{new_output}' targeted by both "
                    f"'{existing_with_same_output.get('target_cohort')}' and '{new_target}'"
                )

        merged.append(new)
        seen_program_ids.add(new["program_id"])
        seen_target_pairs.add((new_target, new_output))
        if new_output is not None:
            seen_output_cohorts.add(new_output)

    base_deal_inputs["renovation_programs"] = merged
    return base_deal_inputs


def _build_cohort_lookup(base_deal_inputs: dict | None) -> list[dict]:
    """Extract cohort records from base_deal_inputs for matching."""
    if not base_deal_inputs:
        return []
    return base_deal_inputs.get("unit_cohorts", [])


def _resolve_cohort_id(
    bedrooms: int,
    bathrooms: int,
    sqft: float,
    count: int,
    idx: int,
    cohort_lookup: list[dict],
) -> str:
    """Find the best-matching existing cohort ID, or synthesize one.

    Matching priority:
      1. Cohorts with explicit ``bedrooms`` AND ``bathrooms`` fields
         take precedence; their explicit values are trusted directly
         and the cohort_id is NOT regex-parsed.
      2. Legacy cohorts without explicit beds/baths fall back to a
         regex parse of the cohort_id (patterns like "1B1B", "2B2B").
      3. Among matching candidates, the closest (count, sqft) wins.
         If two candidates score *equally*, the function refuses to
         guess and raises -- silent ambiguity here was the source of
         multiple federation collision bugs.
      4. Synthesized fallback: stable hash of (bedrooms, bathrooms,
         sqft, count) so identical unit-type inputs across runs map
         to the same synthetic id.
    """
    if not cohort_lookup:
        return _cohort_id_synthetic(bedrooms, bathrooms, sqft, count)

    def parse_cohort(c: dict) -> tuple[int, float] | None:
        # Prefer explicit fields when both are present and non-null.
        # Bathrooms must stay as float — 1.5BA / 2.5BA cohorts must not
        # collapse to 1BA / 2BA.
        cb = c.get("bedrooms")
        cba = c.get("bathrooms")
        if cb is not None and cba is not None:
            return int(cb), float(cba)
        # Fall back to regex on the cohort_id for legacy records.
        cid = c.get("cohort_id", "")
        # Match ``\d+(\.\d+)?B`` so legacy ids like "1B1.5B" still parse.
        nums = re.findall(r"(\d+(?:\.\d+)?)B", cid)
        if len(nums) >= 2:
            return int(float(nums[0])), float(nums[1])
        return None

    bedrooms_int = int(bedrooms)
    bathrooms_float = float(bathrooms)
    BATHROOMS_TOLERANCE = 0.01  # absorb 1.0 vs 1.00 float noise; not 1.0 vs 1.5

    candidates = []
    for c in cohort_lookup:
        parsed = parse_cohort(c)
        if parsed is None:
            continue
        cb, cba = parsed
        if cb == bedrooms_int and abs(cba - bathrooms_float) < BATHROOMS_TOLERANCE:
            csqft = c.get("sqft", 0) or 0
            ccount = c.get("unit_count", 0) or 0
            candidates.append((c, abs(csqft - sqft), abs(ccount - count)))

    if not candidates:
        return _cohort_id_synthetic(bedrooms, bathrooms, sqft, count)

    # Sort by count match first, then sqft proximity.
    candidates.sort(key=lambda x: (x[2], x[1]))

    # Refuse ambiguous ties: if 2+ candidates score equally on (count_diff,
    # sqft_diff), there is no principled way to pick one -- raise instead
    # of silently returning the first.
    if len(candidates) >= 2:
        s0 = (candidates[0][2], candidates[0][1])
        s1 = (candidates[1][2], candidates[1][1])
        if s0 == s1:
            tied_ids = [c[0].get("cohort_id") for c in candidates if (c[2], c[1]) == s0]
            raise ValueError(
                f"Ambiguous cohort match for bedrooms={bedrooms} bathrooms={bathrooms} "
                f"sqft={sqft} count={count}: tied candidates {tied_ids}"
            )

    return candidates[0][0]["cohort_id"]


def splice_renovation_programs_into_canonical(
    canonical: dict,
    inputs: DealInputs,
    unit_type_results: list[UnitTypeResult],
    strategy: str = "on_turnover",
) -> dict:
    """Compose build + merge + post-merge validation into one call.

    This is the unified entry point that orchestrators (notably the
    cost-orchestrator) should use to splice federation-produced
    renovation programs into a canonical deal-inputs dict. It wraps
    ``build_renovation_programs`` and ``merge_renovation_programs``
    with a single explicit uniqueness pre-check so that any cohort
    namespace collision raises a single, well-typed error before any
    mutation occurs.

    Uniqueness invariants enforced (in addition to those already
    enforced by ``merge_renovation_programs``):

      * ``output_cohort`` MUST NOT collide with any existing
        ``unit_cohorts.cohort_id`` in ``canonical``.
      * ``program_id`` MUST be unique across the merged program list.
      * Two programs MAY share the same ``output_cohort`` only when
        they share the same ``target_cohort`` (which is the supported
        "two strategies into one outcome" pattern).

    The caller (cost-orchestrator) should catch ``ValueError`` and
    convert it into a ``BridgeError(code="schema_violation")``.

    Args:
        canonical: Canonical deal-inputs dict (NOT mutated; a shallow
            copy with a new ``renovation_programs`` field is returned).
        inputs: Original ``DealInputs`` driving the analysis (passes
            through to ``build_renovation_programs`` for cohort
            resolution + program parameter defaults).
        unit_type_results: Per-unit-type results from ``analyze_deal``.
        strategy: Renovation strategy passed through to
            ``build_renovation_programs``. Default ``"on_turnover"``.

    Returns:
        A new dict equal to ``canonical`` with ``renovation_programs``
        replaced by the merged-and-validated list.

    Raises:
        ValueError: On cohort_id collision, duplicate program_id, or
            ambiguous (target_cohort, output_cohort) pairing.
    """
    base_unit_cohorts = list(canonical.get("unit_cohorts", []))
    new_programs = build_renovation_programs(
        inputs,
        unit_type_results,
        strategy=strategy,
        base_deal_inputs=canonical,
    )

    # Explicit pre-check: surface collisions BEFORE merge mutates
    # anything. ``merge_renovation_programs`` repeats these checks
    # internally; the duplication is intentional so the splice
    # helper has a single, predictable failure surface for callers.
    base_cohort_ids = {c["cohort_id"] for c in base_unit_cohorts}
    existing_program_ids = {
        p["program_id"]
        for p in canonical.get("renovation_programs", [])
        if "program_id" in p
    }
    for new in new_programs:
        new_output = new.get("output_cohort")
        if new_output is not None and new_output in base_cohort_ids:
            raise ValueError(
                f"renovation_programs[{new['program_id']}].output_cohort="
                f"'{new_output}' collides with existing unit_cohorts.cohort_id"
            )
        if new["program_id"] in existing_program_ids:
            raise ValueError(
                f"renovation_program program_id='{new['program_id']}' "
                f"already exists in canonical.renovation_programs"
            )

    # ``merge_renovation_programs`` mutates and returns the dict it is
    # given. Pass a shallow copy so the original canonical is not
    # mutated by callers who hold a reference.
    canonical_copy = {**canonical, "renovation_programs": list(canonical.get("renovation_programs", []))}
    merged = merge_renovation_programs(
        canonical_copy,
        new_programs,
        base_unit_cohorts=base_unit_cohorts,
    )
    return merged


def _cohort_id_synthetic(
    bedrooms: int,
    bathrooms: int,
    sqft: float | None = None,
    count: int | None = None,
) -> str:
    """Generate a stable synthetic cohort ID for unit-type fingerprint.

    The id is keyed on (bedrooms, bathrooms, sqft, count) -- not the
    list index -- so identical inputs always map to the same id. This
    avoids the prior idx-based collisions where two runs over the same
    unit mix could produce different cohort ids depending on ordering.
    """
    fingerprint = f"{bedrooms}|{bathrooms}|{sqft or 0:.1f}|{count or 0}"
    digest = hashlib.sha1(fingerprint.encode("utf-8")).hexdigest()[:8]
    return f"{bedrooms}BR{bathrooms}BA_{digest}"
