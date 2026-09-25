"""Lifecycle memo subsystem (spec §2.5).

Assembles a one-pager screening memo (Markdown source-of-truth + optional PDF)
from upstream lifecycle artifacts. Wired into the orchestrator via MemoStep,
which implements LifecycleStep from plan-01.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

from plat_agent.lifecycle.atomic import atomic_write_text
from plat_agent.lifecycle.defaults import JUDGMENT_ENGINE_V1
from plat_agent.lifecycle.punchlist import read_punchlist_json
from plat_agent.lifecycle.state import BlockerItem


def _template_dir() -> Path:
    """Return the absolute path to the bundled Jinja2 template directory."""
    return Path(__file__).parent / "memo_templates"


@dataclass
class MemoContent:
    """Typed payload for memo rendering.

    Produced by assemble_memo_content(); consumed by render_memo_markdown()
    and the PDF integration. All numeric fields use floats (not Decimal) to
    match upstream JSON deserialization; formatting is the renderer's job.
    """
    deal_slug: str
    run_id: str
    address: str
    units: int
    vintage: int | None
    broker: str
    asking_price: float
    ppu: float | None
    recommendation: str
    recommendation_confidence: float
    thesis_text: str
    positioning: dict[str, Any]
    metrics: dict[str, float]   # levered_irr, levered_em, min_dscr, going_in_cap, exit_cap
    sanity_flags: list[str]
    uncleared_blockers: list[BlockerItem]
    artifact_paths: dict[str, str]
    judgment_mode: str
    passthrough: bool
    property_tax: dict[str, Any] | None = None
    draft_mode: bool = False  # set when underwriting absent or failed
    # Per V1.1 spec §1 + §4.6 (Demo Annex shape): true draft mode when canonical
    # is absent (intake blocked). The memo still renders, but with explicit
    # "data not available" placeholders + the punchlist embedded so the
    # analyst can see exactly what to drop in.
    draft_mode_canonical_absent: bool = False
    revenue_quality: dict[str, Any] | None = None
    insurance_review_per_unit: float | None = None


def _assemble_revenue_quality(
    run_dir: Path,
    canonical: dict[str, Any],
    deal_summary: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Return the audited ancillary-income bridge plus engine-net Year-1 credit."""
    bridge = _read_optional_json(
        run_dir / "reconciliation_house_case" / "revenue_quality_bridge.json"
    )
    if bridge is None:
        return None

    cashflow = (deal_summary or {}).get("cashflow", {})
    by_month = cashflow.get("by_month", [])
    first_twelve_programs = [
        row.get("net_programs")
        for row in by_month[:12]
    ]
    net_house_credit = None
    if (
        len(first_twelve_programs) == 12
        and all(value is not None for value in first_twelve_programs)
    ):
        net_house_credit = round(
            sum(float(value) for value in first_twelve_programs), 2
        )
    else:
        by_year = cashflow.get("by_year", [])
        start_date = canonical.get("time_grid", {}).get("analysis_start_date", "")
        first_full_year_index = 0 if start_date[5:7] == "01" else 1
        if len(by_year) > first_full_year_index:
            net_house_credit = by_year[first_full_year_index].get("net_programs")

    lines = bridge.get("lines", [])
    paired_expense_total = sum(
        float(line.get("paired_expense") or 0.0)
        for line in lines
    )
    return {
        "source_summary": bridge.get("source_summary", {}),
        "lines": lines,
        "net_house_credit": net_house_credit,
        "paired_expense_total": paired_expense_total,
    }


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def _read_optional_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return None

