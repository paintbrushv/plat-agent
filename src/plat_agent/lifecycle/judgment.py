"""Judgment subsystem — V1 deterministic rules engine.

Reads intake/canonical_deal.json + (optional) comps/comps.json. Produces:
  - positioning.json — schema per spec §2.3
  - thesis.md — 3–4 paragraph narrative
  - engine_inputs.json — flat schema-v0.1 inputs derived from positioning.*.selected
  - _provenance.json — audit trail
  - _complete — atomic step commit marker

`recommendation` and `recommendation_confidence` are NOT produced here; the
orchestrator patches them in post-step (spec §3 Step 4.5) once underwriting
metrics are available.

Implementation V1 is deterministic Python rules (DeterministicJudgmentEngine).
V2 will swap in an LLM-backed engine behind the same `JudgmentEngine` protocol.
"""

from __future__ import annotations

from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from plat_agent.lifecycle.state import BlockerItem


# Valid delta_flag values — directional/editorial labels surfaced in memos
# and thesis copy. The threshold-based severity (green/yellow/red per spec
# §1 V1 Defaults: yellow at ±15%, red at ±25%) lives on `delta_severity` so
# downstream counts (provenance summary, dashboards) preserve the spec bands
# regardless of direction.
DeltaFlag = Literal[
    "green",
    "yellow",
    "red",
    "broker_optimistic",
    "broker_underestimates",
]

# Threshold-based severity (matches spec §1 V1 Defaults). Always one of
# green/yellow/red regardless of whether the editorial `delta_flag` is
# directional ("broker_optimistic" / "broker_underestimates").
DeltaSeverity = Literal["green", "yellow", "red"]

PositioningValue = Literal["value_add", "stabilized", "core_plus", "opportunistic"]


class PositioningClass(BaseModel):
    """High-level deal classification (spec §2.3)."""

    value: PositioningValue
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str


class ValidatedField(BaseModel):
    """Per-assumption broker-claim validation envelope (spec §2.3).

    `data_derived` — what the data (rent roll, T12, comps) implies.
    `om_claimed` — what the broker OM claims (None if no OM claim available).
    `selected` — what judgment chose to use (typically conservative-of-the-two).
    `confidence` — judgment's confidence in `selected`.
    `delta_flag` — editorial/directional label for memos (see DeltaFlag).
    `delta_severity` — threshold-based severity (green/yellow/red) per spec
        §1 V1 Defaults. Preserved separately so counts/dashboards keep the
        spec bands even when the editorial flag is directional.
    """

    data_derived: float | None = None
    om_claimed: float | None = None
    selected: float
    confidence: float = Field(ge=0.0, le=1.0)
    delta_flag: DeltaFlag = "green"
    delta_severity: DeltaSeverity = "green"
    rationale: str | None = None


class JudgmentResult(BaseModel):
    """Output of a JudgmentEngine.evaluate() call. Serialized to positioning.json.

    Per spec §2.3, `recommendation` and `recommendation_confidence` are populated
    by the orchestrator post-step (§3 Step 4.5), NOT by judgment proper.
    """

    positioning: PositioningClass
    capex_per_unit: ValidatedField | None = None
    renovation_pace_units_per_month: ValidatedField | None = None
    rent_growth: ValidatedField | None = None
    exit_cap: ValidatedField | None = None
    leverage: dict[str, Any]
    engine_inputs_relative: str
    # NOTE: spec §2.3 does NOT define a `comps_unavailable` field on
    # positioning.json. When the comps step produced no comps.json, judgment
    # signals it via a BlockerItem(step="judgment", id="comps_unavailable")
    # in `blockers` — see DeterministicJudgmentEngine.evaluate(). Downstream
    # consumers (memo, recommendation derivation) inspect `blockers` rather
    # than a dedicated boolean.
    blockers: list[BlockerItem] = Field(default_factory=list)
    # Patched by orchestrator post-underwriting (§3 Step 4.5) — null until then
    recommendation: Literal["PROCEED", "DECLINE", "NEEDS_DATA"] | None = None
    recommendation_confidence: float | None = None


