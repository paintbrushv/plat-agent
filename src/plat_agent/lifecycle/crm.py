"""CRM subsystem — append-only JSONL ledger of every lifecycle run.

Per spec §2.6:
  - Single file at runs/deals/_crm.jsonl (slug-level, all deals share it).
  - One row per (deal_slug, run_id, ts); re-runs APPEND a new row so full
    history is preserved. crm.get_latest(deal_slug) returns the most recent.
  - Append-only writes use fcntl.LOCK_EX so concurrent runs writing different
    deals don't corrupt the file (spec §3 Concurrency).

Per spec §3 Step 6:
  - The CRM step receives the FINAL lifecycle state values in-memory from the
    orchestrator. CRM does NOT read _lifecycle_state.json — by that point the
    orchestrator has computed `status="memo_ready"`, `finished_at`, etc., and
    passes them into book_deal(). The orchestrator persists state to disk
    AFTER CRM appends, fixing the staleness bug documented in §3.

Public API:
  - CRMRow                           — pydantic row schema
  - book_deal(...)                   — assemble + atomic-append a row
  - CRMStep                          — LifecycleStep adapter (used by orchestrator)
  - list_deals(filter=...)           — read helper
  - get_latest(deal_slug)            — read helper
  - crm_path(runs_root)              — path resolver
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field
from plat_agent.lifecycle.memo import collect_underwriting_sanity_flags


CRM_FILENAME = "_crm.jsonl"


def crm_path(runs_deals_root: Path) -> Path:
    """Return the absolute Path to the slug-level CRM JSONL file.

    runs_deals_root is the `runs/deals/` directory (parent of all per-deal
    subdirs). Resolver is pure — does not create the file or its parent.
    """
    return Path(runs_deals_root) / CRM_FILENAME


# Status strings mirror lifecycle.state.LifecycleStatus but kept loose here so
# CRM rows can record any status the orchestrator emits, including future V2
# values. Consumers should treat this as an opaque string.
CRMStatus = str


# RecommendationEnum is re-exported from state.py for convenience but accepted
# as a plain Literal in the row so analysts editing JSONL by hand stay valid.
CRMRecommendation = Literal["PROCEED", "DECLINE", "NEEDS_DATA"]


class CRMRow(BaseModel):
    """One row in runs/deals/_crm.jsonl.

    Field set is the spec §2.6 column set + Decision 6 (15 fields). Required
    fields fail validation if missing; Optional fields default to None or 0.
    """

    # Required (spec §2.6)
    deal_slug: str
    run_id: str
    ts: datetime
    status: CRMStatus
    recommendation: CRMRecommendation
    memo_path: str
    address: str
    units: int
    asking_price: float
    property_name: str | None = None

    # Optional (may be null when underwriting failed, didn't run, or source
    # data was absent; per §2.6 read helpers must tolerate null in any of these)
    levered_irr: float | None = None
    equity_multiple: float | None = None
    going_in_cap: float | None = None
    property_tax_millage_rate_mills: float | None = None
    property_tax_rate_pct: float | None = None
    property_tax_assessment_ratio: float | None = None
    property_tax_purchase_price_basis: float | None = None
    property_tax_assessed_value_basis: float | None = None
    property_tax_annual_ad_valorem_tax: float | None = None
    property_tax_source: str | None = None
    property_tax_source_locator: str | None = None
    property_tax_analyst_override: bool | None = None
    # V1.5 — Year-1 Cash-on-Cash (NOI net of capex, AM fees, partnership exp)
    # / total_equity_basis. Source: deal_summary.metrics.coc.cash_on_cash_year_1
    # (with fallback to deal_summary.metrics.cash_on_cash.cash_on_cash_year_1).
    cash_on_cash_year_1: float | None = None
    ppu: float | None = None
    vintage: int | None = None
    blocker_count: int = 0
    sanity_flag_count: int = 0
    recommendation_confidence: float | None = None
    workbook_path: str | None = None
    thesis_path: str | None = None
    judgment_mode_override: str | None = None  # set only when != "deterministic_v1"


import fcntl


def atomic_append(jsonl_path: Path, row: "CRMRow") -> None:
    """Append a single CRMRow as a JSONL line under fcntl.LOCK_EX.

    Per spec §3 Concurrency: concurrent runs writing different deals must not
    corrupt the file. LOCK_EX is acquired on the open file descriptor before
    write and released at close.

    Creates parent directories if missing. Each row terminates with `\\n` so
    the file is always valid JSONL even when interleaved with other writers
    (each writer's payload is one whole line, written under the lock).
    """
    jsonl_path = Path(jsonl_path)
    jsonl_path.parent.mkdir(parents=True, exist_ok=True)
    line = row.model_dump_json() + "\n"
    # Open with "a" so the OS appends; LOCK_EX serializes concurrent writers.
    with open(jsonl_path, "a", encoding="utf-8") as f:
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            f.write(line)
            f.flush()
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


from typing import Any

from plat_agent.lifecycle.state import LifecycleState


JUDGMENT_ENGINE_DEFAULT = "deterministic_v1"


def _safe_get(d: dict | None, *keys: str, default: Any = None) -> Any:
    """Walk a nested dict by keys; return default if any key is missing."""
    if d is None:
        return default
    cur: Any = d
    for k in keys:
        if not isinstance(cur, dict) or k not in cur:
            return default
        cur = cur[k]
    return cur


def _first_nonempty(*values: Any) -> Any:
    for value in values:
        if value not in (None, ""):
            return value
    return None


def _derive_property_name(*deals: dict | None) -> str | None:
    for deal in deals:
        metadata = _safe_get(deal, "metadata", default={}) or {}
        raw = _first_nonempty(
            metadata.get("property_name"),
            metadata.get("deal_name"),
            metadata.get("deal_id"),
        )
        if not raw:
            continue
        name = str(raw).replace("_", " ")
        name = re.sub(r"\s+", " ", name).strip(" -_")
        name = re.sub(r"\b(ipa|texas|om|offering memorandum|pdf)\b", "", name, flags=re.I)
        name = re.sub(r"\s+", " ", name).strip(" -_")
        if name:
            return name
    return None


def book_deal(
    *,
    crm_jsonl_path: Path,
    canonical_deal: dict | None,
    positioning: dict | None,
    deal_summary: dict | None,
    memo_provenance: dict | None,
    underwriting_provenance: dict | None,
    judgment_provenance: dict | None,
    final_state: LifecycleState,
    engine_inputs: dict | None = None,
) -> CRMRow:
    """Assemble a CRMRow from upstream artifacts + final lifecycle state, then
    atomically append it to crm_jsonl_path. Returns the row that was written.

    Per spec §3 Step 6: final_state is passed in-memory by the orchestrator
    AFTER it has set status to its final value (e.g., "memo_ready"). CRM does
    NOT read _lifecycle_state.json from disk.

    Per spec §2.6 graceful degradation (V1.1, surfaced by Demo Annex real-world
    run): when intake or judgment was blocked/failed, the upstream artifacts
    may be absent. CRM still writes a row — required fields fall back to
    sensible defaults derivable from lifecycle state, and optional fields go
    null. recommendation defaults to NEEDS_DATA when positioning is absent.

    Extraction map per §2.6:
      - deal_slug, run_id              ← final_state
      - ts                             ← final_state.finished_at
      - status                         ← final_state.status
      - blocker_count                  ← len(positioning.blockers) + len(final_state.blockers)
      - sanity_flag_count              ← len(deal_summary.sanity_flags) (or 0)
      - address                        ← final engine inputs, then canonical_deal.metadata.address
      - property_name                  ← final engine inputs, then canonical_deal metadata
      - units                          ← sum(final engine inputs or canonical_deal.unit_cohorts[].unit_count)
      - vintage                        ← final engine inputs, then canonical_deal.metadata.year_built
      - asking_price                   ← final engine inputs, then canonical_deal.purchase_assumptions.purchase_price
      - ppu                            ← computed: asking_price / units
      - levered_irr                    ← deal_summary.metrics.irr.levered_irr
      - equity_multiple                ← deal_summary.metrics.equity_multiple.levered_em
      - going_in_cap                   ← deal_summary.metrics.yields.going_in_cap_rate
      - recommendation                 ← positioning.recommendation (or NEEDS_DATA)
      - recommendation_confidence      ← positioning.recommendation_confidence
      - memo_path                      ← memo_provenance.memo_path (or "")
      - workbook_path                  ← underwriting_provenance.workbook_path
      - thesis_path                    ← judgment_provenance.thesis_path
      - judgment_mode_override         ← set when judgment_engine != "deterministic_v1"
    """
    # --- Required fields, graceful when artifacts are absent ---
    # Prefer judgment/engine_inputs.json because it is the priced final input
    # surface. intake/canonical_deal.json can legitimately be pre-pricing.
    address = _first_nonempty(
        _safe_get(engine_inputs, "metadata", "address"),
        _safe_get(canonical_deal, "metadata", "address"),
    ) or ""
    property_name = _derive_property_name(engine_inputs, canonical_deal)
    cohorts = (
        _safe_get(engine_inputs, "unit_cohorts", default=None)
        or _safe_get(canonical_deal, "unit_cohorts", default=[])
        or []
    )
    units = sum(int(c.get("unit_count", 0)) for c in cohorts)
    asking_price = _first_nonempty(
        _safe_get(engine_inputs, "purchase_assumptions", "purchase_price"),
        _safe_get(canonical_deal, "purchase_assumptions", "purchase_price"),
    )
    if asking_price is None:
        asking_price = 0.0

    # --- Optional metrics from deal_summary (engine field-name mismatch handled here) ---
    levered_irr = _safe_get(deal_summary, "metrics", "irr", "levered_irr")
    # Engine field is metrics.equity_multiple.levered_em; CRM column is equity_multiple
    equity_multiple = _safe_get(deal_summary, "metrics", "equity_multiple", "levered_em")
    going_in_cap = _safe_get(deal_summary, "metrics", "yields", "going_in_cap_rate")
    # V1.5 — Year-1 CoC. Prefer the federation-friendly coc.cash_on_cash_year_1;
    # fall back to cash_on_cash.cash_on_cash_year_1 (legacy block).
    cash_on_cash_year_1 = _safe_get(
        deal_summary, "metrics", "coc", "cash_on_cash_year_1"
    )
    if cash_on_cash_year_1 is None:
        cash_on_cash_year_1 = _safe_get(
            deal_summary, "metrics", "cash_on_cash", "cash_on_cash_year_1"
        )
    property_tax = (
        _safe_get(deal_summary, "property_tax_calculation", default={}) or {}
    )
    decimal_tax_rate = property_tax.get("decimal_tax_rate")
    sanity_flags = collect_underwriting_sanity_flags(
        deal_summary, underwriting_provenance, engine_inputs
    )

    # --- Blockers: positioning + final_state, deduped is NOT required (count is len) ---
    pos_blockers = _safe_get(positioning, "blockers", default=[]) or []
    state_blockers = list(final_state.blockers)
    blocker_count = len(pos_blockers) + len(state_blockers)

    # --- Paths (absolute) from each provenance file ---
    # memo_path is "required" in the row schema, so default to "" when memo
    # provenance is absent (e.g., MemoStep itself failed before writing it).
    memo_path = _safe_get(memo_provenance, "memo_path") or ""
    workbook_path = _safe_get(underwriting_provenance, "workbook_path")
    thesis_path = _safe_get(judgment_provenance, "thesis_path")

    # --- judgment_mode_override: only set when nondefault (§5.4 passthrough guardrail) ---
    # Source of truth: final_state.judgment_mode (set by the orchestrator at
    # run_lifecycle entry from the `judgment_mode` arg). Falls back to the
    # judgment provenance value when state lacks the field — this preserves
    # backward compatibility for any caller that hand-rolls a LifecycleState
    # without setting judgment_mode (e.g. older fixtures, tests targeting
    # CRMStep in isolation).
    state_mode = getattr(final_state, "judgment_mode", None)
    if state_mode and state_mode != JUDGMENT_ENGINE_DEFAULT:
        judgment_mode_override = state_mode
    elif state_mode == JUDGMENT_ENGINE_DEFAULT:
        judgment_mode_override = None
    else:
        # state.judgment_mode unset → fall back to judgment provenance
        judgment_engine = _safe_get(
            judgment_provenance, "judgment_engine", default=JUDGMENT_ENGINE_DEFAULT
        )
        judgment_mode_override = (
            judgment_engine if judgment_engine != JUDGMENT_ENGINE_DEFAULT else None
        )

    # --- ppu: computed if both asking_price and units are present and units > 0 ---
    ppu: float | None = None
    if asking_price is not None and units:
        ppu = float(asking_price) / float(units)

    # recommendation defaults to NEEDS_DATA when positioning is absent
    # (intake/judgment blocked → no positioning.json). Per spec §2.6 V1.1.
    recommendation = _safe_get(positioning, "recommendation") or "NEEDS_DATA"

    row = CRMRow(
        deal_slug=final_state.deal_slug,
        run_id=final_state.run_id,
        ts=final_state.finished_at or datetime.now(),
        status=final_state.status,
        recommendation=recommendation,
        memo_path=memo_path,
        address=address,
        units=units,
        asking_price=asking_price,
        property_name=property_name,
        levered_irr=levered_irr,
        equity_multiple=equity_multiple,
        going_in_cap=going_in_cap,
        property_tax_millage_rate_mills=property_tax.get("millage_rate_mills"),
        property_tax_rate_pct=(
            decimal_tax_rate * 100
            if decimal_tax_rate is not None
            else None
        ),
        property_tax_assessment_ratio=property_tax.get("assessment_ratio"),
        property_tax_purchase_price_basis=property_tax.get(
            "purchase_price_basis"
        ),
        property_tax_assessed_value_basis=property_tax.get(
            "assessed_value_basis"
        ),
        property_tax_annual_ad_valorem_tax=property_tax.get(
            "annual_ad_valorem_tax"
        ),
        property_tax_source=property_tax.get("source"),
        property_tax_source_locator=property_tax.get("source_locator"),
        property_tax_analyst_override=property_tax.get("analyst_override"),
        cash_on_cash_year_1=cash_on_cash_year_1,
        ppu=ppu,
        vintage=_first_nonempty(
            _safe_get(engine_inputs, "metadata", "year_built"),
            _safe_get(canonical_deal, "metadata", "year_built"),
        ),
        blocker_count=blocker_count,
        sanity_flag_count=len(sanity_flags),
        recommendation_confidence=_safe_get(positioning, "recommendation_confidence"),
        workbook_path=workbook_path,
        thesis_path=thesis_path,
        judgment_mode_override=judgment_mode_override,
    )
    atomic_append(crm_jsonl_path, row)
    return row


def list_deals(
    crm_jsonl_path: Path,
    *,
    filter: dict[str, Any] | None = None,
) -> list[CRMRow]:
    """Read all CRM rows. Returns [] if the file does not exist.

    Each line is parsed via CRMRow.model_validate_json — null in any Optional
    field is tolerated per §2.6.

    `filter` is an exact-match dict on row attributes (e.g.,
    {"deal_slug": "a", "recommendation": "PROCEED"}). All keys must match.
    """
    crm_jsonl_path = Path(crm_jsonl_path)
    if not crm_jsonl_path.exists():
        return []
    rows: list[CRMRow] = []
    for line in crm_jsonl_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        if filter:
            raw = json.loads(line)
            if any(raw.get(k) != v for k, v in filter.items()):
                continue
        rows.append(CRMRow.model_validate_json(line))
    if filter:
        rows = [r for r in rows if all(getattr(r, k, None) == v for k, v in filter.items())]
    return rows


def get_latest(crm_jsonl_path: Path, deal_slug: str) -> CRMRow | None:
    """Return the row with the most recent `ts` for `deal_slug`, or None.

    Re-runs of the same (deal_slug, run_id) append a new row per §2.6;
    this returns whichever has the latest timestamp.
    """
    rows = list_deals(crm_jsonl_path, filter={"deal_slug": deal_slug})
    if not rows:
        return None
    return max(rows, key=lambda r: r.ts)


from plat_agent.lifecycle.atomic import atomic_write_json
from plat_agent.lifecycle.cache import compute_input_hash, is_satisfied as _cache_is_satisfied, write_provenance
from plat_agent.lifecycle.complete_marker import write_complete_marker
from plat_agent.lifecycle.protocol import StepResult


class CRMStep:
    """LifecycleStep adapter: reads upstream artifacts off disk, calls
    book_deal(), writes its own _provenance.json + _complete marker.

    The orchestrator passes the FINAL `state` (status already set to its
    terminal value) into run() — see spec §3 Step 6.
    """

    name = "crm"

    def __init__(self, runs_deals_root: Path) -> None:
        self.runs_deals_root = Path(runs_deals_root)

    def run(self, state: LifecycleState, run_dir: Path) -> StepResult:
        intake_canon = run_dir / "intake" / "canonical_deal.json"
        engine_inputs_path = run_dir / "judgment" / "engine_inputs.json"
        positioning_path = run_dir / "judgment" / "positioning.json"
        deal_summary_path = run_dir / "underwriting" / "deal_summary.json"
        memo_prov_path = run_dir / "memo" / "_provenance.json"
        uw_prov_path = run_dir / "underwriting" / "_provenance.json"
        judgment_prov_path = run_dir / "judgment" / "_provenance.json"

        # V1.1 graceful degradation (Demo Annex shape): when intake / judgment /
        # memo / underwriting blocked or failed, their artifacts may not
        # exist. CRM still writes a row — book_deal() tolerates Nones and
        # falls back to lifecycle-state-derived defaults.
        canonical = (
            json.loads(intake_canon.read_text()) if intake_canon.exists() else None
        )
        engine_inputs = (
            json.loads(engine_inputs_path.read_text()) if engine_inputs_path.exists() else None
        )
        positioning = (
            json.loads(positioning_path.read_text()) if positioning_path.exists() else None
        )
        deal_summary = (
            json.loads(deal_summary_path.read_text()) if deal_summary_path.exists() else None
        )
        memo_provenance = (
            json.loads(memo_prov_path.read_text()) if memo_prov_path.exists() else None
        )
        underwriting_provenance = (
            json.loads(uw_prov_path.read_text()) if uw_prov_path.exists() else None
        )
        judgment_provenance = (
            json.loads(judgment_prov_path.read_text()) if judgment_prov_path.exists() else None
        )

        crm_jsonl = crm_path(self.runs_deals_root)
        book_deal(
            crm_jsonl_path=crm_jsonl,
            canonical_deal=canonical,
            positioning=positioning,
            deal_summary=deal_summary,
            memo_provenance=memo_provenance,
            underwriting_provenance=underwriting_provenance,
            judgment_provenance=judgment_provenance,
            final_state=state,
            engine_inputs=engine_inputs,
        )

        # Write step's own provenance + _complete marker (per spec §4.4.1).
        # The CRM JSONL row payload is also serialized to a step-local marker
        # (`crm_row.json`) so the manifest can witness that this step actually
        # produced output — the shared `_crm.jsonl` lives outside step_dir
        # (one file across all deals) and cannot be listed in the manifest.
        crm_step_dir = run_dir / "crm"
        crm_step_dir.mkdir(parents=True, exist_ok=True)
        # input_hash spans the upstream artifacts we consumed.
        # NOTE: compute_input_hash() (plan-01 foundations) tolerates missing
        # files — every path contributes the delimiter regardless of presence,
        # so absent vs present always hash differently. This means CRM does
        # NOT need to short-circuit when underwriting/memo failed: the same
        # path list is hashed and the resulting hash naturally reflects the
        # absence (and `is_satisfied()` will reject any cached state when
        # those upstream files appear/disappear).
        input_paths = [
            intake_canon, engine_inputs_path, positioning_path, deal_summary_path,
            memo_prov_path, uw_prov_path, judgment_prov_path,
        ]
        input_hash = compute_input_hash(input_paths)

        # crm_row.json — step-local witness of the row appended to _crm.jsonl
        # for this run. Same payload, easy to diff during debugging.
        latest_row = get_latest(crm_jsonl, deal_slug=state.deal_slug)
        if latest_row is not None:
            atomic_write_json(crm_step_dir / "crm_row.json", latest_row.model_dump(mode="json"))

        write_provenance(crm_step_dir, input_hash=input_hash)
        write_complete_marker(
            crm_step_dir,
            step="crm",
            file_manifest=["_provenance.json", "crm_row.json"],
        )

        return StepResult(status="ok")

    def is_satisfied(self, state: LifecycleState, run_dir: Path) -> bool:
        # Mirror run()'s input_paths exactly. compute_input_hash tolerates
        # missing files (per plan-01 foundations) so this works whether or
        # not underwriting/memo produced their provenance.
        intake_canon = run_dir / "intake" / "canonical_deal.json"
        engine_inputs_path = run_dir / "judgment" / "engine_inputs.json"
        positioning_path = run_dir / "judgment" / "positioning.json"
        deal_summary_path = run_dir / "underwriting" / "deal_summary.json"
        memo_prov_path = run_dir / "memo" / "_provenance.json"
        uw_prov_path = run_dir / "underwriting" / "_provenance.json"
        judgment_prov_path = run_dir / "judgment" / "_provenance.json"
        input_paths = [
            intake_canon, engine_inputs_path, positioning_path, deal_summary_path,
            memo_prov_path, uw_prov_path, judgment_prov_path,
        ]
        input_hash = compute_input_hash(input_paths)
        return _cache_is_satisfied(state, run_dir, step="crm", current_input_hash=input_hash)