def collect_underwriting_sanity_flags(
    deal_summary: dict[str, Any] | None,
    underwriting_provenance: dict[str, Any] | None,
    engine_inputs: dict[str, Any] | None = None,
) -> list[str]:
    """Merge persisted risks and retain visible exit-cap compression."""
    provenance_flags = (
        list(underwriting_provenance.get("feasibility_sanity_flags", []) or [])
        if underwriting_provenance is not None
        else []
    )
    summary_flags = (
        list(deal_summary.get("sanity_flags", []) or [])
        if deal_summary is not None
        else []
    )
    flags = list(dict.fromkeys([*provenance_flags, *summary_flags]))
    has_exit_cap_risk = any(
        "exit_cap" in flag.lower()
        and ("going_in" in flag.lower() or "compression" in flag.lower())
        for flag in flags
    )
    yields = (deal_summary or {}).get("metrics", {}).get("yields", {})
    going_in_cap = yields.get("going_in_cap_rate")
    exit_cap = yields.get("exit_cap_rate")
    if (
        not has_exit_cap_risk
        and exit_cap is not None
        and going_in_cap is not None
        and exit_cap < going_in_cap
    ):
        flags.append("EXIT_CAP_LOWER_THAN_GOING_IN")
    property_summary = (
        (engine_inputs or {}).get("metadata", {}).get("property_summary", {})
    )
    if (
        property_summary.get("insurance_claim_review_required")
        and "INSURANCE_CLAIMS_HISTORY_REVIEW_REQUIRED" not in flags
    ):
        flags.append("INSURANCE_CLAIMS_HISTORY_REVIEW_REQUIRED")
    return flags