@runtime_checkable
class JudgmentEngine(Protocol):
    """Pluggable V1/V2 judgment seam.

    V1 implementation is `DeterministicJudgmentEngine` (rules in judgment_rules.py).
    V2 will be an LLM-backed implementation that wraps V1 as a fallback/validator.
    """

    name: str

    def evaluate(self, intake: dict, comps: dict | None) -> JudgmentResult:
        """Return a JudgmentResult given intake (canonical_deal.json) and comps."""
        ...


from plat_agent.lifecycle.defaults import JUDGMENT_ENGINE_V1
from plat_agent.lifecycle.judgment_rules import (
    classify_positioning,
    compute_capex_per_unit,
    compute_exit_cap,
    compute_leverage,
    compute_renovation_pace,
    compute_rent_growth,
)


ENGINE_INPUTS_RELATIVE = "judgment/engine_inputs.json"


class DeterministicJudgmentEngine:
    """V1 deterministic rules engine. Implements the JudgmentEngine protocol.

    Composes the per-field rules from `judgment_rules.py` into a JudgmentResult.
    """

    name = JUDGMENT_ENGINE_V1  # "deterministic_v1"

    def evaluate(self, intake: dict, comps: dict | None) -> JudgmentResult:
        comps_unavailable = comps is None

        positioning = classify_positioning(intake, comps)
        positioning_value = positioning.value

        capex_per_unit = compute_capex_per_unit(
            intake, comps, positioning_value=positioning_value,
        )
        renovation_pace = compute_renovation_pace(
            intake, comps, positioning_value=positioning_value,
        )
        rent_growth = compute_rent_growth(intake, comps)
        exit_cap = compute_exit_cap(intake, comps)
        leverage = compute_leverage(intake)

        blockers: list[BlockerItem] = []
        if comps_unavailable:
            blockers.append(BlockerItem(
                step="judgment",
                id="comps_unavailable",
                description=(
                    "Comps step did not produce comps.json; judgment ran with "
                    "OM-only inputs. Recommendation will default to NEEDS_DATA."
                ),
                resolution_hint=(
                    "rerun the comps step (or unblock the scraper) so judgment "
                    "can validate broker rent claims against market evidence."
                ),
                cleared=False,
            ))

        return JudgmentResult(
            positioning=positioning,
            capex_per_unit=capex_per_unit,
            renovation_pace_units_per_month=renovation_pace,
            rent_growth=rent_growth,
            exit_cap=exit_cap,
            leverage=leverage,
            engine_inputs_relative=ENGINE_INPUTS_RELATIVE,
            blockers=blockers,
        )


def _format_pct(x: float | None) -> str:
    if x is None:
        return "n/a"
    return f"{x * 100:.1f}%"


def _format_dollars(x: float | None) -> str:
    if x is None:
        return "n/a"
    return f"${x:,.0f}"


def _broker_disagreements(res: JudgmentResult) -> list[str]:
    """Return human-readable bullet points for any non-green delta_flags."""
    bullets: list[str] = []
    fields = {
        "capex/unit": (res.capex_per_unit, _format_dollars),
        "rent growth": (res.rent_growth, _format_pct),
        "exit cap": (res.exit_cap, _format_pct),
    }
    for label, (field, fmt) in fields.items():
        if field is None or field.delta_flag == "green":
            continue
        data = fmt(field.data_derived)
        om = fmt(field.om_claimed)
        bullets.append(
            f"- **{label}** — broker: {om}, data-derived: {data}, "
            f"flag: `{field.delta_flag}` (selected: {fmt(field.selected)})"
        )
    return bullets


