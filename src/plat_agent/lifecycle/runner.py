"""Lifecycle orchestrator (spec §1 + §3).

Composes IntakeStep + CompsStep + JudgmentStep + UnderwritingStep + MemoStep
+ CRMStep into a single sync pipeline. Owns run_id allocation, raw_inputs
snapshotting, dependency-map application, recommendation patch (Step 4.5),
and the in-memory final-state hand-off to the CRM step.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from datetime import datetime, timezone

from plat_agent.lifecycle.atomic import atomic_write_json
from plat_agent.lifecycle.cache import compute_input_hash
from plat_agent.lifecycle.defaults import CONTRACT_VERSION
from plat_agent.lifecycle.dependency_map import decide_after_step
from plat_agent.lifecycle.protocol import LifecycleStep, StepResult
from plat_agent.lifecycle.punchlist import (
    read_punchlist_json,
    reconcile_punchlist,
    write_punchlist_json,
    write_punchlist_markdown,
)
from plat_agent.lifecycle.recommendation_patch import patch_recommendation
from plat_agent.lifecycle.property_tax import (
    PropertyTaxGateResult,
    is_property_tax_blocker,
    resolve_property_tax_gate,
)
from plat_agent.lifecycle.state import LifecycleState, LifecycleStatus, StepName

_RUN_ID_RE = re.compile(r"^run_(\d{3,})$")
LIFECYCLE_STATE_FILE = "_lifecycle_state.json"

# Step ordering is fixed per spec §3
PIPELINE_STEPS: tuple[StepName, ...] = (
    "intake", "comps", "judgment", "underwriting", "memo", "crm",
)


def _replace_step_blockers(
    state: LifecycleState,
    *,
    step_name: StepName,
    new_blockers,
) -> None:
    """Replace a step's blockers instead of appending forever on reruns.

    Prior behavior appended blockers from every rerun into `_lifecycle_state.json`,
    so a single sticky intake/comps issue could appear dozens of times on a
    long-lived run. The lifecycle state should reflect the CURRENT blockers
    for each step, not the historical accumulation of every prior attempt.
    """
    retained = [blocker for blocker in state.blockers if blocker.step != step_name]
    deduped: list = []
    seen_ids: set[str] = set()
    for blocker in new_blockers or []:
        if blocker.id in seen_ids:
            continue
        deduped.append(blocker)
        seen_ids.add(blocker.id)
    state.blockers = retained + deduped


def _normalize_state_blockers(state: LifecycleState) -> None:
    """Deduplicate any historical blocker accumulation already on disk."""
    deduped: list = []
    seen: set[tuple[str, str]] = set()
    for blocker in state.blockers:
        key = (blocker.step, blocker.id)
        if key in seen:
            continue
        deduped.append(blocker)
        seen.add(key)
    state.blockers = deduped


@dataclass
class LifecycleResult:
    """Outcome of a single run_lifecycle() invocation."""

    deal_slug: str
    run_id: str
    status: LifecycleStatus
    memo_path: Path | None = None
    deal_summary_path: Path | None = None
    crm_row_appended: bool = False
    blocker_count: int = 0
    exit_code: int = 0


def deal_outputs_dir(project_root: Path, deal_slug: str) -> Path:
    """Return the runs/deals/<slug>/outputs/ directory (per mfu convention)."""
    return project_root / "runs" / "deals" / deal_slug / "outputs"


def allocate_run_id(outputs_dir: Path) -> str:
    """Allocate next run_NNN by scanning existing run_* subdirs.

    Returns "run_001" if no prior runs exist; otherwise "run_<max+1>".
    Skips any directory whose name doesn't match `run_\\d{3,}`.
    """
    if not outputs_dir.exists():
        return "run_001"
    nums = []
    for entry in outputs_dir.iterdir():
        if not entry.is_dir():
            continue
        m = _RUN_ID_RE.match(entry.name)
        if m:
            nums.append(int(m.group(1)))
    nxt = (max(nums) + 1) if nums else 1
    return f"run_{nxt:03d}"


def snapshot_raw_inputs(data_room: Path, run_dir: Path) -> None:
    """Snapshot data room into <run_dir>/raw_inputs/ (run-scoped per §3).

    Uses os.link (hardlink) for cheap snapshotting on the same filesystem;
    falls back to shutil.copy2 across filesystems. Idempotent — files that
    already exist at the destination are skipped (handy for --resume).

    Raises FileNotFoundError if data_room doesn't exist; NotADirectoryError
    if it's a file. The orchestrator catches and reports as hard error.
    """
    data_room = Path(data_room)
    if not data_room.exists():
        raise FileNotFoundError(f"data room not found: {data_room}")
    if not data_room.is_dir():
        raise NotADirectoryError(f"data room must be a directory: {data_room}")

    dest = run_dir / "raw_inputs"
    dest.mkdir(parents=True, exist_ok=True)

    for src_path in data_room.rglob("*"):
        if src_path.is_dir():
            continue
        rel = src_path.relative_to(data_room)
        if rel.parts and rel.parts[0] in {"outputs", "standardized"}:
            continue
        if len(rel.parts) == 1 and rel.name in {"deal_manifest.md", "assumptions.md", "intake_punchlist.md"}:
            continue
        dst_path = dest / rel
        if dst_path.exists():
            continue
        dst_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.link(src_path, dst_path)
        except OSError:
            # Cross-filesystem or unsupported — fall back to copy
            shutil.copy2(src_path, dst_path)


def persist_state(run_dir: Path, state: LifecycleState) -> None:
    """Atomically write the lifecycle state to run_dir."""
    atomic_write_json(run_dir / LIFECYCLE_STATE_FILE, state.model_dump(mode="json"))


def write_initial_state(run_dir: Path, *, deal_slug: str, run_id: str) -> LifecycleState:
    """Create the initial _lifecycle_state.json with status=running."""
    state = LifecycleState(deal_slug=deal_slug, run_id=run_id)
    persist_state(run_dir, state)
    return state


def read_state(run_dir: Path) -> LifecycleState:
    """Read existing _lifecycle_state.json (used by --resume)."""
    import json
    payload = json.loads((run_dir / LIFECYCLE_STATE_FILE).read_text())
    state = LifecycleState.model_validate(payload)
    _normalize_state_blockers(state)
    return state


def _default_steps(project_root: Path) -> dict[StepName, LifecycleStep]:
    """Build the default step adapters from plans 02-06 + this plan's UnderwritingStep.

    Importing here keeps lifecycle.runner importable even when sibling
    plans are not yet complete (tests can pass `steps=` explicitly).

    `project_root` is required because CRMStep needs the runs/deals/ root to
    locate the slug-level _crm.jsonl ledger.
    """
    from plat_agent.lifecycle.steps.intake import IntakeStep        # plan-02
    from plat_agent.lifecycle.steps.comps import CompsStep          # plan-03
    from plat_agent.lifecycle.judgment import JudgmentStep          # plan-04
    from plat_agent.lifecycle.memo import MemoStep                  # plan-05
    from plat_agent.lifecycle.crm import CRMStep                    # plan-06
    from plat_agent.lifecycle.steps.underwriting import UnderwritingStep
    return {
        "intake": IntakeStep(),
        "comps": CompsStep(),
        "judgment": JudgmentStep(),
        "underwriting": UnderwritingStep(),
        "memo": MemoStep(),
        "crm": CRMStep(runs_deals_root=project_root / "runs" / "deals"),
    }


def _derive_deal_slug(data_room: Path) -> str:
    """Snake-case the data room directory name."""
    raw = data_room.resolve().name.lower()
    slug = re.sub(r"[^a-z0-9]+", "_", raw).strip("_")
    return re.sub(r"_(data_room|dataroom)$", "", slug)


def _canonical_with_metadata_overrides(
    canonical: dict,
    *,
    address: str | None,
    market: str | None,
) -> dict:
    if not address and not market:
        return canonical
    patched = json.loads(json.dumps(canonical))
    metadata = patched.setdefault("metadata", {})
    if address:
        metadata["address"] = address
    if market:
        metadata["market"] = market
    return patched


def _without_property_tax_policy(canonical: dict) -> dict:
    comparable = json.loads(json.dumps(canonical))
    metadata = comparable.get("metadata")
    if not isinstance(metadata, dict):
        return comparable
    property_summary = metadata.get("property_summary")
    if isinstance(property_summary, dict):
        property_summary.pop("property_tax_policy", None)
    return comparable


def _single_file_hash(content: bytes) -> str:
    digest = hashlib.sha256()
    digest.update(content)
    digest.update(b"|")
    return digest.hexdigest()


def _resolve_and_persist_property_tax_gate(
    *,
    run_dir: Path,
    state: LifecycleState,
    data_room: Path,
    millage_rate: str | None,
    address: str | None = None,
    market: str | None = None,
    canonical_before_gate: dict | None = None,
    canonical_bytes_before_gate: bytes | None = None,
) -> PropertyTaxGateResult | None:
    canonical_path = run_dir / "intake" / "canonical_deal.json"
    if not canonical_path.exists():
        return None
    canonical = json.loads(canonical_path.read_text())
    staged_canonical = _canonical_with_metadata_overrides(
        canonical,
        address=address,
        market=market,
    )
    gate = resolve_property_tax_gate(
        staged_canonical,
        millage_rate=millage_rate,
        source_locator=(
            "plat lifecycle:--millage-rate"
            if millage_rate is not None
            else None
        ),
        data_room=data_room,
        deal_slug=state.deal_slug,
        run_id=state.run_id,
    )
    canonical_changed = gate.canonical != canonical
    if gate.blocker is None and canonical_changed:
        atomic_write_json(canonical_path, gate.canonical)
    if gate.changed:
        tax_policy_is_only_delta = (
            canonical_before_gate is not None
            and canonical_bytes_before_gate is not None
            and _without_property_tax_policy(canonical_before_gate)
            == _without_property_tax_policy(gate.canonical)
        )
        if tax_policy_is_only_delta and "comps" in state.steps_completed:
            comps_provenance_path = run_dir / "comps" / "_provenance.json"
            if comps_provenance_path.exists():
                try:
                    comps_provenance = json.loads(
                        comps_provenance_path.read_text()
                    )
                except json.JSONDecodeError:
                    comps_provenance = None
                prior_input_hash = _single_file_hash(
                    canonical_bytes_before_gate
                )
                if (
                    isinstance(comps_provenance, dict)
                    and comps_provenance.get("status") == "ok"
                    and comps_provenance.get("contract_version")
                    == CONTRACT_VERSION
                    and comps_provenance.get("input_hash")
                    == prior_input_hash
                ):
                    comps_provenance["input_hash"] = compute_input_hash(
                        [canonical_path]
                    )
                    atomic_write_json(
                        comps_provenance_path,
                        comps_provenance,
                    )
        judgment_idx = PIPELINE_STEPS.index("judgment")
        state.steps_completed = [
            step for step in state.steps_completed
            if PIPELINE_STEPS.index(step) < judgment_idx
        ]

    state.blockers = [
        blocker for blocker in state.blockers
        if not is_property_tax_blocker(blocker)
    ]
    punchlist_blockers = [
        blocker for blocker in read_punchlist_json(run_dir)
        if not is_property_tax_blocker(blocker)
    ]
    if gate.blocker is not None:
        state.blockers.append(gate.blocker)
        punchlist_blockers.append(gate.blocker)
    write_punchlist_json(run_dir, punchlist_blockers)
    write_punchlist_markdown(
        run_dir,
        state.deal_slug,
        state.run_id,
        punchlist_blockers,
    )
    return gate


def _blocked_property_tax_result(
    *,
    run_dir: Path,
    state: LifecycleState,
) -> LifecycleResult:
    state.status = "needs_analyst_input"
    state.finished_at = datetime.now(timezone.utc)
    persist_state(run_dir, state)
    return LifecycleResult(
        deal_slug=state.deal_slug,
        run_id=state.run_id,
        status=state.status,
        blocker_count=len(state.blockers),
        exit_code=2,
    )


def run_lifecycle(
    data_room: Path,
    *,
    deal_slug: str | None = None,
    resume_run_id: str | None = None,
    rerun_from: StepName | None = None,
    judgment_mode: str = "deterministic_v1",
    project_root: Path | None = None,
    steps: dict[StepName, LifecycleStep] | None = None,
    address: str | None = None,
    market: str | None = None,
    millage_rate: str | None = None,
) -> LifecycleResult:
    """Run the full deal lifecycle pipeline (spec §1 + §3).

    Args:
      data_room: directory containing the dropped data room.
      deal_slug: override slug derivation (default: snake-case of dir name).
      resume_run_id: re-enter an existing run; skip cached steps.
      rerun_from: with --resume, force-rerun this step + downstream.
      judgment_mode: "deterministic_v1" (default) or "passthrough" (test-only).
      project_root: where runs/deals/<slug>/ lives (default: cwd).
      steps: dependency injection for tests; default is _default_steps().
      address: V1.3 — analyst-supplied subject address; patched into
        canonical_deal.json metadata.address after intake completes.
      market: V1.3 — analyst-supplied market slug; patched into
        canonical_deal.json metadata.market after intake completes.
      millage_rate: raw combined mills supplied noninteractively by the analyst.
    """
    project_root = project_root or Path.cwd()
    deal_slug = deal_slug or _derive_deal_slug(data_room)
    outputs = deal_outputs_dir(project_root, deal_slug)
    steps = steps or _default_steps(project_root)

    # Pre-step: allocate run_id, snapshot raw_inputs, write initial state
    if resume_run_id:
        run_id = resume_run_id
        run_dir = outputs / run_id
        state = read_state(run_dir)
        # Historical owner refreshes may have appended the same completed
        # step more than once. Normalize on resume so state remains a stable,
        # ordered lifecycle ledger.
        state.steps_completed = list(dict.fromkeys(state.steps_completed))
        # Carry forward the orchestrator's current judgment_mode onto the
        # resumed state so any re-run steps see the right value.
        state.judgment_mode = judgment_mode
        # Reconcile any analyst markdown edits before is_satisfied checks
        try:
            reconcile_punchlist(run_dir)
        except Exception:
            # Reconciliation errors should not crash the resume flow
            pass
        # --rerun-from: trim steps_completed so target + downstream re-run
        if rerun_from:
            target_idx = PIPELINE_STEPS.index(rerun_from)
            state.steps_completed = [
                s for s in state.steps_completed
                if PIPELINE_STEPS.index(s) < target_idx
            ]
        persist_state(run_dir, state)
    else:
        run_id = allocate_run_id(outputs)
        run_dir = outputs / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        snapshot_raw_inputs(data_room, run_dir)
        state = write_initial_state(run_dir, deal_slug=deal_slug, run_id=run_id)
        # Persist judgment_mode immediately so MemoStep + CRMStep see it
        # via state (rather than re-deriving from provenance). Spec §5.4.
        state.judgment_mode = judgment_mode
        persist_state(run_dir, state)

    # V1.5.1 — on resume paths the canonical may already exist from a cached
    # intake step. Patch analyst-supplied metadata overrides before any cached
    # downstream step decides it is satisfied.
    canonical_before_gate: dict | None = None
    canonical_bytes_before_gate: bytes | None = None
    if resume_run_id:
        canonical_path = run_dir / "intake" / "canonical_deal.json"
        if canonical_path.exists():
            canonical_bytes_before_gate = canonical_path.read_bytes()
            canonical_before_gate = json.loads(
                canonical_bytes_before_gate
            )
    # Tax-only canonical changes may rebase valid comps provenance, but every
    # step still performs its normal cache integrity and TTL check.
    if resume_run_id:
        gate = _resolve_and_persist_property_tax_gate(
            run_dir=run_dir,
            state=state,
            data_room=data_room,
            millage_rate=millage_rate,
            canonical_before_gate=canonical_before_gate,
            canonical_bytes_before_gate=canonical_bytes_before_gate,
            address=address,
            market=market,
        )
        if gate is not None:
            if gate.blocker is not None:
                return _blocked_property_tax_result(
                    run_dir=run_dir,
                    state=state,
                )
            state.status = "running"
            state.finished_at = None
            persist_state(run_dir, state)

    # Pipeline state
    terminal_status = None
    terminal_status_pending = None  # set if no later step recovers
    skip_underwriting = False
    comps_unavailable = False
    exit_code = 0
    blocker_count = len(state.blockers)

    for step_name in PIPELINE_STEPS:
        # Skip downstream-of-judgment when judgment hard-errored
        if step_name == "underwriting" and skip_underwriting:
            continue

        # Resume: an explicit --rerun-from establishes a hard execution
        # boundary. Upstream artifacts are analyst-owned inputs to that
        # refresh and must never be re-executed merely because their cache
        # hashes changed.
        if resume_run_id:
            force_rerun = False
            if rerun_from:
                target_idx = PIPELINE_STEPS.index(rerun_from)
                step_idx = PIPELINE_STEPS.index(step_name)
                if step_idx < target_idx:
                    continue
                force_rerun = True
            if not force_rerun and steps[step_name].is_satisfied(state, run_dir):
                # Cached — skip; treat as ok for dependency-map decisions.
                # Do NOT re-append to steps_completed (already there from prior run).
                continue

        # Special-case Step 4.5 between underwriting and memo.
        # Guard: if judgment hard-errored, positioning.json may be missing.
        # Per spec §4.3 the memo still runs (in draft mode) on judgment hard
        # error — so we skip the patch silently when no positioning exists.
        if step_name == "memo":
            # Owner agents may refresh economic artifacts in place after their
            # lifecycle steps complete. Re-stamp provenance from those saved
            # artifacts without re-executing judgment or underwriting.
            if (run_dir / "judgment" / "_provenance.json").exists():
                from plat_agent.lifecycle.judgment import (
                    synchronize_judgment_provenance,
                )

                synchronize_judgment_provenance(run_dir)
            if (run_dir / "underwriting" / "_provenance.json").exists():
                from plat_agent.lifecycle.steps.underwriting import (
                    synchronize_underwriting_provenance,
                )

                synchronize_underwriting_provenance(run_dir)
            positioning_path = run_dir / "judgment" / "positioning.json"
            if positioning_path.exists():
                patch_recommendation(
                    run_dir,
                    comps_unavailable=comps_unavailable,
                    judgment_mode=judgment_mode,
                    blocker_count=blocker_count,
                )
                state.recommendation_patched_at = datetime.now(timezone.utc)
                persist_state(run_dir, state)

        if step_name == "crm":
            # §3 Step 6: compute final state values in-memory before handing to CRM
            state.status = (
                terminal_status or terminal_status_pending or "memo_ready"
            )
            state.finished_at = datetime.now(timezone.utc)
            # CRM consumes this state via the `state` arg; does NOT read the file
            persist_state(run_dir, state)

        step = steps[step_name]
        result = step.run(state, run_dir)

        # Track blockers in state as the current truth for this step.
        _replace_step_blockers(state, step_name=step_name, new_blockers=result.blockers)
        blocker_count = len(state.blockers)
        if result.status == "ok":
            state.steps_completed.append(step_name)
        persist_state(run_dir, state)

        if step_name == "intake":
            gate = _resolve_and_persist_property_tax_gate(
                run_dir=run_dir,
                state=state,
                data_room=data_room,
                millage_rate=millage_rate,
                address=address,
                market=market,
            )
            if gate is not None:
                blocker_count = len(state.blockers)
                if gate.blocker is not None:
                    return _blocked_property_tax_result(
                        run_dir=run_dir,
                        state=state,
                    )
                persist_state(run_dir, state)

        # Apply §4.3 dependency map
        decision = decide_after_step(step_name, result)
        if decision.comps_unavailable:
            comps_unavailable = True
        if decision.skip_underwriting:
            skip_underwriting = True
        if decision.terminal_status:
            terminal_status = decision.terminal_status
            exit_code = 1
            if decision.halt:
                break
        if decision.terminal_status_if_no_recovery:
            terminal_status_pending = decision.terminal_status_if_no_recovery
        if decision.exit_code_override is not None:
            exit_code = decision.exit_code_override
        if decision.halt:
            break

    # Finalize status
    if terminal_status:
        state.status = terminal_status
    elif terminal_status_pending:
        state.status = terminal_status_pending
    elif blocker_count == 0:
        state.status = "memo_ready"
    else:
        state.status = "memo_ready"
    state.finished_at = datetime.now(timezone.utc)
    persist_state(run_dir, state)

    return LifecycleResult(
        deal_slug=deal_slug,
        run_id=run_id,
        status=state.status,
        memo_path=run_dir / "memo" / "memo.md" if (run_dir / "memo" / "memo.md").exists() else None,
        deal_summary_path=run_dir / "underwriting" / "deal_summary.json"
            if (run_dir / "underwriting" / "deal_summary.json").exists() else None,
        crm_row_appended="crm" in state.steps_completed,
        blocker_count=blocker_count,
        exit_code=exit_code,
    )
