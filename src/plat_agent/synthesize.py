"""Federated decision_summary emission.

Reads federated artifacts under `<deal_root>/outputs/<run_id>/` and emits
`decision_summary.json` next to them with the same shape `decision.py`
produces in the legacy single-process path:

    {
        "headline": str,
        "binding_constraints": list[str],
        "recommended_changes": list[str],
        "data_gaps": list[str]   # federated extension — what was missing
    }

The federated path bypasses `analyze_deal()`, so the inputs to
`build_decision_summary()` (typed UnitTypeResult, structured sanity flag
dicts) aren't available. This module reads the federated artifacts directly
and recomputes the decision-relevant fields:

  - Per-cohort ROI is recomputed from `costmodel/renovation_programs.json`
    using the same formula as `plat-costmodel/roi.py`
    (annual_lift / cost * 100 >= threshold).
  - Federated `_provenance.json.sanity_flags` is a list of bare string IDs;
    SANITY_FLAG_CATALOG translates each into the structured shape the legacy
    layer uses, with metric values pulled from `metrics_extracted` when
    available. Catalog mirrors the HARD/WARN bands inline in
    `.claude/agents/deal-memo-writer.md` §5 — keep in sync if either changes.
  - Fail-soft: missing artifacts contribute nothing; `data_gaps` lists what
    was missing so the memo-writer can surface the gaps.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

DEFAULT_ROI_THRESHOLD_PCT = 15.0


# ---------------------------------------------------------------------------
# Sanity-flag catalog — federated string ID → structured dict.
# Mirrors deal-memo-writer.md §5 HARD/WARN bands. Keep in sync.
# ---------------------------------------------------------------------------

# severity is "error" for HARD bands (engine-blocking class), "warning" for WARN.
SANITY_FLAG_CATALOG: dict[str, dict[str, Any]] = {
    # HARD
    "cap_rate_outside_4_7_band": {
        "metric": "going_in_cap_rate",
        "severity": "error",
        "expected_range": "4%-7%",
        "message_template": "Cap rate {value_pct} is outside the 4-7% multifamily band. Verify NOI assumption and purchase price.",
    },
    "dscr_below_1_10_likely_covenant_breach": {
        "metric": "min_dscr",
        "severity": "error",
        "expected_range": ">=1.10x",
        "message_template": "Minimum DSCR {value_x} is below the 1.10x covenant floor — likely lender breach.",
    },
    "irr_outside_expected_band": {
        "metric": "levered_irr",
        "severity": "error",
        "expected_range": "8%-30%",
        "message_template": "Levered IRR {value_pct} is outside the 8-30% expected band. Verify rent growth, exit cap, and leverage assumptions.",
    },
    # WARN
    "dscr_below_1_20": {
        "metric": "min_dscr",
        "severity": "warning",
        "expected_range": ">=1.20x",
        "message_template": "Minimum DSCR {value_x} is below the 1.20x lender threshold.",
    },
    "dscr_below_1_30_lender_refi_floor": {
        "metric": "min_dscr",
        "severity": "warning",
        "expected_range": ">=1.30x",
        "message_template": "Minimum DSCR {value_x} is below the 1.30x lender refi floor.",
    },
    "irr_below_12pct_hurdle": {
        "metric": "levered_irr",
        "severity": "warning",
        "expected_range": ">=12%",
        "message_template": "Levered IRR {value_pct} is below the 12% sponsor hurdle.",
    },
    "equity_multiple_low": {
        "metric": "levered_em",
        "severity": "warning",
        "expected_range": ">=1.5x",
        "message_template": "Levered equity multiple {value_x} is below 1.5x.",
    },
    "partnership_irr_below_lp_hurdle": {
        "metric": "partnership_irr",
        "severity": "warning",
        "expected_range": ">=8%",
        "message_template": "Partnership IRR (LP) {value_pct} is below the 8% LP hurdle.",
    },
}

# Metric name → metrics_extracted key in _provenance.json.
_METRIC_LOOKUP_KEYS: dict[str, tuple[str, ...]] = {
    "going_in_cap_rate": ("going_in_cap",),
    "exit_cap_rate": ("exit_cap",),
    "min_dscr": ("min_dscr",),
    "levered_irr": ("levered_irr",),
    "levered_em": ("equity_multiple",),
    "partnership_irr": ("partnership_irr",),
}


# ---------------------------------------------------------------------------
# Public entry
# ---------------------------------------------------------------------------

def synthesize_from_federated_artifacts(
    deal_root: Path,
    run_id: str,
    *,
    threshold_pct: float = DEFAULT_ROI_THRESHOLD_PCT,
) -> dict:
    """Read federated artifacts and return a decision_summary dict.

    Args:
        deal_root: e.g. multifamily-underwriting/runs/deals/<slug>/
        run_id: e.g. "run_002_federation"
        threshold_pct: ROI gate threshold (defaults to 15% to match
            plat-costmodel's DEFAULT_THRESHOLD_PCT).

    Returns:
        Dict with keys {headline, binding_constraints, recommended_changes,
        data_gaps}. data_gaps lists which expected artifacts were missing.
    """
    run_dir = Path(deal_root) / "outputs" / run_id

    costmodel = _read_json_safe(run_dir / "costmodel" / "renovation_programs.json")
    property_estimate = _read_json_safe(run_dir / "costmodel" / "property_estimate.json")
    deal_summary = _read_json_safe(run_dir / "underwriting" / "deal_summary.json")
    underwriting_provenance = _read_json_safe(run_dir / "underwriting" / "_provenance.json")

    data_gaps: list[str] = []
    if costmodel is None:
        data_gaps.append("costmodel/renovation_programs.json missing — no per-cohort ROI constraints")
    if property_estimate is None:
        data_gaps.append("costmodel/property_estimate.json missing — no risk flags")
    if deal_summary is None:
        data_gaps.append("underwriting/deal_summary.json missing — no metrics")
    if underwriting_provenance is None:
        data_gaps.append("underwriting/_provenance.json missing — no verdict, no sanity flags")

    # --- Per-cohort ROI failures from costmodel programs ----------------
    failing_programs: list[dict] = []
    if isinstance(costmodel, list):
        for program in costmodel:
            roi = _recompute_roi(program, threshold_pct=threshold_pct)
            if not roi["roi_passes"]:
                failing_programs.append({**program, "_roi": roi})

    # --- Risk flags from costmodel property estimate --------------------
    risk_flags: list[dict] = []
    if isinstance(property_estimate, dict):
        risk_flags = property_estimate.get("risk_flags") or []

    # --- Sanity flags translation from underwriting provenance ----------
    sanity_flags: list[dict] = []
    underwriting_verdict: str | None = None
    metrics_extracted: dict = {}
    if isinstance(underwriting_provenance, dict):
        underwriting_verdict = underwriting_provenance.get("verdict")
        metrics_extracted = underwriting_provenance.get("metrics_extracted") or {}
        for flag_id in underwriting_provenance.get("sanity_flags", []) or []:
            sanity_flags.append(_translate_federated_sanity_flag(flag_id, metrics_extracted))

    # --- Compose ---------------------------------------------------------
    binding_constraints = (
        _roi_failure_constraints(failing_programs)
        + _sanity_constraints(sanity_flags)
        + _risk_flag_constraints(risk_flags)
    )
    recommended_changes = (
        _roi_path_to_pass(failing_programs)
        + _sanity_remediation(sanity_flags)
    )
    headline = _headline(
        n_failing_cohorts=len(failing_programs),
        n_total_cohorts=len(costmodel) if isinstance(costmodel, list) else 0,
        underwriting_verdict=underwriting_verdict,
        n_sanity_errors=sum(1 for f in sanity_flags if f["severity"] == "error"),
        n_sanity_warnings=sum(1 for f in sanity_flags if f["severity"] == "warning"),
        data_gaps_present=bool(data_gaps),
    )

    return {
        "headline": headline,
        "binding_constraints": binding_constraints,
        "recommended_changes": recommended_changes,
        "data_gaps": data_gaps,
    }


# ---------------------------------------------------------------------------
# ROI recomputation — mirrors plat-costmodel/roi.py
# ---------------------------------------------------------------------------

def _recompute_roi(program: dict, *, threshold_pct: float) -> dict:
    """Recompute per-cohort ROI verdict from a federated renovation program."""
    cost = float(program.get("renovation_cost_per_unit") or 0)
    current = float(program.get("current_monthly_rent") or 0)
    target = float(program.get("target_monthly_rent") or 0)
    monthly_lift = target - current
    annual_lift = monthly_lift * 12

    if cost <= 0:
        return {
            "roi_pct": 0.0, "roi_passes": False, "roi_gap_pp": -threshold_pct,
            "monthly_rent_lift": monthly_lift,
            "cost_reduction_needed_to_pass": None,
            "rent_increase_needed_to_pass": None,
        }

    roi_pct = (annual_lift / cost) * 100
    roi_passes = roi_pct >= threshold_pct

    cost_reduction_needed: float | None = None
    rent_increase_needed: float | None = None
    if not roi_passes:
        if annual_lift > 0:
            required_cost = (annual_lift * 100.0) / threshold_pct
            cost_reduction_needed = round(cost - required_cost)
        needed_annual = cost * threshold_pct / 100.0
        needed_monthly_lift = needed_annual / 12.0
        rent_increase_needed = round((current + needed_monthly_lift) - target)

    return {
        "roi_pct": round(roi_pct, 2),
        "roi_passes": roi_passes,
        "roi_gap_pp": round(roi_pct - threshold_pct, 2),
        "monthly_rent_lift": monthly_lift,
        "cost_reduction_needed_to_pass": cost_reduction_needed,
        "rent_increase_needed_to_pass": rent_increase_needed,
    }


# ---------------------------------------------------------------------------
# Sanity flag translation
# ---------------------------------------------------------------------------

def _translate_federated_sanity_flag(flag_id: str, metrics: dict) -> dict:
    """Translate a federated string-ID sanity flag into the structured dict
    shape used by the legacy plat_agent.sanity layer.

    Special case: `cap_rate_outside_4_7_band` does not disambiguate going-in
    vs exit cap. We pick whichever cap is ACTUALLY outside [0.04, 0.07] —
    or fall back to going-in if both are in-band (which would mean the
    federated engine and our catalog disagree on the band, worth surfacing).
    """
    catalog = SANITY_FLAG_CATALOG.get(flag_id)
    if catalog is None:
        return {
            "metric": "unknown",
            "value": None,
            "severity": "warning",
            "expected_range": "n/a",
            "message": f"Unrecognized federated sanity flag: {flag_id!r} (not in SANITY_FLAG_CATALOG).",
            "flag_id": flag_id,
        }

    metric = catalog["metric"]
    if flag_id == "cap_rate_outside_4_7_band":
        metric, value = _select_out_of_band_cap(metrics)
    else:
        value = _lookup_metric_value(metric, metrics)

    return {
        "metric": metric,
        "value": value,
        "severity": catalog["severity"],
        "expected_range": catalog["expected_range"],
        "message": _format_template(catalog["message_template"], value),
        "flag_id": flag_id,
    }


def _select_out_of_band_cap(metrics: dict) -> tuple[str, float | None]:
    """Pick whichever cap rate is outside [0.04, 0.07]. Prefer exit if both
    are out (typical multifamily — exit cap drives the longer-tail risk).
    Fall back to going-in if neither is out (shouldn't happen given the
    flag was emitted, but stay defensive).
    """
    going_in = metrics.get("going_in_cap")
    exit_cap = metrics.get("exit_cap")
    LO, HI = 0.04, 0.07

    def _out(v: float | None) -> bool:
        return v is not None and (v < LO or v > HI)

    if _out(exit_cap) and not _out(going_in):
        return "exit_cap_rate", exit_cap
    if _out(going_in) and not _out(exit_cap):
        return "going_in_cap_rate", going_in
    if _out(exit_cap) and _out(going_in):
        # Prefer exit cap when both are out — typically the more dangerous
        return "exit_cap_rate", exit_cap
    # Fallback: report going-in for legibility even though neither is out
    return "going_in_cap_rate", going_in


def _lookup_metric_value(metric: str, metrics_extracted: dict) -> float | None:
    for key in _METRIC_LOOKUP_KEYS.get(metric, (metric,)):
        if key in metrics_extracted:
            return metrics_extracted[key]
    return None


def _format_template(template: str, value: float | None) -> str:
    if value is None:
        return template.replace("{value_pct}", "n/a").replace("{value_x}", "n/a")
    return (
        template
        .replace("{value_pct}", f"{value:.2%}")
        .replace("{value_x}", f"{value:.2f}x")
    )


# ---------------------------------------------------------------------------
# Constraint + change builders (federated equivalents of decision.py helpers)
# ---------------------------------------------------------------------------

def _roi_failure_constraints(failing: list[dict]) -> list[str]:
    out: list[str] = []
    for p in failing:
        roi = p["_roi"]
        label = _cohort_label(p)
        out.append(
            f"{label} fails ROI gate "
            f"(ROI {roi['roi_pct']:.1f}%, gap {roi['roi_gap_pp']:.1f}pp; "
            f"monthly rent lift ${roi['monthly_rent_lift']:,.0f})."
        )
    return out


def _sanity_constraints(sanity_flags: list[dict]) -> list[str]:
    return [
        f"[{f.get('severity', 'warning').upper()}] {f.get('message', '')}"
        for f in sanity_flags
    ]


def _risk_flag_constraints(risk_flags: list[dict]) -> list[str]:
    out: list[str] = []
    for f in risk_flags:
        msg = f.get("message") or f.get("description") or f.get("flag")
        if msg:
            out.append(f"[risk] {msg}")
    return out


def _roi_path_to_pass(failing: list[dict]) -> list[str]:
    out: list[str] = []
    for p in failing:
        roi = p["_roi"]
        label = _cohort_label(p)
        if (rent_delta := roi.get("rent_increase_needed_to_pass")) and rent_delta > 0:
            out.append(
                f"{label}: raise target rent by ${rent_delta:,.0f}/mo to clear "
                "ROI gate at current cost."
            )
        if (cost_delta := roi.get("cost_reduction_needed_to_pass")) and cost_delta > 0:
            out.append(
                f"{label}: cut renovation cost by ${cost_delta:,.0f} to clear "
                "ROI gate at current target rent."
            )
    return out


def _sanity_remediation(sanity_flags: list[dict]) -> list[str]:
    """Per-flag remediation strings. Deduplicated by flag_id."""
    seen: set[str] = set()
    out: list[str] = []
    for f in sanity_flags:
        flag_id = f.get("flag_id", "")
        if flag_id in seen:
            continue
        seen.add(flag_id)
        metric = f.get("metric")
        if metric in ("going_in_cap_rate", "exit_cap_rate"):
            out.append(
                "Re-verify NOI assumption and purchase price — cap rate is "
                "outside the typical 4-7% multifamily range."
            )
        elif metric == "min_dscr":
            out.append(
                "Re-check debt sizing or NOI in worst months — minimum DSCR "
                "is below the lender threshold."
            )
        elif metric == "levered_irr":
            out.append(
                "Sanity-check rent growth, exit cap, and leverage — levered "
                "IRR is outside the plausible range."
            )
        elif metric == "levered_em":
            out.append(
                "Sanity-check inputs — levered equity multiple is outside "
                "the plausible range."
            )
        elif metric == "partnership_irr":
            out.append(
                "Re-check waterfall structure or deal-level returns — "
                "partnership IRR (LP) is below hurdle."
            )
    return out


def _headline(
    *,
    n_failing_cohorts: int,
    n_total_cohorts: int,
    underwriting_verdict: str | None,
    n_sanity_errors: int,
    n_sanity_warnings: int,
    data_gaps_present: bool,
) -> str:
    if data_gaps_present and n_total_cohorts == 0 and underwriting_verdict is None:
        return "Insufficient federated artifacts to synthesize a decision — see data_gaps."
    if n_failing_cohorts:
        return (
            f"Cost gate fails — {n_failing_cohorts} of {n_total_cohorts} "
            "cohort(s) miss the ROI threshold."
        )
    if underwriting_verdict == "fail":
        return "Underwriting verdict: fail — see binding_constraints for details."
    if n_sanity_errors:
        return (
            f"Underwriting passed but {n_sanity_errors} sanity error(s) suggest "
            "the inputs or outputs are out of range — verify before acting."
        )
    if underwriting_verdict == "marginal":
        return "Underwriting verdict: marginal — review sanity flags before advancing."
    if n_sanity_warnings:
        return (
            f"Deal clears all gates with {n_sanity_warnings} sanity warning(s) "
            "worth a second look."
        )
    if underwriting_verdict == "pass":
        return "Deal clears the ROI gate and underwriting feasibility — ready to advance."
    return "Synthesis complete; no binding constraints surfaced."


def _cohort_label(program: dict) -> str:
    """Federated label uses cohort_id (richer than br/ba alone)."""
    cohort_id = program.get("cohort_id") or "unknown"
    unit_count = program.get("unit_count")
    if unit_count:
        return f"{cohort_id} ({unit_count}u)"
    return cohort_id


def _read_json_safe(path: Path) -> Any | None:
    """Read JSON; return None if file missing. Raises on malformed JSON."""
    try:
        with path.open() as f:
            return json.load(f)
    except FileNotFoundError:
        return None