def render_thesis(res: JudgmentResult, *, deal_slug: str, run_id: str) -> str:
    """Render thesis.md narrative (3–4 paragraphs)."""
    title = f"# Investment Thesis — {deal_slug} / {run_id}"

    # Paragraph 1 — positioning + rationale
    pos = res.positioning
    pos_label = "value-add" if pos.value == "value_add" else pos.value.replace("_", " ")
    p1 = (
        f"**Positioning:** {pos_label} (confidence {pos.confidence:.2f}). "
        f"{pos.rationale}"
    )

    # Paragraph 2 — broker-claim validation summary
    disagreements = _broker_disagreements(res)
    if disagreements:
        p2 = (
            "**Broker-claim validation:** judgment found material disagreements "
            "with the OM in the following areas:\n\n" + "\n".join(disagreements) +
            "\n\nThe `selected` value in each case is the GP-conservative pick."
        )
    else:
        p2 = (
            "**Broker-claim validation:** OM assumptions align with data-derived "
            "values; no material disagreements (all delta flags green)."
        )

    # Paragraph 3 — operating plan
    capex = res.capex_per_unit
    pace = res.renovation_pace_units_per_month
    rg = res.rent_growth
    if pos.value == "value_add" and capex is not None and pace is not None:
        p3 = (
            f"**Business plan:** value-add execution at "
            f"{_format_dollars(capex.selected)}/unit capex, pace "
            f"{pace.selected:.0f} units/month. Underwritten rent growth: "
            f"{_format_pct(rg.selected if rg else None)}."
        )
    else:
        p3 = (
            f"**Business plan:** stabilized hold; minimal turn-cost capex "
            f"({_format_dollars(capex.selected if capex else None)}/unit). "
            f"Underwritten rent growth: {_format_pct(rg.selected if rg else None)}."
        )

    # Paragraph 4 — leverage + caveat
    lev = res.leverage
    p4 = (
        f"**Leverage:** {int(lev['ltv'] * 100)}% LTV at {lev['rate'] * 100:.2f}%, "
        f"{lev['amort_years']}yr amort, {lev['io_months']}mo I/O "
        f"(source: `{lev['source']}`). "
    )
    if lev["source"] == "v1_hardcoded":
        p4 += (
            "Note: V1 uses hardcoded leverage placeholder per design §6.A.1; "
            "recommendation_confidence is capped at 0.6 to reflect this."
        )

    paragraphs = [title, p1, p2, p3, p4]

    # Spec §2.3 has no top-level comps_unavailable field; we derive the signal
    # from the blockers list instead.
    comps_unavailable = any(b.id == "comps_unavailable" for b in res.blockers)
    if comps_unavailable:
        paragraphs.append(
            "**Caveat — comps unavailable:** the comps step did not produce a "
            "valid comps.json for this run. Judgment ran on OM-only inputs at "
            "reduced confidence. The orchestrator will set recommendation = "
            "NEEDS_DATA regardless of underwriting metrics."
        )

    return "\n\n".join(paragraphs) + "\n"


import copy
import subprocess
import sys


