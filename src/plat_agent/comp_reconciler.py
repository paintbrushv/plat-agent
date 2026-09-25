"""Helpers for the comp-reconciler federation step (Wave 6 Task 6.2).

The Sonnet agent body lives at `.claude/agents/comp-reconciler.md`. The
pure-Python logic — gate checks + divergence computation + payload
construction — lives here so it can be unit-tested directly without
spinning up a sibling-agent subprocess.

Two entry points:

* `check_comp_evidence_gate(canonical, comps_json_path)` — invoked by
  `market-context-orchestrator`. Returns the list of cohort_ids that
  carry a non-zero rent premium. If `comps_json_path` is missing AND
  the list is non-empty, the orchestrator must raise
  `BridgeError(code="comp_evidence_missing", recoverable=True)`.

* `compute_comp_reconciliation(canonical, comps_payload, threshold_pct)` —
  invoked by `comp-reconciler`. Returns a `CompReconciliationResult` with
  per-cohort calibration + a list of `comp_disagreement_<cohort>` advisory
  flags ready to slot into `BridgeResponse.sanity_flags`.

Per Wave 6 Q2 (CONSOLIDATED_FIX_PLAN.md): disagreement is advisory only.
Only `comp_evidence_missing` (the gate) is a hard block.
"""

from __future__ import annotations

import statistics
from pathlib import Path
from typing import Any

from plat_agent.contracts.domain.market_study import (
    CompReconciliationEntry,
    CompReconciliationResult,
    cohort_key,
)


def collect_premium_cohort_ids(canonical: dict[str, Any]) -> list[str]:
    """Return cohort_ids carrying a non-zero rent premium in canonical.

    Scans both encoding paths the federation uses:
      1. `revenue_programs[].reno_premium_<period>` analyst-side encoding.
         Any field whose name starts with `reno_premium_` and whose value
         is > 0 counts.
      2. `renovation_programs[].rent_premium_monthly` federation-side
         encoding produced by cost-bridge-analyst splice. `rent_premium_monthly > 0` counts.

    Returns a sorted, deduplicated list of cohort_ids.
    """
    premium_cohorts: set[str] = set()

    # Path 1: revenue_programs[i] where program_id starts with "reno_premium_"
    # (analyst-side encoding: each program is one cohort's premium fee). The
    # canonical schema uses `eligible_units` (cohort_id), `program_id`
    # ("reno_premium_<cohort>"), and `price_value` (the premium amount).
    for program in canonical.get("revenue_programs", []) or []:
        if not isinstance(program, dict):
            continue
        program_id = program.get("program_id", "")
        if not isinstance(program_id, str) or not program_id.startswith("reno_premium_"):
            continue
        cohort_id = program.get("eligible_units") or program.get("target_cohort") or program.get("cohort_id")
        if not cohort_id or cohort_id == "ALL":
            continue
        try:
            price = program.get("price_value")
            if price is not None and float(price) > 0:
                premium_cohorts.add(cohort_id)
        except (TypeError, ValueError):
            continue

    # Path 2: renovation_programs.rent_premium_monthly
    for program in canonical.get("renovation_programs", []) or []:
        if not isinstance(program, dict):
            continue
        target = program.get("target_cohort") or program.get("cohort_id")
        if not target:
            continue
        raw = program.get("rent_premium_monthly")
        try:
            if raw is not None and float(raw) > 0:
                premium_cohorts.add(target)
        except (TypeError, ValueError):
            continue

    return sorted(premium_cohorts)


