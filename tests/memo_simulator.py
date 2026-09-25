"""Deterministic Python simulator of the deal-memo-writer agent contract.

The deal-memo-writer is a Sonnet-backed Claude Code sub-agent — its prompt
(`.claude/agents/deal-memo-writer.md`) is the production source of truth.
This simulator is a *contract checker*: it implements the same rules in
deterministic Python so the test suite can assert byte-stable behavior on
audited fixtures. If the simulator and the prompt disagree on a fixture,
either the prompt is ambiguous (fix the prompt) or the simulator is wrong
(fix the simulator). Both are covered by the same `tests/test_deal_memo_writer_contract.py`.

Wave 5 / Stage 6 audit findings enforced:
- M-1 cross-run reconciliation
- M-2 sanity bands inline
- M-3 partnership IRR in metrics table
- M-4 staleness/idempotency (header fields + footer mtimes)
- M-6 cap-rate raw + derived precision
- M-7 cohort_id collision defense-in-depth
"""

from __future__ import annotations

import glob
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


# ---------------------------------------------------------------------------
# Sanity bands catalog — mirrors `engine.feasibility.classify` (Wave 1a) and
# `multifamily-underwriting/.claude/agents/underwriting-runner.md` (Wave 4).
# Repeated here to keep the memo composer self-contained per audit M-2.
# ---------------------------------------------------------------------------

HARD_CAP_RATE_BAND = (0.04, 0.07)
HARD_DSCR_FLOOR = 1.10
HARD_IRR_BAND = (0.08, 0.30)

WARN_DSCR_1_20 = 1.20
WARN_DSCR_1_30 = 1.30
WARN_IRR_HURDLE = 0.12
WARN_EM_LOW = 1.5
WARN_PARTNERSHIP_IRR_HURDLE = 0.08

CROSS_RUN_IRR_DELTA_THRESHOLD = 0.05  # 5 percentage points
CROSS_RUN_EM_DELTA_THRESHOLD = 0.30   # 0.3x equity multiple


# ---------------------------------------------------------------------------
# Result shape
# ---------------------------------------------------------------------------

@dataclass
class MemoSimResult:
    """Markdown-shaped dict the simulator emits — proxy for what the LLM
    agent would produce. Tests assert against this."""

    header: dict[str, str] = field(default_factory=dict)
    sections: dict[str, str] = field(default_factory=dict)
    metrics_table_rows: list[dict[str, Any]] = field(default_factory=list)
    sanity_flags: list[str] = field(default_factory=list)
    structural_integrity_flags: list[str] = field(default_factory=list)
    cross_run_table: list[dict[str, Any]] = field(default_factory=list)
    cohort_table_rows: list[dict[str, Any]] = field(default_factory=list)
    source_artifacts_footer: list[dict[str, Any]] = field(default_factory=list)
    refused_overwrite: bool = False
    refusal_reason: Optional[str] = None
    recommendation: Optional[str] = None  # 'pursue' | 'pursue-with-conditions' | 'pass'

    def to_markdown(self) -> str:
        """Render a memo.md string, useful for golden-file inspection."""
        lines = ["# Investment Memo"]
        for k, v in self.header.items():
            lines.append(f"**{k}:** {v}")
        lines.append("")
        for sec_name, sec_body in self.sections.items():
            lines.append(f"## {sec_name}")
            lines.append(sec_body)
            lines.append("")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Sanity verdict helpers (M-2)
# ---------------------------------------------------------------------------

def cap_rate_verdict(cap: Optional[float]) -> tuple[str, Optional[str]]:
    if cap is None:
        return "OK", None
    lo, hi = HARD_CAP_RATE_BAND
    if cap < lo or cap > hi:
        return "FLAG", "cap_rate_outside_4_7_band"
    return "OK", None


def irr_verdict(irr: Optional[float]) -> tuple[str, Optional[str]]:
    if irr is None:
        return "OK", None
    lo, hi = HARD_IRR_BAND
    if irr < lo or irr > hi:
        return "FLAG", "irr_outside_expected_band"
    if irr < WARN_IRR_HURDLE:
        return "WARN", "irr_below_12pct_hurdle"
    return "OK", None


def dscr_verdict(dscr: Optional[float]) -> tuple[str, Optional[str]]:
    if dscr is None:
        return "OK", None
    if dscr < HARD_DSCR_FLOOR:
        return "FLAG", "dscr_below_1_10_likely_covenant_breach"
    if dscr < WARN_DSCR_1_20:
        return "WARN", "dscr_below_1_20"
    if dscr < WARN_DSCR_1_30:
        return "WARN", "dscr_below_1_30_lender_refi_floor"
    return "OK", None