def build_engine_inputs(intake: dict, result: JudgmentResult) -> dict:
    """Flatten a JudgmentResult onto intake to produce schema-v0.1 engine inputs.

    Strategy: deep-copy intake (so we keep all already-extracted fields like
    metadata, unit_cohorts, opex_table) then overlay the judgment's `selected`
    values for the four assumption categories judgment owns.
    """
    out = copy.deepcopy(intake)
    time_grid = dict(out.get("time_grid", {}) or {})
    analysis_start = time_grid.get("analysis_start_date")
    analysis_end = time_grid.get("analysis_end_date")

    # V1.5.1 live-smoke fix: judgment historically emitted legacy
    # federation-era blocks (`capex_assumptions`, `debt`, and
    # `growth_assumptions.rent_growth`) that the current mfu schema rejects.
    # Normalize the known drift here so underwriting-runner sees the
    # canonical engine contract shape.
    legacy_growth = dict(out.get("growth_assumptions", {}) or {})
    legacy_exit = dict(out.get("exit_assumptions", {}) or {})
    metadata = dict(out.get("metadata", {}) or {})
    out.pop("capex_assumptions", None)
    out.pop("debt", None)
    if metadata:
        out["metadata"] = metadata

    # Rent growth (schema-v0.1 uses annual_growth_rate, not rent_growth).
    growth = legacy_growth
    if result.rent_growth is not None:
        growth["annual_growth_rate"] = result.rent_growth.selected
    elif growth.get("annual_growth_rate") is None and growth.get("rent_growth") is not None:
        growth["annual_growth_rate"] = growth["rent_growth"]
    growth.pop("rent_growth", None)
    if growth:
        growth.setdefault("growth_type", "annual_compound")
        out["growth_assumptions"] = growth

    # Exit assumptions (schema-v0.1 requires exit_month).
    exit_a = legacy_exit
    if result.exit_cap is not None:
        exit_a["exit_cap_rate"] = result.exit_cap.selected
    if exit_a:
        exit_a.setdefault("exit_month", analysis_end)
        out["exit_assumptions"] = exit_a

    # Leverage — schema-v0.1 expects debt_terms, not the legacy debt.loans[].
    # V1.5: when source == agency_dscr_constrained, use the loan_amount sized
    # by mfu's compute_agency_loan_terms() (binding LTV vs DSCR rule). When
    # source == v1_hardcoded, fall back to LTV × purchase_price for commitment.
    purchase_price = (out.get("purchase_assumptions", {}) or {}).get("purchase_price")
    lev = result.leverage
    if lev.get("source") == "agency_dscr_constrained" and lev.get("loan_amount"):
        commitment = float(lev["loan_amount"])
    elif purchase_price is not None:
        commitment = float(purchase_price) * float(lev["ltv"])
    else:
        commitment = 0.0

    debt_terms = dict(out.get("debt_terms", {}) or {})
    debt_terms.update({
        "rate": lev["rate"],
        "amort_years": lev["amort_years"],
        "io_months": lev["io_months"],
        "commitment": commitment,
    })
    if analysis_start:
        debt_terms.setdefault("loan_start_month", analysis_start)
    term_years = lev.get("term_years")
    if term_years is not None:
        debt_terms.setdefault("term_months", int(float(term_years) * 12))
    out["debt_terms"] = debt_terms

    return out


import json
import traceback
from collections import Counter
from pathlib import Path

from plat_agent.dispatch.sibling import SiblingRepo
from plat_agent.lifecycle.atomic import atomic_write_json, atomic_write_text
from plat_agent.lifecycle.cache import compute_input_hash, is_satisfied as _cache_is_satisfied
from plat_agent.lifecycle.cache import write_provenance
from plat_agent.lifecycle.complete_marker import write_complete_marker
from plat_agent.lifecycle.protocol import StepResult
from plat_agent.lifecycle.state import LifecycleState


JUDGMENT_FILE_MANIFEST = [
    "positioning.json",
    "thesis.md",
    "engine_inputs.json",
    "_provenance.json",
]

MFU_SIBLING_NAME = "multifamily-underwriting"
DEFAULT_BENCHMARK_5YR_TREASURY = "0.04"
DEFAULT_HOUSE_STRATEGY = "cashflow"


def _input_paths(run_dir: Path) -> list[Path]:
    """Files that contribute to the judgment step's input_hash."""
    return [
        run_dir / "intake" / "canonical_deal.json",
        run_dir / "comps" / "comps.json",
    ]

def _judgment_file_manifest(step_dir: Path) -> list[str]:
    """Include optional house-pricing boundary evidence when it is present."""
    manifest = list(JUDGMENT_FILE_MANIFEST)
    pricing_summary = "pricing_policy/backsolve_summary.json"
    if (step_dir / pricing_summary).exists():
        manifest.append(pricing_summary)
    return manifest