def assemble_memo_content(
    run_dir: Path,
    *,
    state: "LifecycleState | None" = None,
) -> MemoContent:
    """Read every upstream artifact and produce a typed MemoContent.

    Required:
      - intake/canonical_deal.json (per §2.1)
      - judgment/positioning.json (per §2.3)
      - _lifecycle_state.json (per §2.7)
    Optional (graceful degradation per §4.3):
      - comps/comps.json
      - judgment/thesis.md
      - underwriting/deal_summary.json
      - underwriting/_provenance.json (canonical home for feasibility_sanity_flags
        per src/plat_agent/contracts/domain/underwriting.py)
      - punchlist.json

    `state`: when provided (orchestrator hand-off), `state.judgment_mode` is
    the source of truth for the §5.4 passthrough banner. When omitted (legacy
    callers), we fall back to reading judgment_mode from _lifecycle_state.json.

    V1.1 graceful degradation (Demo Annex shape, spec §1 + §4.6): when
    intake/canonical_deal.json or judgment/positioning.json is absent (intake
    or judgment blocked/failed), MemoContent is still produced with
    `draft_mode_canonical_absent=True`. The renderer emits a "Draft — intake
    blocked" memo with the punchlist embedded and "data not available"
    placeholders for everything that depends on canonical/judgment data.
    """
    canonical_absent = not (run_dir / "intake" / "canonical_deal.json").exists()
    positioning_absent = not (run_dir / "judgment" / "positioning.json").exists()
    canonical = _read_optional_json(run_dir / "intake" / "canonical_deal.json") or {}
    positioning = _read_optional_json(run_dir / "judgment" / "positioning.json") or {}
    lifecycle_state = _read_optional_json(run_dir / "_lifecycle_state.json") or {}

    deal_summary = _read_optional_json(run_dir / "underwriting" / "deal_summary.json")
    underwriting_prov = _read_optional_json(run_dir / "underwriting" / "_provenance.json")
    engine_inputs = _read_optional_json(run_dir / "judgment" / "engine_inputs.json")
    comps = _read_optional_json(run_dir / "comps" / "comps.json")  # noqa: F841 — reserved for V2
    revenue_quality = _assemble_revenue_quality(run_dir, canonical, deal_summary)

    thesis_path = run_dir / "judgment" / "thesis.md"
    thesis_text = thesis_path.read_text().strip() if thesis_path.exists() \
        else "(thesis unavailable)"

    meta = canonical.get("metadata", {})
    property_summary = (meta.get("property_summary") or {})
    house_box = property_summary.get("house_box_score") or {}
    broker_box = property_summary.get("box_score") or {}
    canonical_units = sum((c.get("unit_count") or 0) for c in canonical.get("unit_cohorts", []))
    units = (
        canonical_units
        or house_box.get("total_units")
        or broker_box.get("total_units")
        or 0
    )
    asking = canonical.get("purchase_assumptions", {}).get("purchase_price", 0) or 0
    ppu = (asking / units) if units > 0 and asking > 0 else None

    metrics: dict[str, float] = {}
    sanity_flags: list[str] = []
    draft_mode = deal_summary is None
    if deal_summary is not None:
        m = deal_summary.get("metrics", {})
        if "irr" in m and m["irr"].get("levered_irr") is not None:
            metrics["levered_irr"] = m["irr"]["levered_irr"]
        if "equity_multiple" in m and m["equity_multiple"].get("levered_em") is not None:
            metrics["levered_em"] = m["equity_multiple"]["levered_em"]
        if "yields" in m:
            if m["yields"].get("going_in_cap_rate") is not None:
                metrics["going_in_cap"] = m["yields"]["going_in_cap_rate"]
            if m["yields"].get("exit_cap_rate") is not None:
                metrics["exit_cap"] = m["yields"]["exit_cap_rate"]
        if "dscr" in m and m["dscr"].get("minimum_dscr") is not None:
            metrics["min_dscr"] = m["dscr"]["minimum_dscr"]
    sanity_flags = collect_underwriting_sanity_flags(
        deal_summary, underwriting_prov, engine_inputs
    )

    uncleared = [b for b in read_punchlist_json(run_dir) if not b.cleared]
    if state is not None:
        seen_blockers = {(b.step, b.id) for b in uncleared}
        for blocker in state.blockers:
            if not blocker.cleared and (blocker.step, blocker.id) not in seen_blockers:
                uncleared.append(blocker)
                seen_blockers.add((blocker.step, blocker.id))

    # Spec §5.4: prefer the in-memory state (passed by orchestrator) over the
    # persisted file — the orchestrator always sets judgment_mode at the
    # start of run_lifecycle() but a legacy caller might invoke
    # assemble_memo_content() directly without re-persisting.
    if state is not None:
        judgment_mode = state.judgment_mode
    else:
        judgment_mode = lifecycle_state.get("judgment_mode", JUDGMENT_ENGINE_V1)
    passthrough = judgment_mode != JUDGMENT_ENGINE_V1

    artifact_paths = {
        "Canonical deal": str(run_dir / "intake" / "canonical_deal.json"),
        "Comps": str(run_dir / "comps" / "comps.json"),
        "Positioning": str(run_dir / "judgment" / "positioning.json"),
        "Thesis": str(run_dir / "judgment" / "thesis.md"),
        "Deal summary": str(run_dir / "underwriting" / "deal_summary.json"),
        "Underwriting provenance": str(run_dir / "underwriting" / "_provenance.json"),
        "Revenue-quality bridge": str(
            run_dir / "reconciliation_house_case" / "revenue_quality_bridge.json"
        ),
    }

    # V1.1: when canonical OR positioning is absent, the memo is in true
    # draft mode — no real metrics, no recommendation, just the punchlist
    # for the analyst.
    draft_mode_canonical_absent = canonical_absent or positioning_absent

    # Slug fallback chain: lifecycle state → canonical metadata → derive
    # from path (runs/deals/<slug>/outputs/<run_id> means slug is grandparent).
    deal_slug = (
        lifecycle_state.get("deal_slug")
        or meta.get("deal_id")
        or (run_dir.parent.parent.name if run_dir.parent.parent.exists() else "unknown")
    )

    engine_property_summary = (
        (engine_inputs or {}).get("metadata", {}).get("property_summary", {})
    )
    insurance_review_per_unit = (
        engine_property_summary.get("year1_insurance_per_unit_applied")
        if engine_property_summary.get("insurance_claim_review_required")
        else None
    )
    return MemoContent(
        deal_slug=deal_slug,
        run_id=lifecycle_state.get("run_id", run_dir.name),
        address=meta.get("address", "") or "",
        units=units,
        vintage=meta.get("year_built"),
        broker=meta.get("broker", "") or "",
        asking_price=float(asking),
        ppu=ppu,
        recommendation=positioning.get("recommendation") or "NEEDS_DATA",
        recommendation_confidence=float(positioning.get("recommendation_confidence") or 0.0),
        thesis_text=thesis_text,
        positioning=positioning,
        metrics=metrics,
        sanity_flags=sanity_flags,
        uncleared_blockers=uncleared,
        artifact_paths=artifact_paths,
        judgment_mode=judgment_mode,
        passthrough=passthrough,
        property_tax=(
            deal_summary.get("property_tax_calculation")
            if deal_summary is not None
            else None
        ),
        draft_mode=draft_mode,
        draft_mode_canonical_absent=draft_mode_canonical_absent,
        revenue_quality=revenue_quality,
        insurance_review_per_unit=insurance_review_per_unit,
    )