def equity_multiple_verdict(em: Optional[float]) -> tuple[str, Optional[str]]:
    if em is None:
        return "OK", None
    if em < WARN_EM_LOW:
        return "WARN", "equity_multiple_low"
    return "OK", None


def partnership_irr_verdict(p_irr: Optional[float]) -> tuple[str, Optional[str]]:
    if p_irr is None:
        return "OK", None
    if p_irr < WARN_PARTNERSHIP_IRR_HURDLE:
        return "WARN", "partnership_irr_below_lp_hurdle"
    return "OK", None


# ---------------------------------------------------------------------------
# Header builder (M-4)
# ---------------------------------------------------------------------------

def build_header(
    *,
    deal_slug: str,
    run_id: str,
    provenance: Optional[dict],
    composition_time_utc: Optional[datetime] = None,
) -> tuple[dict[str, str], list[str]]:
    """Return (header dict, list of structural-integrity flags emitted by header)."""
    flags: list[str] = []
    prov = provenance or {}
    engine_version = prov.get("engine_version") or "not_available"
    schema_version = prov.get("schema_version") or "not_available"
    full_hash = prov.get("inputs_hash_sha256") or ""
    inputs_hash_short = full_hash[:12] if full_hash else "not_available"

    if engine_version == "not_available" or inputs_hash_short == "not_available":
        flags.append("stale_provenance")

    composed_at = composition_time_utc or datetime.now(timezone.utc)

    header = {
        "Deal slug": deal_slug,
        "Run": run_id,
        "engine_version": engine_version,
        "schema_version": schema_version,
        "inputs_hash_sha256_short": inputs_hash_short,
        "generated_at_utc": composed_at.isoformat(),
    }
    return header, flags


# ---------------------------------------------------------------------------
# Cross-run reconciliation (M-1)
# ---------------------------------------------------------------------------

def discover_prior_runs(deal_root: Path, current_run_id: str) -> list[Path]:
    """Find prior `outputs/run_*/underwriting/deal_summary.json` and legacy
    `outputs/run_*/value_add_base_outputs_*.json` / `*_model_outputs.json`."""
    paths: list[Path] = []
    outputs_dir = deal_root / "outputs"
    if not outputs_dir.is_dir():
        return paths
    for run_dir in sorted(outputs_dir.iterdir()):
        if not run_dir.is_dir():
            continue
        if run_dir.name == current_run_id:
            continue
        # Federation-shaped
        ds = run_dir / "underwriting" / "deal_summary.json"
        if ds.is_file():
            paths.append(ds)
        # Legacy /underwrite-deal pre-federation outputs
        for legacy in glob.glob(str(run_dir / "value_add_base_outputs_*.json")):
            paths.append(Path(legacy))
        for legacy in glob.glob(str(run_dir / "*_model_outputs.json")):
            paths.append(Path(legacy))
    return paths


def _extract_irr_em(payload: dict) -> tuple[Optional[float], Optional[float]]:
    """Extract levered_irr and equity_multiple from either the federation
    deal_summary shape or a legacy pre-federation output."""
    metrics = payload.get("metrics") or {}
    irr_block = metrics.get("irr") or {}
    em_block = metrics.get("equity_multiple") or {}
    irr = irr_block.get("levered_irr")
    em = em_block.get("levered_em")
    if irr is None:
        # Legacy flat shape sometimes used by /underwrite-deal
        irr = payload.get("levered_irr")
    if em is None:
        em = payload.get("levered_em") or payload.get("equity_multiple")
    return irr, em


def reconcile_cross_runs(
    *,
    current_irr: Optional[float],
    current_em: Optional[float],
    prior_paths: list[Path],
) -> tuple[bool, list[dict[str, Any]]]:
    """Returns (disagreement_flag, table_rows). table_rows has one entry per
    prior run plus a delta row for any run exceeding thresholds."""
    rows: list[dict[str, Any]] = []
    disagreement = False
    if current_irr is None or current_em is None:
        return False, rows
    for prior in prior_paths:
        try:
            with open(prior) as fh:
                payload = json.load(fh)
        except (OSError, json.JSONDecodeError):
            continue
        prior_irr, prior_em = _extract_irr_em(payload)
        if prior_irr is None and prior_em is None:
            continue
        irr_delta = (
            abs(current_irr - prior_irr) if prior_irr is not None else None
        )
        em_delta = abs(current_em - prior_em) if prior_em is not None else None
        triggers_flag = (
            (irr_delta is not None and irr_delta > CROSS_RUN_IRR_DELTA_THRESHOLD)
            or (em_delta is not None and em_delta > CROSS_RUN_EM_DELTA_THRESHOLD)
        )
        if triggers_flag:
            disagreement = True
        rows.append(
            {
                "source": str(prior),
                "irr": prior_irr,
                "em": prior_em,
                "irr_delta": irr_delta,
                "em_delta": em_delta,
                "triggers_disagreement": triggers_flag,
            }
        )
    return disagreement, rows