def _delta_flag_counts(result: JudgmentResult) -> dict[str, int]:
    """Bucket delta SEVERITY into red/yellow/green for provenance summary.

    Counts use ``ValidatedField.delta_severity`` rather than ``delta_flag`` so
    the spec §1 V1 Defaults bands (yellow at ±15%, red at ±25%) are preserved
    regardless of any directional override on the editorial flag. A field
    flagged ``broker_optimistic`` at a 30% delta still counts as RED here.
    """
    severities: list[str] = []
    for f in (result.capex_per_unit, result.renovation_pace_units_per_month,
              result.rent_growth, result.exit_cap):
        if f is None:
            continue
        severities.append(f.delta_severity)
    counter = Counter(severities)
    return {
        "red": counter["red"],
        "yellow": counter["yellow"],
        "green": counter["green"],
    }


def _needs_house_base_case(engine_inputs: dict[str, Any]) -> bool:
    purchase = (engine_inputs.get("purchase_assumptions") or {})
    purchase_price = purchase.get("purchase_price")
    return purchase_price in (None, "", 0, 0.0)


def _solver_benchmark_from_engine_inputs(engine_inputs: dict[str, Any]) -> str:
    property_summary = ((engine_inputs.get("metadata") or {}).get("property_summary") or {})
    debt_guidance = property_summary.get("debt_guidance") or {}
    selected = debt_guidance.get("hold_matched_recommendation") or {}
    benchmark = selected.get("benchmark_rate")
    if benchmark in (None, "", 0, 0.0):
        return DEFAULT_BENCHMARK_5YR_TREASURY
    try:
        return f"{float(benchmark):.4f}"
    except (TypeError, ValueError):
        return DEFAULT_BENCHMARK_5YR_TREASURY


def _solver_year_built_from_engine_inputs(engine_inputs: dict[str, Any]) -> int | None:
    def _normalize_year(value: Any) -> int | None:
        try:
            year = int(value)
        except (TypeError, ValueError):
            return None
        if 1800 <= year <= 2100:
            return year
        return None

    metadata = engine_inputs.get("metadata") or {}
    direct = metadata.get("year_built")
    if direct not in (None, ""):
        normalized = _normalize_year(direct)
        if normalized is not None:
            return normalized
    property_summary = metadata.get("property_summary") or {}
    for candidate in (
        property_summary.get("year_built"),
        (property_summary.get("broker_underwriting_snapshot") or {}).get("year_built"),
    ):
        normalized = _normalize_year(candidate)
        if normalized is not None:
            return normalized
    return None


def _annual_gpr_from_engine_inputs(engine_inputs: dict[str, Any]) -> float | None:
    cohorts = engine_inputs.get("unit_cohorts") or []
    if not cohorts:
        return None
    rent_by_cohort: dict[str, float] = {}
    for row in engine_inputs.get("market_rent_curve") or []:
        cid = row.get("cohort_id")
        rent = row.get("market_rent")
        if cid and rent not in (None, ""):
            try:
                rent_by_cohort[str(cid)] = float(rent)
            except (TypeError, ValueError):
                continue
    total = 0.0
    for cohort in cohorts:
        try:
            units = int(cohort.get("unit_count") or 0)
            rent = float(
                rent_by_cohort.get(
                    str(cohort.get("cohort_id")),
                    cohort.get("target_monthly_rent")
                    or cohort.get("initial_inplace_rent")
                    or 0,
                )
            )
        except (TypeError, ValueError):
            continue
        total += units * rent * 12
    return total if total > 0 else None


def _validate_pricing_seed_before_solver(engine_inputs: dict[str, Any]) -> None:
    """Catch parser-poisoned pricing seeds before running the CoC solver."""
    gpr = _annual_gpr_from_engine_inputs(engine_inputs)
    if not gpr:
        return
    annual_opex = 0.0
    suspicious: list[str] = []
    for row in engine_inputs.get("opex_table") or []:
        try:
            amount = float(row.get("base_value") or 0)
        except (TypeError, ValueError):
            continue
        annual_opex += amount
        category = str(row.get("category_name") or "unknown")
        normalized = category.lower().replace(" ", "_")
        if normalized not in {"real_estate_taxes", "property_taxes", "insurance"} and amount > gpr * 0.40:
            suspicious.append(f"{category}=${amount:,.0f} exceeds 40% of annual GPR")
    if annual_opex > gpr * 0.90:
        suspicious.append(
            f"total opex=${annual_opex:,.0f} exceeds 90% of annual GPR=${gpr:,.0f}"
        )
    if suspicious:
        raise RuntimeError(
            "pricing seed failed opex sanity preflight; likely T12/parser normalization defect: "
            + "; ".join(suspicious)
        )