# Field-level config: which positioning.json keys are broker claims, how to label
# them, and how to format their values. Order is stable for deterministic memos.
_BROKER_CLAIM_FIELDS: list[tuple[str, str, str]] = [
    # (positioning_key, display_label, formatter_kind)
    ("rent_growth", "Rent growth", "pct"),
    ("capex_per_unit", "Capex per unit", "currency"),
    ("exit_cap", "Exit cap", "pct"),
    ("renovation_pace_units_per_month", "Reno pace (units/mo)", "number"),
]


@dataclass
class BrokerClaim:
    """One row in the memo's broker claim validation table."""
    label: str
    broker_fmt: str
    data_fmt: str
    selected_fmt: str
    delta_fmt: str
    flag: str  # "broker_optimistic" | "broker_underestimates" | "green" | ""


def _fmt_pct(v: float | None, decimals: int = 1) -> str:
    if v is None:
        return "—"
    return f"{v * 100:.{decimals}f}%"


def _fmt_currency(v: float | None) -> str:
    if v is None:
        return "—"
    return f"${v:,.0f}"


def _fmt_number(v: float | None) -> str:
    if v is None:
        return "—"
    if abs(v - round(v)) < 1e-9:
        return f"{int(round(v))}"
    return f"{v:.1f}"


def _fmt_for_kind(v: float | None, kind: str) -> str:
    if kind == "pct":
        return _fmt_pct(v)
    if kind == "currency":
        return _fmt_currency(v)
    return _fmt_number(v)


def _delta_pct(broker: float | None, data: float | None) -> str:
    if broker is None or data is None:
        return "—"
    if data == 0:
        return "n/a"
    pct = (broker - data) / data
    sign = "+" if pct > 0 else ""
    return f"{sign}{pct * 100:.1f}%"


def render_risks(
    *,
    sanity_flags: list[str],
    uncleared_blockers: list[BlockerItem],
) -> dict[str, Any]:
    """Build the Risks-section payload.

    Sanity flags render verbatim per §4.7 (no translation; analysts know the
    stable identifiers from engine.feasibility). Blockers stay typed so the
    template can render `step/id`.
    """
    seen: set[str] = set()
    deduped: list[str] = []
    for f in sanity_flags:
        if f in seen:
            continue
        seen.add(f)
        deduped.append(f)
    return {"risks": deduped, "blockers": list(uncleared_blockers)}


def _fmt_multiple(v: float | None) -> str:
    if v is None:
        return "—"
    return f"{v:.2f}x"


def render_returns(metrics: dict[str, float]) -> dict[str, str]:
    """Format metrics dict into the strings the Jinja template expects.

    Unset metrics render as '—' so draft-mode memos are still well-formed.
    """
    return {
        "levered_irr_fmt": _fmt_pct(metrics.get("levered_irr")),
        "levered_em_fmt": _fmt_multiple(metrics.get("levered_em")),
        "min_dscr_fmt": _fmt_multiple(metrics.get("min_dscr")),
        "going_in_cap_fmt": _fmt_pct(metrics.get("going_in_cap")),
        "exit_cap_fmt": _fmt_pct(metrics.get("exit_cap")),
    }


def render_broker_claims(positioning: dict[str, Any]) -> list[BrokerClaim]:
    """Translate positioning.json triples into ordered BrokerClaim rows."""
    rows: list[BrokerClaim] = []
    for key, label, kind in _BROKER_CLAIM_FIELDS:
        block = positioning.get(key)
        if not isinstance(block, dict):
            continue
        # Only emit a row when at least the data_derived OR selected is present
        # (a bare {selected: ...} with no triple is not a "claim" worth reconciling)
        if "data_derived" not in block and "selected" not in block:
            continue
        broker = block.get("om_claimed")
        data = block.get("data_derived")
        selected = block.get("selected")
        rows.append(BrokerClaim(
            label=label,
            broker_fmt=_fmt_for_kind(broker, kind),
            data_fmt=_fmt_for_kind(data, kind),
            selected_fmt=_fmt_for_kind(selected, kind),
            delta_fmt=_delta_pct(broker, data),
            flag=block.get("delta_flag", "") or "",
        ))
    return rows