# ---------------------------------------------------------------------------
# Cohort collision detection (M-7)
# ---------------------------------------------------------------------------

def detect_cohort_collisions(canonical: dict) -> tuple[bool, list[str]]:
    """Return (has_collision, list of duplicate cohort_id values)."""
    seen: dict[str, int] = {}
    for cohort in canonical.get("unit_cohorts", []) or []:
        cid = cohort.get("cohort_id")
        if cid is None:
            continue
        seen[cid] = seen.get(cid, 0) + 1
    duplicates = [cid for cid, count in seen.items() if count > 1]
    return (len(duplicates) > 0, duplicates)


def annotate_cohort_table(canonical: dict) -> list[dict[str, Any]]:
    """Build §2 unit-mix rows. Duplicates get #1, #2 suffixes — never collapsed."""
    seen_count: dict[str, int] = {}
    rows: list[dict[str, Any]] = []
    cohorts = canonical.get("unit_cohorts", []) or []
    # First pass: count duplicates to know whether to suffix.
    counts: dict[str, int] = {}
    for cohort in cohorts:
        cid = cohort.get("cohort_id", "")
        counts[cid] = counts.get(cid, 0) + 1
    for cohort in cohorts:
        cid = cohort.get("cohort_id", "")
        seen_count[cid] = seen_count.get(cid, 0) + 1
        display_id = cid
        if counts.get(cid, 0) > 1:
            display_id = f"{cid} #{seen_count[cid]}"
        rows.append(
            {
                "display_cohort_id": display_id,
                "raw_cohort_id": cid,
                "bedrooms": cohort.get("bedrooms"),
                "bathrooms": cohort.get("bathrooms"),
                "count": cohort.get("count") or cohort.get("unit_count"),
                "current_rent": cohort.get("current_monthly_rent"),
                "target_rent": cohort.get("target_monthly_rent"),
            }
        )
    return rows


# ---------------------------------------------------------------------------
# Comp-evidence detection (Structural integrity flag)
# ---------------------------------------------------------------------------

def detect_missing_comp_evidence(
    *, run_root: Path, renovation_programs: list[dict]
) -> bool:
    if not renovation_programs:
        return False
    has_premium = any(
        (p.get("rent_premium_monthly") or 0) > 0 for p in renovation_programs
    )
    if not has_premium:
        return False
    comps_path = run_root / "market_study" / "comps.json"
    return not comps_path.is_file()


# ---------------------------------------------------------------------------
# Staleness guard (M-4)
# ---------------------------------------------------------------------------

def parse_prior_memo_generated_at(memo_path: Path) -> Optional[datetime]:
    if not memo_path.is_file():
        return None
    try:
        text = memo_path.read_text()
    except OSError:
        return None
    for line in text.splitlines():
        if line.startswith("**generated_at_utc:**"):
            iso = line.split("`", 2)
            if len(iso) >= 2:
                ts = iso[1].strip()
                try:
                    return datetime.fromisoformat(ts.replace("Z", "+00:00"))
                except ValueError:
                    return None
    return None


def staleness_check(
    *, run_root: Path, cited_paths: list[Path], recompose: bool = False
) -> tuple[bool, Optional[str]]:
    """Return (should_refuse, reason)."""
    memo_path = run_root / "memo.md"
    prior_at = parse_prior_memo_generated_at(memo_path)
    if prior_at is None:
        return False, None
    mtimes: list[float] = []
    for p in cited_paths:
        try:
            mtimes.append(p.stat().st_mtime)
        except (OSError, FileNotFoundError):
            continue
    if not mtimes:
        return False, None
    max_mtime = datetime.fromtimestamp(max(mtimes), tz=timezone.utc)
    if max_mtime > prior_at and not recompose:
        return True, (
            f"cited artifacts newer than prior memo ({max_mtime.isoformat()} "
            f"> {prior_at.isoformat()}). Pass recompose:true to override."
        )
    return False, None


# ---------------------------------------------------------------------------
# Top-level compose
# ---------------------------------------------------------------------------