def _grouped_comps_path_for_run(run_dir: Path) -> Path | None:
    candidate = run_dir / "market_study" / "comps_cohort_grouped.json"
    if candidate.exists():
        return candidate
    return None


def _load_grouped_comps_for_judgment(run_dir: Path) -> dict[str, Any] | None:
    """Synthesize judgment-friendly comps when cohort-level comp evidence exists.

    Some salvage flows produce `market_study/comps_cohort_grouped.json` even when
    `comps/comps.json` remains empty. Judgment only needs a rent surface, so we
    flatten the cohort-level rows into the `comps.json` shape expected by the
    deterministic rules engine.
    """
    path = _grouped_comps_path_for_run(run_dir)
    if path is None:
        return None
    try:
        payload = json.loads(path.read_text())
    except Exception:
        return None
    comps_by_cohort = payload.get("comps_by_cohort") or {}
    synthesized: list[dict[str, Any]] = []
    for cohort_key, rows in comps_by_cohort.items():
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            asking_rent = row.get("asking_rent")
            if asking_rent in (None, "", 0, 0.0):
                continue
            synthesized.append(
                {
                    "name": row.get("property_name") or cohort_key,
                    "distance_miles": row.get("distance_miles"),
                    "year_built": row.get("year_built"),
                    "unit_types": [
                        {
                            "unit_type": cohort_key,
                            "face_rent": asking_rent,
                            "effective_rent": asking_rent,
                            "rent_psf": row.get("rent_per_sqft"),
                            "sqft": row.get("sqft"),
                        }
                    ],
                }
            )
    if not synthesized:
        return None
    return {
        "as_of": payload.get("as_of"),
        "comps": synthesized,
        "source": "cohort_level_comp_evidence_fallback",
        "status": payload.get("status"),
    }