def _jinja_env() -> Environment:
    return Environment(
        loader=FileSystemLoader(_template_dir()),
        autoescape=select_autoescape(default=False),  # markdown is plain text
        keep_trailing_newline=True,
        trim_blocks=True,
        lstrip_blocks=True,
    )


def render_memo_markdown(content: MemoContent) -> str:
    """Render the one-pager Markdown from a MemoContent.

    The Markdown is the source of truth (PDF is regenerated from it).
    """
    env = _jinja_env()

    banner = None
    if content.passthrough:
        banner = env.get_template("passthrough_banner.md.j2").render(
            judgment_mode=content.judgment_mode,
        )

    deal_view = {
        "slug": content.deal_slug,
        "address": content.address,
        "units": content.units,
        "vintage": content.vintage,
        "broker": content.broker,
        "asking_price": content.asking_price,
        "ppu": content.ppu,
    }
    returns_view = render_returns(content.metrics)
    risks_view = render_risks(
        sanity_flags=content.sanity_flags,
        uncleared_blockers=content.uncleared_blockers,
    )
    broker_claims = render_broker_claims(content.positioning)

    # V1.1: draft_mode_canonical_absent flag flips the template into a
    # "Draft — intake blocked" branch that renders the punchlist + analyst-
    # facing placeholders instead of the standard returns/broker-claims grid.
    return env.get_template("memo.md.j2").render(
        deal=deal_view,
        recommendation={
            "value": content.recommendation,
            "confidence": content.recommendation_confidence,
        },
        returns=returns_view,
        thesis_text=content.thesis_text,
        broker_claims=broker_claims,
        risks=risks_view["risks"],
        blockers=risks_view["blockers"],
        artifact_paths=content.artifact_paths,
        revenue_quality=content.revenue_quality,
        insurance_review_per_unit=content.insurance_review_per_unit,
        passthrough_banner=banner,
        property_tax=content.property_tax,
        draft_mode_canonical_absent=content.draft_mode_canonical_absent,
        deal_slug=content.deal_slug,
        run_id=content.run_id,
    )


def write_memo_markdown(run_dir: Path, content: MemoContent) -> Path:
    """Atomically write memo.md to <run_dir>/memo/memo.md."""
    md = render_memo_markdown(content)
    out = run_dir / "memo" / "memo.md"
    atomic_write_text(out, md)
    return out


from typing import Callable


@dataclass
class PDFRenderResult:
    status: str   # "ok" | "fallback" | "failed"
    path: Path | None
    detail: str | None = None


def _import_mfu_onepager() -> Callable | None:
    """Try to import mfu's generate_onepager. Returns None if unavailable.

    Cross-repo coordination per §6.A.3: developer-collocated repos. When
    multifamily-underwriting is `pip install -e`'d into the same venv as
    plat-agent, this import succeeds and we get the full branded PDF.
    """
    try:
        from engine.pdf_onepager import generate_onepager  # type: ignore[import-not-found]
        return generate_onepager
    except (ImportError, ModuleNotFoundError):
        return None


def _content_to_mfu_inputs(content: MemoContent) -> dict[str, Any]:
    """Translate MemoContent to the 'inputs' dict shape mfu's pdf_onepager expects.

    mfu's pdf_onepager reads `inputs.metadata.deal_id`, `inputs.metadata.address`,
    `inputs.fund_assumptions.jv_partner_name` (optional). We synthesize the
    minimum surface; full schema is in mfu's deal_schema_v0_1.json.
    """
    return {
        "metadata": {
            "deal_id": content.deal_slug,
            "address": content.address,
            "year_built": content.vintage,
            "broker": content.broker,
        },
        "purchase_assumptions": {"purchase_price": content.asking_price},
        "fund_assumptions": {},  # JV partner unset in V1
    }