def compose_memo(
    *,
    deal_slug: str,
    run_id: str,
    deal_root: Path,
    canonical: dict,
    deal_summary: dict,
    provenance: Optional[dict],
    renovation_programs: Optional[list[dict]] = None,
    composition_time_utc: Optional[datetime] = None,
    recompose: bool = False,
    extra_cited_paths: Optional[list[Path]] = None,
) -> MemoSimResult:
    """Run the full deal-memo-writer contract on the provided fixtures."""
    result = MemoSimResult()
    run_root = deal_root / "outputs" / run_id

    # --- Header (M-4) ---
    header, header_flags = build_header(
        deal_slug=deal_slug,
        run_id=run_id,
        provenance=provenance,
        composition_time_utc=composition_time_utc,
    )
    result.header = header
    for f in header_flags:
        if f not in result.structural_integrity_flags:
            result.structural_integrity_flags.append(f)

    # --- §2 unit mix + cohort collision (M-7) ---
    cohort_rows = annotate_cohort_table(canonical)
    result.cohort_table_rows = cohort_rows
    has_collision, _dups = detect_cohort_collisions(canonical)
    if has_collision:
        if "cohort_id_collision" not in result.structural_integrity_flags:
            result.structural_integrity_flags.append("cohort_id_collision")
        if "cohort_id_collision" not in result.sanity_flags:
            result.sanity_flags.append("cohort_id_collision")

    # --- Comp evidence ---
    if detect_missing_comp_evidence(
        run_root=run_root,
        renovation_programs=renovation_programs or [],
    ):
        if "comp_evidence_missing" not in result.structural_integrity_flags:
            result.structural_integrity_flags.append("comp_evidence_missing")

    # --- §5 metrics + sanity bands (M-2, M-3, M-6) ---
    metrics = (deal_summary.get("metrics") or {})
    irr = (metrics.get("irr") or {}).get("levered_irr")
    em = (metrics.get("equity_multiple") or {}).get("levered_em")
    dscr_min = (metrics.get("dscr") or {}).get("minimum_dscr")
    dscr_avg = (metrics.get("dscr") or {}).get("average_dscr")
    yields = metrics.get("yields") or {}
    going_in_raw = yields.get("going_in_cap_rate")
    exit_raw = yields.get("exit_cap_rate")

    cap_deriv = (provenance or {}).get("cap_rate_derivation") or {}
    going_in_derived = cap_deriv.get("going_in_derived_y1noi_over_price")
    exit_derived = cap_deriv.get("exit_derived_fwdnoi_over_saleprice")

    waterfall = (deal_summary.get("fund_waterfall") or {}).get("summary") or {}
    p_irr = waterfall.get("partnership_irr")
    p_em = waterfall.get("partnership_equity_multiple")

    # Authoritative source for sanity flags: provenance.feasibility_sanity_flags
    # (M-2 "PRIMARY"). Fallback: derive from bands (M-2 "FALLBACK").
    primary_flags = list(
        (provenance or {}).get("feasibility_sanity_flags") or []
    )
    derived_flags: list[str] = []

    def _verdict_for(metric_name: str, verdict_fn, value, *, primary_match: str):
        v, code = verdict_fn(value)
        # If the primary already flagged this metric, prefer the primary code.
        for pf in primary_flags:
            if pf.startswith(primary_match):
                return ("FLAG" if v in {"OK", "WARN"} else v), pf
        if code and code not in derived_flags:
            derived_flags.append(code)
        return v, code

    rows: list[dict[str, Any]] = []

    v, code = _verdict_for("levered_irr", irr_verdict, irr,
                            primary_match="irr_")
    rows.append({"metric": "levered_irr", "value": irr, "verdict": v, "flag": code,
                 "precision": 4, "source": "metrics.irr.levered_irr"})

    v, code = _verdict_for("equity_multiple", equity_multiple_verdict, em,
                            primary_match="equity_multiple_")
    rows.append({"metric": "equity_multiple", "value": em, "verdict": v, "flag": code,
                 "precision": 4, "source": "metrics.equity_multiple.levered_em"})

    v, code = _verdict_for("min_dscr", dscr_verdict, dscr_min,
                            primary_match="dscr_")
    rows.append({"metric": "min_dscr", "value": dscr_min, "verdict": v, "flag": code,
                 "precision": 2, "source": "metrics.dscr.minimum_dscr"})

    rows.append({"metric": "avg_dscr", "value": dscr_avg, "verdict": "OK",
                 "flag": None, "precision": 2,
                 "source": "metrics.dscr.average_dscr"})

    # Cap rate raw (M-6): show raw, then show derived alongside.
    v_raw, code_raw = _verdict_for(
        "going_in_cap_raw", cap_rate_verdict, going_in_raw,
        primary_match="cap_rate_",
    )
    rows.append({
        "metric": "going_in_cap_raw",
        "value": going_in_raw,
        "verdict": v_raw,
        "flag": code_raw,
        "precision": 2,
        "source": "metrics.yields.going_in_cap_rate",
    })
    v_der, code_der = cap_rate_verdict(going_in_derived)
    rows.append({
        "metric": "going_in_cap_derived",
        "value": going_in_derived,
        "verdict": v_der,
        "flag": code_der,
        "precision": 2,
        "source": "_provenance.cap_rate_derivation.going_in_derived_y1noi_over_price",
    })
    rows.append({
        "metric": "exit_cap_raw",
        "value": exit_raw,
        "verdict": cap_rate_verdict(exit_raw)[0],
        "flag": cap_rate_verdict(exit_raw)[1],
        "precision": 2,
        "source": "metrics.yields.exit_cap_rate",
    })
    rows.append({
        "metric": "exit_cap_derived",
        "value": exit_derived,
        "verdict": cap_rate_verdict(exit_derived)[0],
        "flag": cap_rate_verdict(exit_derived)[1],
        "precision": 2,
        "source": "_provenance.cap_rate_derivation.exit_derived_fwdnoi_over_saleprice",
    })

    # Partnership IRR / EM (M-3)
    v, code = _verdict_for(
        "partnership_irr", partnership_irr_verdict, p_irr,
        primary_match="partnership_irr_",
    )
    rows.append({"metric": "partnership_irr", "value": p_irr, "verdict": v,
                 "flag": code, "precision": 4,
                 "source": "fund_waterfall.summary.partnership_irr"})
    rows.append({"metric": "partnership_equity_multiple", "value": p_em,
                 "verdict": equity_multiple_verdict(p_em)[0],
                 "flag": equity_multiple_verdict(p_em)[1],
                 "precision": 4,
                 "source": "fund_waterfall.summary.partnership_equity_multiple"})
    rows.append({
        "metric": "gp_total_distribution",
        "value": waterfall.get("gp_total_distribution"),
        "verdict": "—", "flag": None, "precision": 0,
        "source": "fund_waterfall.summary.gp_total_distribution",
    })
    rows.append({
        "metric": "lp_total_distribution",
        "value": waterfall.get("lp_total_distribution"),
        "verdict": "—", "flag": None, "precision": 0,
        "source": "fund_waterfall.summary.lp_total_distribution",
    })

    result.metrics_table_rows = rows

    # Merge primary + derived flags into result.sanity_flags
    for f in primary_flags:
        if f not in result.sanity_flags:
            result.sanity_flags.append(f)
    for f in derived_flags:
        if f not in result.sanity_flags:
            result.sanity_flags.append(f)

    # --- Cross-run reconciliation (M-1) ---
    prior_paths = discover_prior_runs(deal_root, run_id)
    disagreement, cross_rows = reconcile_cross_runs(
        current_irr=irr, current_em=em, prior_paths=prior_paths
    )
    result.cross_run_table = cross_rows
    if disagreement:
        if "cross_run_disagreement" not in result.structural_integrity_flags:
            result.structural_integrity_flags.append("cross_run_disagreement")
        if "cross_run_disagreement" not in result.sanity_flags:
            result.sanity_flags.append("cross_run_disagreement")

    # --- Recommendation gating ---
    # If any structural integrity flag, downgrade from 'pursue' to
    # 'pursue-with-conditions'.
    if (provenance or {}).get("feasibility_verdict") == "fail":
        result.recommendation = "pass"
    elif result.structural_integrity_flags:
        result.recommendation = "pursue-with-conditions"
    elif (provenance or {}).get("feasibility_verdict") == "marginal":
        result.recommendation = "pursue-with-conditions"
    else:
        result.recommendation = "pursue"

    # --- Source-artifacts footer (M-4 freshness) ---
    cited: list[Path] = []
    for rel in [
        run_root / "underwriting" / "deal_summary.json",
        run_root / "underwriting" / "_provenance.json",
        run_root / "intake" / "canonical_deal.json",
    ]:
        if rel.is_file():
            cited.append(rel)
    if extra_cited_paths:
        cited.extend(extra_cited_paths)
    for p in prior_paths:
        if p.is_file():
            cited.append(p)
    footer: list[dict[str, Any]] = []
    for p in cited:
        try:
            mtime = datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc)
            footer.append({"path": str(p), "mtime_utc": mtime.isoformat()})
        except OSError:
            continue
    result.source_artifacts_footer = footer

    # --- Staleness guard (M-4) ---
    refuse, reason = staleness_check(
        run_root=run_root, cited_paths=cited, recompose=recompose
    )
    if refuse:
        result.refused_overwrite = True
        result.refusal_reason = reason

    return result