def _maybe_synthesize_house_base_case(
    *,
    state: LifecycleState,
    run_dir: Path,
    engine_inputs: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], Any | None]:
    if not _needs_house_base_case(engine_inputs):
        return engine_inputs, {"house_base_case_applied": False}, None

    repo = SiblingRepo.from_env_or_default(
        MFU_SIBLING_NAME,
        default_relative=MFU_SIBLING_NAME,
    )
    script_path = repo.path / "runs" / "backsolve_price_for_target_coc.py"
    if not script_path.exists():
        raise FileNotFoundError(f"house base-case solver missing at {script_path}")

    step_dir = run_dir / "judgment"
    pricing_dir = step_dir / "pricing_policy"
    pricing_dir.mkdir(parents=True, exist_ok=True)
    seed_path = pricing_dir / "_engine_inputs_seed.json"
    atomic_write_json(seed_path, engine_inputs)
    _validate_pricing_seed_before_solver(engine_inputs)

    metadata = engine_inputs.get("metadata") or {}
    year_built = _solver_year_built_from_engine_inputs(engine_inputs)
    property_summary = metadata.get("property_summary") or {}
    broker_snapshot = property_summary.get("broker_underwriting_snapshot")
    python_bin = repo.path / ".venv" / "bin" / "python"
    command = [
        str(python_bin if python_bin.exists() else Path(sys.executable)),
        str(script_path),
        "--canonical-json",
        str(seed_path),
        "--output-dir",
        str(pricing_dir),
        "--strategy",
        DEFAULT_HOUSE_STRATEGY,
        "--benchmark-5yr-treasury",
        _solver_benchmark_from_engine_inputs(engine_inputs),
    ]
    if year_built is not None:
        command.extend(["--year-built", str(year_built)])
    if isinstance(broker_snapshot, dict) and broker_snapshot:
        broker_snapshot_path = pricing_dir / "broker_underwriting_snapshot.json"
        atomic_write_json(broker_snapshot_path, broker_snapshot)
        command.extend(["--broker-json", str(broker_snapshot_path)])
    grouped_comps_path = _grouped_comps_path_for_run(run_dir)
    if grouped_comps_path is not None:
        command.extend(["--grouped-comps-json", str(grouped_comps_path)])

    completed = subprocess.run(
        command,
        cwd=str(repo.path),
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        stderr = (completed.stderr or "").strip()
        stdout = (completed.stdout or "").strip()
        detail = stderr or stdout or f"exit code {completed.returncode}"
        raise RuntimeError(f"house base-case solver failed: {detail}")

    solved_path = pricing_dir / "canonical_backsolved_target_coc.json"
    summary_path = pricing_dir / "backsolve_summary.json"
    if not solved_path.exists():
        raise FileNotFoundError(
            f"house base-case solver succeeded but did not write {solved_path}"
        )

    solved = json.loads(solved_path.read_text())
    summary = json.loads(summary_path.read_text()) if summary_path.exists() else {}
    blocker = None
    if not solved.get("purchase_assumptions"):
        from plat_agent.lifecycle.state import BlockerItem

        blocker = BlockerItem(
            step="judgment",
            id="house_base_case_unavailable",
            description=(
                "Lifecycle could not synthesize purchase/debt/fund assumptions "
                "from the saved cashflow-first house policy."
            ),
            resolution_hint=(
                "Inspect judgment/pricing_policy outputs, classify the debt "
                "guidance, or provide an analyst patch with economics."
            ),
            cleared=False,
        )
    return solved, {
        "house_base_case_applied": True,
        "house_base_case_summary_relative": summary_path.relative_to(run_dir).as_posix()
        if summary_path.exists()
        else None,
        "house_base_case_purchase_price": (
            (solved.get("purchase_assumptions") or {}).get("purchase_price")
        ),
    }, blocker


def synchronize_judgment_provenance(run_dir: Path) -> dict[str, Any]:
    """Re-stamp provenance after an owner refreshes judgment artifacts in place."""
    step_dir = run_dir / "judgment"
    provenance_path = step_dir / "_provenance.json"
    positioning = json.loads((step_dir / "positioning.json").read_text())
    existing = json.loads(provenance_path.read_text())
    field_names = (
        "capex_per_unit",
        "renovation_pace_units_per_month",
        "rent_growth",
        "exit_cap",
    )
    fields = [
        positioning.get(name)
        for name in field_names
        if isinstance(positioning.get(name), dict)
    ]
    counts = {"red": 0, "yellow": 0, "green": 0}
    for field in fields:
        severity = field.get("delta_severity") or field.get("delta_flag") or "green"
        counts[severity if severity in counts else "green"] += 1
    blockers = list(positioning.get("blockers") or [])
    lifecycle_keys = {
        "status",
        "input_hash",
        "contract_version",
        "written_at",
        "broker_claims_validated",
        "delta_flag_counts",
        "comps_unavailable",
        "blockers",
    }
    preserved = {
        key: value
        for key, value in existing.items()
        if key not in lifecycle_keys
    }
    write_provenance(
        step_dir,
        input_hash=compute_input_hash(_input_paths(run_dir)),
        status="blocked" if blockers else "ok",
        extra={
            **preserved,
            "judgment_engine": existing.get("judgment_engine", "deterministic_v1"),
            "broker_claims_validated": sum(
                field.get("om_claimed") is not None for field in fields
            ),
            "delta_flag_counts": counts,
            "comps_unavailable": any(
                blocker.get("id") == "comps_unavailable"
                for blocker in blockers
            ),
            "blockers": blockers,
        },
    )
    write_complete_marker(
        step_dir,
        step="judgment",
        file_manifest=_judgment_file_manifest(step_dir),
    )
    return json.loads(provenance_path.read_text())


class JudgmentStep:
    """Orchestrator-facing adapter implementing LifecycleStep for V1."""

    name = "judgment"

    def __init__(self, engine: JudgmentEngine | None = None) -> None:
        self.engine: JudgmentEngine = engine or DeterministicJudgmentEngine()

    def run(self, state: LifecycleState, run_dir: Path) -> StepResult:
        step_dir = run_dir / "judgment"
        step_dir.mkdir(parents=True, exist_ok=True)
        try:
            intake_path = run_dir / "intake" / "canonical_deal.json"
            if not intake_path.exists():
                raise FileNotFoundError(
                    f"intake/canonical_deal.json not found under {run_dir}"
                )
            intake = json.loads(intake_path.read_text())

            comps_path = run_dir / "comps" / "comps.json"
            comps = json.loads(comps_path.read_text()) if comps_path.exists() else None
            if not (comps or {}).get("comps"):
                grouped = _load_grouped_comps_for_judgment(run_dir)
                if grouped is not None:
                    comps = grouped

            result = self.engine.evaluate(intake, comps)
            engine_inputs = build_engine_inputs(intake, result)
            engine_inputs, house_case_meta, house_case_blocker = (
                _maybe_synthesize_house_base_case(
                    state=state,
                    run_dir=run_dir,
                    engine_inputs=engine_inputs,
                )
            )
            if house_case_blocker is not None:
                result.blockers.append(house_case_blocker)

            # Write artifacts (each via atomic_write_*)
            atomic_write_json(
                step_dir / "positioning.json", result.model_dump(mode="json"),
            )
            atomic_write_text(
                step_dir / "thesis.md",
                render_thesis(result, deal_slug=state.deal_slug, run_id=state.run_id),
            )
            atomic_write_json(
                step_dir / "engine_inputs.json", engine_inputs,
            )

            input_hash = compute_input_hash(_input_paths(run_dir))
            counts = _delta_flag_counts(result)
            comps_unavailable = any(b.id == "comps_unavailable" for b in result.blockers)
            write_provenance(
                step_dir,
                input_hash=input_hash,
                status="blocked" if result.blockers else "ok",
                extra={
                    "judgment_engine": self.engine.name,
                    "broker_claims_validated": sum(
                        1 for f in (result.capex_per_unit, result.rent_growth, result.exit_cap)
                        if f is not None and f.om_claimed is not None
                    ),
                    "delta_flag_counts": counts,
                    "comps_unavailable": comps_unavailable,
                    **house_case_meta,
                    # Per spec §4.1 audit trail: persist actual blockers, not just counts.
                    "blockers": [b.model_dump(mode="json") for b in result.blockers],
                },
            )

            # _complete marker — LAST action
            write_complete_marker(
                step_dir,
                step="judgment",
                file_manifest=_judgment_file_manifest(step_dir),
            )

            status = "blocked" if result.blockers else "ok"
            return StepResult(status=status, blockers=list(result.blockers))

        except Exception as e:
            # Hard error — write _provenance with status=error so downstream can detect
            try:
                write_provenance(
                    step_dir,
                    input_hash="",
                    status="error",
                    extra={
                        "judgment_engine": self.engine.name,
                        "error_message": str(e),
                        "error_traceback": traceback.format_exc(),
                    },
                )
            except Exception:
                pass  # never let provenance writes mask the original error
            return StepResult(
                status="error",
                error_message=str(e),
                error_traceback=traceback.format_exc(),
            )

    def is_satisfied(self, state: LifecycleState, run_dir: Path) -> bool:
        """Delegate to lifecycle.cache.is_satisfied with judgment's input_hash."""
        current_hash = compute_input_hash(_input_paths(run_dir))
        return _cache_is_satisfied(
            state, run_dir, step="judgment", current_input_hash=current_hash,
        )