def _content_to_mfu_results(content: MemoContent) -> dict[str, Any]:
    """Translate MemoContent metrics to the 'results' dict shape mfu expects."""
    return {
        "metrics": {
            "irr": {"levered_irr": content.metrics.get("levered_irr")},
            "equity_multiple": {"levered_em": content.metrics.get("levered_em")},
            "yields": {
                "going_in_cap_rate": content.metrics.get("going_in_cap"),
                "exit_cap_rate": content.metrics.get("exit_cap"),
            },
            "dscr": {"minimum_dscr": content.metrics.get("min_dscr")},
        },
        "sanity_flags": content.sanity_flags,
    }


def _reportlab_fallback(content: MemoContent, out: Path) -> None:
    """Pure-Python fallback: render the Markdown text into a minimal PDF.

    Raises RuntimeError if reportlab is itself unavailable. Caller treats as
    'failed' status (memo.md remains the source of truth per §2.5).
    """
    try:
        from reportlab.lib.pagesizes import letter
        from reportlab.lib.styles import getSampleStyleSheet
        from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer
    except ImportError as e:
        raise RuntimeError(f"reportlab not installed: {e}") from e

    out.parent.mkdir(parents=True, exist_ok=True)
    md = render_memo_markdown(content)
    doc = SimpleDocTemplate(str(out), pagesize=letter)
    styles = getSampleStyleSheet()
    story = []
    for line in md.splitlines():
        if not line.strip():
            story.append(Spacer(1, 6))
            continue
        # Escape angle brackets so reportlab doesn't try to parse pseudo-HTML
        safe = line.replace("<", "&lt;").replace(">", "&gt;")
        story.append(Paragraph(safe, styles["Normal"]))
    doc.build(story)


def write_memo_pdf(run_dir: Path, content: MemoContent) -> PDFRenderResult:
    """Render memo.pdf using mfu's pdf_onepager when available; otherwise fall back.

    Per spec §2.5, PDF is OPTIONAL — any failure returns a `PDFRenderResult`
    rather than raising. Caller logs the result to _provenance.json.

    Passthrough mode (spec §5.4): mfu's pdf_onepager renders from results/
    inputs and has no slot for the "TEST RUN — judgment layer bypassed"
    banner. To guarantee the banner reaches the PDF, passthrough runs go
    straight to the reportlab fallback (which renders the full memo
    Markdown including the banner). This keeps the spec §5.4 contract
    intact in every output mode.
    """
    out = run_dir / "memo" / "memo.pdf"
    out.parent.mkdir(parents=True, exist_ok=True)

    if content.passthrough:
        try:
            _reportlab_fallback(content, out)
            return PDFRenderResult(
                status="fallback",
                path=out,
                detail="passthrough mode: rendered via reportlab so banner is preserved",
            )
        except Exception as e:
            return PDFRenderResult(status="failed", path=None, detail=str(e))

    mfu_generate = _import_mfu_onepager()
    if mfu_generate is not None:
        try:
            mfu_generate(
                _content_to_mfu_results(content),
                _content_to_mfu_inputs(content),
                out,
            )
            return PDFRenderResult(status="ok", path=out)
        except Exception as e:
            # Try fallback rather than failing outright
            try:
                _reportlab_fallback(content, out)
                return PDFRenderResult(
                    status="fallback",
                    path=out,
                    detail=f"mfu pdf_onepager raised: {e}; used reportlab fallback",
                )
            except Exception as e2:
                return PDFRenderResult(
                    status="failed",
                    path=None,
                    detail=f"mfu raised {e}; fallback raised {e2}",
                )

    # mfu unavailable → fallback path
    try:
        _reportlab_fallback(content, out)
        return PDFRenderResult(
            status="fallback",
            path=out,
            detail="mfu pdf_onepager not importable; used reportlab fallback",
        )
    except Exception as e:
        return PDFRenderResult(status="failed", path=None, detail=str(e))


from plat_agent.lifecycle.cache import compute_input_hash, is_satisfied, write_provenance
from plat_agent.lifecycle.complete_marker import write_complete_marker
from plat_agent.lifecycle.defaults import CONTRACT_VERSION
from plat_agent.lifecycle.protocol import StepResult
from plat_agent.lifecycle.state import LifecycleState