def check_comp_evidence_gate(
    canonical: dict[str, Any],
    comps_json_path: Path,
) -> dict[str, Any]:
    """Return a gate decision dict for the orchestrator to act on.

    The ``comps_json_path`` argument is the *legacy* convention that pointed
    at ``<run_id>/market_study/comps.json``. The actual structured federation
    contract output is ``comps_cohort_grouped.json`` next to it (cohort-keyed
    rents, populated by collect_comps_snapshot.py + Playwright fallback;
    ``comps.json`` is the per-comp raw scraper dump and is mostly empty when
    direct-site scrapers fail).

    This function accepts the legacy path and AUTOMATICALLY checks the
    cohort-grouped sibling first. Evidence is "present" when EITHER file
    exists AND has at least one populated cohort entry.

    Output shape:
        {
            "premium_cohorts": list[str],
            "comps_json_exists": bool,            # back-compat: true if EITHER file is usable
            "evidence_path_used": str | None,     # which file actually provided evidence
            "block": bool,
            "cohort_ids_lacking_evidence": list[str],
            "reconciliation_required": bool,
        }

    The orchestrator interprets:
      - `block=True` → raise `BridgeError(code="comp_evidence_missing", ...)`.
      - `reconciliation_required=True` → emit
        `comp_reconciliation_required` flag in `sanity_flags`.
      - `block=False, reconciliation_required=False` → no comp evidence
        needed (no premium cohorts).
    """
    premium_cohorts = collect_premium_cohort_ids(canonical)

    legacy_path = Path(comps_json_path)
    cohort_grouped_path = legacy_path.parent / "comps_cohort_grouped.json"

    evidence_path_used: str | None = None
    for candidate in (cohort_grouped_path, legacy_path):
        if not candidate.exists():
            continue
        # Treat the file as evidence only if it has populated cohort data.
        # (legacy comps.json is often the empty scraper dump.)
        try:
            payload = _load_json(candidate)
        except (OSError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        cbc = payload.get("comps_by_cohort")
        if isinstance(cbc, dict) and any(cbc.values()):
            evidence_path_used = str(candidate)
            break

    exists = evidence_path_used is not None
    block = bool(premium_cohorts) and not exists
    return {
        "premium_cohorts": premium_cohorts,
        "comps_json_exists": exists,
        "evidence_path_used": evidence_path_used,
        "block": block,
        "cohort_ids_lacking_evidence": premium_cohorts if block else [],
        "reconciliation_required": bool(premium_cohorts) and exists,
    }


def _load_json(path: Path) -> Any:
    """Tiny JSON-load helper kept private to this module."""
    import json
    return json.loads(path.read_text(encoding="utf-8"))


def _month1_market_rent(canonical: dict[str, Any], cohort_id: str) -> float | None:
    """Resolve canonical month-1 market_rent for a cohort_id.

    Primary shape (current canonical schema v0.1):
        market_rent_curve: list[{cohort_id, start_period, end_period, market_rent}]
    The earliest segment for the cohort (sorted by start_period) supplies
    month-1 rent. Tolerant of legacy shapes where rents arrive as parallel
    lists (`values` / `monthly_rents` / `rents`).

    Fallback: ``unit_cohorts[].initial_inplace_rent`` (the actual canonical
    field carrying current in-place rent), or any of a few synonyms that
    appeared in earlier schema iterations.
    """
    curve = canonical.get("market_rent_curve")

    # Legacy dict shape: {cohort_id: [rent, rent, ...]}
    if isinstance(curve, dict):
        values = curve.get(cohort_id)
        if isinstance(values, list) and values:
            try:
                return float(values[0])
            except (TypeError, ValueError):
                pass

    # Canonical schema v0.1: list of segments with `market_rent` per segment.
    if isinstance(curve, list):
        cohort_segments = [
            entry for entry in curve
            if isinstance(entry, dict) and entry.get("cohort_id") == cohort_id
        ]
        # Earliest segment by start_period (string-sortable ISO YYYY-MM).
        cohort_segments.sort(key=lambda e: e.get("start_period") or "")
        for entry in cohort_segments:
            # Primary: singular market_rent on this segment.
            mr = entry.get("market_rent")
            if mr is not None:
                try:
                    return float(mr)
                except (TypeError, ValueError):
                    pass
            # Legacy parallel-list shapes (values / monthly_rents / rents).
            for key in ("values", "monthly_rents", "rents"):
                vals = entry.get(key)
                if isinstance(vals, list) and vals:
                    try:
                        return float(vals[0])
                    except (TypeError, ValueError):
                        continue

    # Fallback: unit_cohorts[].initial_inplace_rent (canonical field name)
    # plus a few legacy synonyms.
    for cohort in canonical.get("unit_cohorts", []) or []:
        if not isinstance(cohort, dict):
            continue
        if cohort.get("cohort_id") == cohort_id:
            for key in (
                "initial_inplace_rent",
                "market_rent",
                "in_place_rent",
                "current_market_rent",
            ):
                v = cohort.get(key)
                if v is None:
                    continue
                try:
                    return float(v)
                except (TypeError, ValueError):
                    continue
    return None


def _cohort_metadata(canonical: dict[str, Any], cohort_id: str) -> dict[str, Any] | None:
    """Pull (bedrooms, bathrooms, sqft) for a cohort_id from unit_cohorts."""
    for cohort in canonical.get("unit_cohorts", []) or []:
        if isinstance(cohort, dict) and cohort.get("cohort_id") == cohort_id:
            return cohort
    return None


def _recommendation(divergence_pct: float, threshold_pct: float) -> str:
    if abs(divergence_pct) <= threshold_pct:
        return "within tolerance"
    if divergence_pct > 0:
        # comp p50 > canonical -> canonical may be conservative
        return "review and consider upward adjustment"
    # comp p50 < canonical -> canonical may be optimistic
    return "review and consider downward adjustment"


def compute_comp_reconciliation(
    canonical: dict[str, Any],
    comps_payload: dict[str, Any],
    *,
    divergence_threshold_pct: float = 10.0,
) -> tuple[CompReconciliationResult, list[str]]:
    """Compute per-cohort calibration + advisory flags.

    Args:
        canonical: parsed canonical_deal.json
        comps_payload: parsed comps.json (matches `CompFinderResponse`).
        divergence_threshold_pct: absolute percent above which a
            `comp_disagreement_<cohort>` advisory flag is emitted.

    Returns:
        (result, sanity_flags) where:
          - result: `CompReconciliationResult` with per-cohort entries.
          - sanity_flags: list of `comp_disagreement_<cohort_id>` strings
            ready to inject into `BridgeResponse.sanity_flags`. Empty
            when every premium cohort is within tolerance.

    Per Wave 6 Q2: this function NEVER raises on disagreement and does
    NOT mutate `canonical`. Disagreement is advisory only.
    """
    premium_ids = collect_premium_cohort_ids(canonical)
    comps_by_cohort: dict[str, list[dict]] = comps_payload.get("comps_by_cohort", {}) or {}

    entries: list[CompReconciliationEntry] = []
    sanity_flags: list[str] = []
    lacking: list[str] = []

    for cohort_id in premium_ids:
        metadata = _cohort_metadata(canonical, cohort_id)
        canonical_rent = _month1_market_rent(canonical, cohort_id)
        if canonical_rent is None or canonical_rent <= 0:
            lacking.append(cohort_id)
            continue
        if not metadata:
            lacking.append(cohort_id)
            continue

        try:
            ck = cohort_key(
                int(metadata["bedrooms"]),
                float(metadata["bathrooms"]),
                int(float(metadata["sqft"])),
            )
        except (KeyError, TypeError, ValueError):
            lacking.append(cohort_id)
            continue

        comps = comps_by_cohort.get(ck) or []
        rents = [
            float(c["asking_rent"])
            for c in comps
            if isinstance(c, dict) and c.get("asking_rent") is not None
        ]
        if not rents:
            lacking.append(cohort_id)
            continue

        comp_p50 = float(statistics.median(rents))
        divergence_pct = ((comp_p50 - canonical_rent) / canonical_rent) * 100.0
        recommendation = _recommendation(divergence_pct, divergence_threshold_pct)

        entries.append(
            CompReconciliationEntry(
                cohort_id=cohort_id,
                canonical_market_rent=canonical_rent,
                comp_p50_rent=comp_p50,
                comp_count=len(rents),
                divergence_pct=divergence_pct,
                recommendation=recommendation,
            )
        )

        if abs(divergence_pct) > divergence_threshold_pct:
            sanity_flags.append(f"comp_disagreement_{cohort_id}")

    result = CompReconciliationResult(
        market_rent_calibration=entries,
        divergence_threshold_pct=divergence_threshold_pct,
        advisory_only=True,
        cohorts_lacking_comp_evidence=lacking,
    )
    return result, sanity_flags


__all__ = [
    "check_comp_evidence_gate",
    "collect_premium_cohort_ids",
    "compute_comp_reconciliation",
]