class MemoStep:
    """LifecycleStep implementation for the memo subsystem.

    Per spec §4.3 memo never produces blockers — even draft mode is "ok".
    PDF failure is logged in _provenance.json.pdf_status but does not change
    the step status (PDF is OPTIONAL per §2.5).
    """

    name = "memo"

    def run(self, state: LifecycleState, run_dir: Path) -> StepResult:
        # 1. Assemble content from upstream artifacts.
        # Pass `state` so judgment_mode (spec §5.4 passthrough) is read from
        # the orchestrator-owned in-memory state, not just the JSON file.
        try:
            content = assemble_memo_content(run_dir, state=state)
        except FileNotFoundError as e:
            # Required upstream missing → hard error per §4.1
            return StepResult(status="error", error_message=str(e))

        # 2. Compute input hash over the upstream artifacts we read.
        # Includes underwriting/_provenance.json because that's the canonical
        # home of feasibility_sanity_flags (per contracts/domain/underwriting.py)
        # which the memo embeds — its content drives memo output, so changes
        # must invalidate the memo cache.
        input_paths = [
            run_dir / "intake" / "canonical_deal.json",
            run_dir / "comps" / "comps.json",
            run_dir / "judgment" / "positioning.json",
            run_dir / "judgment" / "thesis.md",
            run_dir / "underwriting" / "deal_summary.json",
            run_dir / "underwriting" / "_provenance.json",
        ]
        input_hash = compute_input_hash(input_paths)

        # 3. Render + write Markdown (always succeeds if assembly did)
        memo_md_path = write_memo_markdown(run_dir, content)

        # 4. Render + write PDF (best-effort).
        # V1.1: skip PDF entirely in canonical-absent draft mode — no metrics
        # to render, and reportlab/mfu both expect a populated content shape.
        # The Markdown remains the source of truth (spec §2.5).
        if content.draft_mode_canonical_absent:
            pdf_result = PDFRenderResult(
                status="skipped",
                path=None,
                detail="canonical_deal.json absent; PDF skipped in draft mode",
            )
        else:
            pdf_result = write_memo_pdf(run_dir, content)

        # 5. Build manifest (PDF only included when actually written)
        manifest = ["memo.md", "_provenance.json"]
        if pdf_result.status in ("ok", "fallback") and pdf_result.path is not None \
                and pdf_result.path.exists():
            manifest.insert(1, "memo.pdf")

        # 6. Write provenance.
        # `memo_path` (and optionally `pdf_path`) are persisted so downstream
        # steps — notably plan-04 CRM — can read them from a single canonical
        # location instead of re-deriving by convention.
        memo_pdf_path = (
            str(pdf_result.path.resolve())
            if pdf_result.path is not None and pdf_result.path.exists()
            else None
        )
        write_provenance(
            run_dir / "memo",
            input_hash=input_hash,
            contract_version=CONTRACT_VERSION,
            extra={
                "judgment_mode": content.judgment_mode,
                "memo_path": str(memo_md_path.resolve()),
                "pdf_path": memo_pdf_path,
                "pdf_status": pdf_result.status,
                "pdf_detail": pdf_result.detail,
                "draft_mode": content.draft_mode,
                "draft_mode_canonical_absent": content.draft_mode_canonical_absent,
                "passthrough": content.passthrough,
            },
        )

        # 7. Write _complete marker as the LAST action (per §4.4.1)
        write_complete_marker(run_dir / "memo", step="memo", file_manifest=manifest)

        return StepResult(status="ok")

    def is_satisfied(self, state: LifecycleState, run_dir: Path) -> bool:
        # Compute current input hash for cache validity check.
        # Must include underwriting/_provenance.json (sanity flags location).
        input_paths = [
            run_dir / "intake" / "canonical_deal.json",
            run_dir / "comps" / "comps.json",
            run_dir / "judgment" / "positioning.json",
            run_dir / "judgment" / "thesis.md",
            run_dir / "underwriting" / "deal_summary.json",
            run_dir / "underwriting" / "_provenance.json",
        ]
        return is_satisfied(state, run_dir, step="memo",
                             current_input_hash=compute_input_hash(input_paths))
