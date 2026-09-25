"""Headless dispatch of sibling-repo agents via `claude -p`.

The orchestrator hands the sibling a BridgeRequestV1 JSON document as the
prompt body. The sibling agent body (defined in
<repo>/.claude/agents/<name>.md, YAML frontmatter stripped) is injected into
the receiving Claude session via `--append-system-prompt-file`. This is the
only mechanism that provably wires the receiving session to behave as the
named agent; the `--agent <name>` flag is unreliable (it accepts arbitrary
strings without validating against the project's agent definitions, so the
session may silently run with no agent system prompt at all).

The agent body is materialized to a NamedTemporaryFile under `log_dir` for
the duration of the subprocess. On a successful BridgeResponseV1 parse the
tmpfile is unlinked; on any failure path it is preserved alongside the
stderr log so the operator has the full reproducible context.

Diagnostics: stderr is streamed to a per-dispatch log file under `log_dir`
(default: tempfile.gettempdir()) so that a timeout or crash leaves behind
actionable output. The log path is also printed to the orchestrator's own
stderr on every dispatch.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Type

import pydantic
from pydantic import BaseModel, ValidationError

from plat_agent.contracts.envelope import (
    BridgeError,
    BridgeRequestV1,
    BridgeResponseV1,
)


DEFAULT_TIMEOUT_SECONDS = 1800  # 30 min — deal-intake real-world OM parsing exceeded 10 min on Willow Court
COMP_FINDER_TIMEOUT_SECONDS = 300  # 5 min — steady-state comp refreshes should salvage quickly
COMP_FINDER_BOOTSTRAP_TIMEOUT_SECONDS = 900  # 15 min — first-time market-study onboarding can legitimately take 5-15 min


@dataclass(frozen=True)
class SiblingRepo:
    """Where a sibling repo lives on disk and what its dispatch entrypoint is."""

    name: str
    path: Path

    @classmethod
    def from_env_or_default(cls, name: str, default_relative: str) -> "SiblingRepo":
        """Resolve a sibling repo path from an env var or a relative default.

        Env var convention: `PLAT_<NAME>_PATH` (uppercase, hyphens to
        underscores) — e.g. `PLAT_MARKET_STUDY_AGENT_PATH`.
        """
        env_key = f"PLAT_{name.upper().replace('-', '_')}_PATH"
        raw = os.environ.get(env_key)
        if raw:
            return cls(name=name, path=Path(raw).expanduser().resolve())
        # Default: ../{repo_name} relative to plat-agent's own location.
        plat_agent_root = Path(__file__).resolve().parents[3]
        return cls(name=name, path=(plat_agent_root.parent / default_relative).resolve())


class DispatchError(RuntimeError):
    """Raised when a sibling agent dispatch fails before producing a parseable response.

    `log_path` (when set) points to the on-disk stderr log captured during
    the dispatch — surface this to the caller so they can grep for the
    underlying failure.
    """

    def __init__(self, message: str, *, log_path: Optional[Path] = None) -> None:
        super().__init__(message)
        self.log_path = log_path


def _try_early_persisted_response(
    *,
    repo: SiblingRepo,
    request: BridgeRequestV1,
    dispatch_started_ts: float,
    log_path: Path,
    payload_model: Optional[Type[BaseModel]],
    allow_rebuild: bool = True,
) -> BridgeResponseV1 | None:
    persisted = _load_persisted_response_envelope(
        request,
        dispatch_started_ts=dispatch_started_ts,
    )
    if persisted is None and allow_rebuild:
        persisted = _rebuild_comp_finder_envelope_from_artifacts(
            repo,
            request,
            log_path=log_path,
        )
    if persisted is None:
        return None
    return _finalize_response(
        parsed=persisted,
        request=request,
        payload_model=payload_model,
    )


def _strip_yaml_frontmatter(text: str) -> str:
    """Strip a leading YAML frontmatter block (between two `---` delimiters).

    If the file does not begin with `---` on its own line, returns `text`
    unchanged. Only strips a single leading block; trailing `---` is not
    treated specially.
    """
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].rstrip("\r\n") != "---":
        return text
    for i in range(1, len(lines)):
        if lines[i].rstrip("\r\n") == "---":
            # Drop the frontmatter and the closing delimiter; keep the rest.
            return "".join(lines[i + 1 :]).lstrip("\n")
    # No closing delimiter found — leave file untouched rather than nuke it.
    return text


def _request_payload_dict(request: BridgeRequestV1) -> dict | None:
    payload = request.payload
    if isinstance(payload, dict):
        return payload
    if isinstance(payload, BaseModel):
        return payload.model_dump(mode="json")
    return None


def _effective_timeout_seconds_for_request(
    request: BridgeRequestV1,
    requested_timeout_seconds: int,
) -> int:
    """Return the dispatch timeout after applying agent-specific caps."""
    if request.agent_name != "comp-finder":
        return requested_timeout_seconds

    payload = _request_payload_dict(request) or {}
    bootstrap_plan = payload.get("bootstrap_plan")
    workflow_stage = (
        bootstrap_plan.get("workflow_stage")
        if isinstance(bootstrap_plan, dict)
        else None
    )
    if workflow_stage in {"new_metro", "new_property", "full_onboarding"}:
        return min(requested_timeout_seconds, COMP_FINDER_BOOTSTRAP_TIMEOUT_SECONDS)
    return min(requested_timeout_seconds, COMP_FINDER_TIMEOUT_SECONDS)


def _build_log_path(log_dir: Path, deal_slug: str, agent_name: str) -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    safe_slug = deal_slug.replace("/", "_")
    safe_agent = agent_name.replace("/", "_")
    return log_dir / f"plat-dispatch-{safe_slug}-{safe_agent}-{timestamp}.log"


def _validate_artifact_paths(
    response: BridgeResponseV1,
    deal_root: str,
    run_id: str,
    request_agent_name: str,
) -> None:
    """Assert every ArtifactRef.relative_path resolves under deal_root,
    AND for non-intake agents, under outputs/<run_id>/ specifically.

    The deal-intake.md sibling contract explicitly emits deal-root artifacts
    (`deal_manifest.md`, `intake_punchlist.md`) — those are documented
    convention, not security violations. For all other agents the tighter
    `outputs/<run_id>/` scope still applies.

    Raises ValueError if any artifact path escapes deal_root (via `..`
    traversal) or, for non-intake agents, escapes outputs/<run_id>/.
    """
    deal_root_resolved = Path(deal_root).resolve()
    expected_prefix = (deal_root_resolved / "outputs" / run_id).resolve()
    # The deal-intake contract documents deal-root artifacts (deal_manifest.md,
    # intake_punchlist.md). All other agents are still scoped to outputs/<run>/.
    relax_to_deal_root = request_agent_name == "deal-intake"
    for artifact in response.artifacts:
        resolved = (deal_root_resolved / artifact.relative_path).resolve()
        # Hard floor: never allow escaping deal_root entirely.
        try:
            resolved.relative_to(deal_root_resolved)
        except ValueError:
            raise ValueError(
                f"ArtifactRef.relative_path={artifact.relative_path!r} "
                f"escapes deal_root scope"
            )
        if relax_to_deal_root:
            continue
        try:
            resolved.relative_to(expected_prefix)
        except ValueError:
            raise ValueError(
                f"ArtifactRef.relative_path={artifact.relative_path!r} "
                f"escapes outputs/{run_id}/ scope"
            )


def _check_artifact_existence(
    response: BridgeResponseV1,
    deal_root: str,
) -> None:
    """Assert each declared artifact exists on disk after dispatch.

    Raises ValueError if any declared file is missing — this catches siblings
    that lie about what they wrote, or whose Write tool call silently failed.
    """
    for artifact in response.artifacts:
        full_path = (Path(deal_root) / artifact.relative_path).resolve()
        if not full_path.exists():
            raise ValueError(
                f"ArtifactRef.relative_path={artifact.relative_path!r} "
                f"declared but file not found on disk at {full_path}"
            )


def atomic_write_json(target: Path, data: dict | list, *, indent: int = 2) -> None:
    """Public helper for orchestrators: atomically write `data` as JSON to `target`.

    Use this when an orchestrator agent emits `_provenance.json` (or any
    other sidecar) per the bridging contract §11. The temp-then-rename
    pattern guarantees a reader sees either the prior version or the new
    one — never a half-written file.

    Stage 3 HIGH-4 (audit 2026-04-26): sibling-written `_provenance.json`
    files were not atomic. This helper centralizes the pattern so every
    orchestrator that emits provenance uses the same idempotent write.
    """
    _atomic_write_text(Path(target), json.dumps(data, indent=indent, default=str))


def _atomic_write_text(target: Path, content: str) -> None:
    """Write `content` to `target` via a temp-then-rename pattern.

    Used by the dispatcher to persist `_response_envelope.json` (and the
    helper is exposed for orchestrator agents that emit `_provenance.json`).

    Stage 3 HIGH-4 (audit 2026-04-26): sibling-written `_provenance.json`
    files were not atomic — a mid-write crash left half-formed JSON. Going
    forward, both the dispatcher and orchestrator code paths use this
    helper so any reader sees either the prior version or the new one,
    never a partial document.

    Implementation notes:
      * The temp file lives in the SAME directory as `target` so
        `Path.replace` is an atomic same-filesystem rename (POSIX
        guarantees this; Windows on Python 3.3+ also does).
      * A unique `.tmp.<pid>` suffix avoids collisions when two writers
        happen to land on the same path concurrently — each one renames
        its own temp file last-one-wins, but neither sees the other's
        partial.
      * On any exception the temp file is best-effort cleaned up so we
        don't leave litter behind.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    # Unique suffix per process — protects against two parallel writers
    # racing on the same path within a single dispatch session.
    tmp = target.with_suffix(target.suffix + f".tmp.{os.getpid()}")
    try:
        tmp.write_text(content, encoding="utf-8")
        tmp.replace(target)
    except Exception:
        # Best-effort cleanup of the temp file; don't mask the original error.
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        raise


def _infer_domain_from_response(response: BridgeResponseV1, run_id: str) -> Optional[str]:
    """Pull the domain segment out of the first artifact's relative_path.

    Per `bridging_contract.md` §5, all artifacts for a single dispatch land
    under `outputs/<run_id>/<domain>/`. The domain is the segment immediately
    after `<run_id>/`. Returns None if no artifacts are present or no path
    matches the convention — caller falls back to agent_name mapping.
    """
    for artifact in response.artifacts:
        parts = Path(artifact.relative_path).parts
        # Looking for ('outputs', run_id, <domain>, ...) — also accept paths
        # that omit the leading 'outputs/' prefix since some siblings emit
        # paths relative to <deal_root>/outputs/.
        try:
            if "outputs" in parts:
                idx = parts.index("outputs")
                if idx + 2 < len(parts) and parts[idx + 1] == run_id:
                    return parts[idx + 2]
            elif parts and parts[0] == run_id and len(parts) >= 2:
                return parts[1]
        except (ValueError, IndexError):
            continue
    return None


# Stable mapping from sibling agent_name -> outputs/<run_id>/<domain>/ segment.
# Mirrors `bridging_contract.md` §5 artifact tree. Used as a fallback when
# the response carries no artifacts (e.g. status='error' with no on-disk
# artifacts produced) so we can still persist the envelope under a
# predictable path for the memo composer / audit.
_AGENT_TO_DOMAIN: dict[str, str] = {
    "deal-intake": "intake",
    "cost-bridge-analyst": "costmodel",
    "comp-finder": "market_study",
    "comp-reconciler": "market_study",
    "demographics-analyst": "market_study",
    "submarket-scorer": "submarket",
    "supply-pipeline-analyst": "supply_demand",
    "underwriting-runner": "underwriting",
}


def _persist_response_envelope(
    response: BridgeResponseV1,
    deal_root: str,
    run_id: str,
    domain: Optional[str] = None,
) -> Optional[Path]:
    """Atomic write of the full BridgeResponseV1 envelope JSON for downstream consumers.

    Per Stage 6 M-5 (audit 2026-04-26): the deterministic verdict / sanity_flags
    computed by sibling agents lived only on stdout — the dispatcher captured
    stdout to its own log dir, NOT to the deal artifact tree. Memo composers
    walking `outputs/<run_id>/` therefore had no machine-readable signal and
    fell back to either reading sibling-shaped `_provenance.json` (non-uniform
    per stage_6 M-5) or hallucinating bands from training-data memory.

    Persisting the full envelope as `_response_envelope.json` next to the
    sibling's own artifacts gives downstream consumers a stable, schema-typed
    feed (BridgeResponseV1) regardless of how the sibling shapes its private
    `_provenance.json`.

    Domain resolution order:
      1. Explicit `domain` argument (caller knows best).
      2. First-artifact path inference (per §5 convention).
      3. `_AGENT_TO_DOMAIN` mapping by `agent_name`.
    If none resolve, persistence is skipped (with a warning) — there's no
    safe default subdirectory under `outputs/<run_id>/` to invent.

    Returns the path written on success, or None on (logged, non-fatal) failure.
    Failure to persist MUST NOT fail the dispatch — the response is the source
    of truth for the caller; the on-disk envelope is a best-effort artifact.
    """
    resolved_domain = (
        domain
        or _infer_domain_from_response(response, run_id)
        or _AGENT_TO_DOMAIN.get(response.agent_name)
    )
    if not resolved_domain:
        print(
            f"[plat-agent] WARNING: cannot infer domain for "
            f"{response.agent_name} envelope persistence; skipping. "
            f"(no artifacts and agent_name not in _AGENT_TO_DOMAIN map)",
            file=sys.stderr,
            flush=True,
        )
        return None


def _load_persisted_response_envelope(
    request: BridgeRequestV1,
    *,
    dispatch_started_ts: float,
) -> dict | None:
    """Recover a sibling-written `_response_envelope.json` when stdout is empty.

    Some long-lived sibling sessions complete their real work and write the
    expected envelope on disk, but return empty stdout to the parent process.
    Accept that file as the bridge response when it is fresh for THIS dispatch,
    rather than forcing the caller down a salvage/error path.
    """
    domain = _AGENT_TO_DOMAIN.get(request.agent_name)
    if not domain:
        return None
    envelope_path = (
        Path(request.deal_root)
        / "outputs"
        / request.run_id
        / domain
        / "_response_envelope.json"
    )
    if not envelope_path.exists():
        return None
    # Guard against stale envelopes from a prior run attempt.
    if envelope_path.stat().st_mtime < dispatch_started_ts - 1:
        return None
    try:
        return json.loads(envelope_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _rebuild_comp_finder_envelope_from_artifacts(
    repo: SiblingRepo,
    request: BridgeRequestV1,
    *,
    log_path: Path,
) -> dict | None:
    """Ask market-study-agent's deterministic helper to emit the final envelope.

    This is the runtime backstop for the Willow Court failure mode: comp-finder
    sometimes leaves behind good on-disk artifacts but returns empty stdout.
    In that case we invoke the helper inside market-study-agent itself so the
    repo that owns the artifacts also owns the rebuilt BridgeResponseV1.
    """
    if request.agent_name != "comp-finder":
        return None
    helper = repo.path / "agents" / "runners" / "emit_comp_finder_envelope.py"
    if not helper.is_file():
        return None
    cmd = [
        "uv",
        "run",
        "python",
        str(helper.relative_to(repo.path)),
        "--deal-root",
        request.deal_root,
        "--deal-slug",
        request.deal_slug,
        "--run-id",
        request.run_id,
    ]
    try:
        result = subprocess.run(
            cmd,
            cwd=str(repo.path),
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except Exception as exc:  # noqa: BLE001
        print(
            f"[plat-agent] WARNING: failed to rebuild comp-finder envelope via helper: {exc!r}",
            file=sys.stderr,
            flush=True,
        )
        return None
    if result.stderr:
        try:
            with log_path.open("a", encoding="utf-8") as fh:
                fh.write("\n[plat-agent comp-finder envelope helper stderr]\n")
                fh.write(result.stderr)
        except Exception:
            pass
    if result.returncode != 0:
        return None
    stdout = (result.stdout or "").strip()
    if not stdout:
        return None
    json_doc = _extract_last_json_document(stdout)
    if json_doc is None:
        return None
    try:
        return json.loads(json_doc)
    except json.JSONDecodeError:
        return None


def _finalize_response(
    *,
    parsed: dict,
    request: BridgeRequestV1,
    payload_model: Optional[Type[BaseModel]],
) -> BridgeResponseV1:
    """Normalize, validate, and persist a parsed sibling response."""
    # Normalize common sibling response variations BEFORE strict validation
    # so productive sibling responses don't fail on cosmetic enum differences.
    if isinstance(parsed, dict):
        artifacts = parsed.get("artifacts")
        if isinstance(artifacts, list):
            for a in artifacts:
                if not isinstance(a, dict):
                    continue
                kind = a.get("kind")
                if kind == "markdown":
                    a["kind"] = "md"
                elif kind == "yaml" or kind == "yml":
                    a["kind"] = "json"  # closest valid schema literal

    response = BridgeResponseV1.model_validate(parsed)

    try:
        _validate_artifact_paths(
            response,
            request.deal_root,
            request.run_id,
            request.agent_name,
        )
    except ValueError as exc:
        response = _bridge_response_with_schema_violation(
            request, response, str(exc)
        )
    else:
        if response.status == "ok":
            try:
                _check_artifact_existence(response, request.deal_root)
            except ValueError as exc:
                response = _bridge_response_with_schema_violation(
                    request, response, str(exc)
                )

    if (
        payload_model is not None
        and response.status == "ok"
        and response.payload is not None
    ):
        try:
            payload_model.model_validate(response.payload)
        except pydantic.ValidationError as ve:
            response = _bridge_response_with_schema_violation(
                request,
                response,
                f"Sibling payload failed {payload_model.__name__} "
                f"validation: {ve}",
            )

    _persist_response_envelope(response, request.deal_root, request.run_id)
    return response
    try:
        target_dir = Path(deal_root) / "outputs" / run_id / resolved_domain
        target = target_dir / "_response_envelope.json"
        _atomic_write_text(target, response.model_dump_json(indent=2))
        return target
    except Exception as exc:  # noqa: BLE001 — best-effort persistence
        print(
            f"[plat-agent] WARNING: failed to persist response envelope for "
            f"{response.agent_name} -> {resolved_domain}: {exc!r}",
            file=sys.stderr,
            flush=True,
        )
        return None


def _bridge_response_with_schema_violation(
    request: BridgeRequestV1,
    response: BridgeResponseV1,
    message: str,
) -> BridgeResponseV1:
    """Translate a post-envelope validation failure into a BridgeError-shaped result.

    Preserves provenance and artifacts from the original sibling response so
    the operator can see what the sibling claimed even when its claims failed
    validation.
    """
    return BridgeResponseV1(
        deal_slug=request.deal_slug,
        run_id=request.run_id,
        agent_name=request.agent_name,
        status="error",
        error=BridgeError(
            code="schema_violation",
            message=message,
            recoverable=False,
        ),
        provenance=response.provenance,
        artifacts=response.artifacts,
    )


def dispatch_sibling_agent(
    repo: SiblingRepo,
    request: BridgeRequestV1,
    *,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    claude_binary: str = "claude",
    extra_args: Optional[list[str]] = None,
    log_dir: Optional[Path] = None,
    payload_model: Optional[Type[BaseModel]] = None,
) -> BridgeResponseV1:
    """Invoke `claude -p` in `repo.path` with a BridgeRequestV1 JSON prompt.

    The sibling agent named in `request.agent_name` must exist at
    `<repo.path>/.claude/agents/<agent_name>.md`. Its body (with YAML
    frontmatter stripped) is written to a NamedTemporaryFile under `log_dir`
    and injected into the receiving session via
    `--append-system-prompt-file`. The `--agent` flag is intentionally not
    used: it is unreliable and may leave the session running with no agent
    prompt at all.

    The receiving session must emit exactly one BridgeResponseV1 JSON
    document to stdout.

    Stderr is streamed to a per-dispatch log file under `log_dir` (default:
    `tempfile.gettempdir()`). The path is announced on the orchestrator's
    own stderr and, on failure, embedded in the DispatchError message.

    Tmpfile lifecycle: deleted only on a successful BridgeResponseV1 parse.
    Any failure path (missing repo/agent, subprocess non-zero exit, timeout,
    unparseable stdout, schema mismatch) preserves the tmpfile alongside
    the stderr log for diagnostics.

    Args:
        payload_model: Optional Pydantic model class. When provided AND the
            sibling response status is 'ok', the response payload dict is
            validated against this model. A validation failure is translated
            into a BridgeResponseV1(status='error', error.code='schema_violation')
            rather than raised — so the caller sees a structured error matching
            the rest of the bridge contract.

    Raises:
        DispatchError: subprocess failed, timed out, or stdout was unparseable.
            The exception carries `log_path` to the captured stderr file.
        pydantic.ValidationError: stdout parsed as JSON but did not match
            the BridgeResponseV1 schema (re-raised so the caller sees the
            specific field problem). NOTE: payload_model failures do NOT
            re-raise — they are translated to a status='error' response.
    """
    if not repo.path.is_dir():
        raise DispatchError(f"Sibling repo path does not exist: {repo.path}")
    agent_file = repo.path / ".claude" / "agents" / f"{request.agent_name}.md"
    if not agent_file.is_file():
        raise DispatchError(
            f"Sibling agent definition not found: {agent_file}. "
            f"Expected file under {repo.path}/.claude/agents/."
        )

    resolved_log_dir = Path(log_dir) if log_dir is not None else Path(tempfile.gettempdir())
    resolved_log_dir.mkdir(parents=True, exist_ok=True)
    log_path = _build_log_path(resolved_log_dir, request.deal_slug, request.agent_name)

    # Materialize the agent body (frontmatter stripped) to a tmpfile for
    # diagnostic preservation on failure (we still log the path even though
    # the body itself is now passed inline via --append-system-prompt).
    agent_body = _strip_yaml_frontmatter(agent_file.read_text(encoding="utf-8"))
    tmp_handle = tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".md",
        delete=False,
        dir=str(resolved_log_dir),
        encoding="utf-8",
    )
    try:
        tmp_handle.write(agent_body)
    finally:
        tmp_handle.close()
    agent_prompt_path = Path(tmp_handle.name)

    prompt = json.dumps(request.model_dump(mode="json"))
    # Argument ordering matters: `--add-dir` is variadic and would otherwise
    # consume the trailing prompt as another directory. We put it BEFORE
    # `--append-system-prompt` (a single-value flag that terminates --add-dir
    # consumption) and add `--` before the positional prompt as belt-and-suspenders.
    # We pass the agent body inline rather than via `--append-system-prompt-file`
    # because the file-variant flag is undocumented in the top-level help and
    # has shown inconsistent recognition across CLI versions.
    # Permission model: --add-dir alone is path-scope only; in headless -p mode
    # the default permission-mode silently denies Write/Edit/Bash and the model
    # fabricates a "success" summary rather than surfacing the failure (proven
    # by minimal repro — see docs/wall_3_diagnosis.md). Pass bypassPermissions
    # so sibling sessions can actually write artifacts under
    # <deal_root>/outputs/<run_id>/<domain>/. The sandbox weakening is bounded:
    # the spawned session runs the named sibling agent with its prompt-declared
    # `tools:` list, in the sibling repo's cwd, with --add-dir scoping which
    # cross-repo paths are visible.
    cmd = [
        claude_binary,
        "-p",
        "--permission-mode",
        "bypassPermissions",
        "--add-dir",
        request.deal_root,
        "--append-system-prompt",
        agent_body,
        "--",
        prompt,
    ]
    if extra_args:
        cmd.extend(extra_args)

    print(
        f"[plat-agent] dispatching {request.agent_name} -> {repo.name}; "
        f"stderr log: {log_path}; agent prompt: {agent_prompt_path}",
        file=sys.stderr,
        flush=True,
    )

    dispatch_started_ts = datetime.now(timezone.utc).timestamp()
    log_file = log_path.open("w", encoding="utf-8")
    log_closed = False
    proc = subprocess.Popen(
        cmd,
        cwd=str(repo.path),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )

    # Pump stderr to the log file in a background thread so it survives a
    # timeout-induced kill. Without this, Popen.communicate(timeout=) would
    # discard any unread stderr when it raises TimeoutExpired.
    def _pump_stderr() -> None:
        assert proc.stderr is not None
        try:
            for line in proc.stderr:
                log_file.write(line)
                log_file.flush()
        except Exception as pump_exc:  # noqa: BLE001 — best-effort logging
            try:
                log_file.write(f"\n[plat-agent stderr pump error] {pump_exc!r}\n")
                log_file.flush()
            except Exception:
                pass

    stderr_thread = threading.Thread(target=_pump_stderr, daemon=True)
    stderr_thread.start()

    stdout_data = ""
    timed_out = False
    effective_timeout_seconds = _effective_timeout_seconds_for_request(
        request,
        timeout_seconds,
    )
    deadline = time.monotonic() + effective_timeout_seconds
    poll_interval = 2.0 if request.agent_name == "comp-finder" else 0.25
    try:
        while True:
            if proc.poll() is not None:
                stdout_data, _ = proc.communicate(timeout=5)
                break

            if request.agent_name == "comp-finder":
                early = _try_early_persisted_response(
                    repo=repo,
                    request=request,
                    dispatch_started_ts=dispatch_started_ts,
                    log_path=log_path,
                    payload_model=payload_model,
                    allow_rebuild=False,
                )
                if early is not None:
                    proc.kill()
                    try:
                        proc.communicate(timeout=5)
                    except subprocess.TimeoutExpired:
                        pass
                    stderr_thread.join(timeout=2)
                    if not log_closed:
                        log_file.flush()
                        log_file.close()
                        log_closed = True
                    try:
                        agent_prompt_path.unlink()
                    except FileNotFoundError:
                        pass
                    return early

            if time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(cmd=cmd, timeout=effective_timeout_seconds)
            time.sleep(poll_interval)
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        proc.kill()
        # Drain whatever stdout/stderr is still buffered after the kill.
        try:
            stdout_data, _ = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            stdout_data = exc.stdout.decode("utf-8", errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        finally:
            stderr_thread.join(timeout=2)
            if not log_closed:
                log_file.flush()
                log_file.close()
                log_closed = True
        partial = (stdout_data or "").strip()
        tail = partial[-500:] if partial else "(no stdout captured)"
        early = _try_early_persisted_response(
            repo=repo,
            request=request,
            dispatch_started_ts=dispatch_started_ts,
            log_path=log_path,
            payload_model=payload_model,
        )
        if early is not None:
            try:
                agent_prompt_path.unlink()
            except FileNotFoundError:
                pass
            return early
        raise DispatchError(
            f"Sibling agent {request.agent_name} in {repo.name} timed out after "
            f"{effective_timeout_seconds} seconds. Stderr log: {log_path}. "
            f"Last 500 chars of stdout: {tail}",
            log_path=log_path,
        ) from exc
    finally:
        if not timed_out:
            stderr_thread.join(timeout=2)
            if not log_closed:
                log_file.flush()
                log_file.close()
                log_closed = True

    if proc.returncode != 0:
        raise DispatchError(
            f"Sibling agent {request.agent_name} in {repo.name} exited "
            f"non-zero ({proc.returncode}). Stderr log: {log_path}",
            log_path=log_path,
        )

    stdout = (stdout_data or "").strip()
    if not stdout:
        early = _try_early_persisted_response(
            repo=repo,
            request=request,
            dispatch_started_ts=dispatch_started_ts,
            log_path=log_path,
            payload_model=payload_model,
        )
        if early is None:
            raise DispatchError(
                f"Sibling agent {request.agent_name} in {repo.name} produced "
                f"empty stdout. Stderr log: {log_path}",
                log_path=log_path,
            )
        try:
            agent_prompt_path.unlink()
        except FileNotFoundError:
            pass
        return early

    # The sibling may emit the JSON anywhere in stdout (e.g. after status lines).
    # Look for the last balanced JSON document — it should be the BridgeResponseV1.
    json_doc = _extract_last_json_document(stdout)
    if json_doc is None:
        raise DispatchError(
            f"Sibling agent {request.agent_name} in {repo.name} did not "
            f"emit parseable JSON. Stderr log: {log_path}. "
            f"Stdout (truncated): {stdout[:2000]}",
            log_path=log_path,
        )

    try:
        parsed = json.loads(json_doc)
    except json.JSONDecodeError as exc:
        raise DispatchError(
            f"Sibling agent {request.agent_name} in {repo.name} emitted "
            f"malformed JSON: {exc}. Stderr log: {log_path}. "
            f"Document: {json_doc[:2000]}",
            log_path=log_path,
        ) from exc

    try:
        response = _finalize_response(
            parsed=parsed,
            request=request,
            payload_model=payload_model,
        )
    except ValidationError:
        # Salvage the raw stdout next to the stderr log so we can inspect
        # near-misses (e.g. sibling returned a list instead of an envelope,
        # or used string artifacts instead of ArtifactRef objects). Without
        # this the response evaporates and we have no diagnostic.
        raw_path = log_path.with_suffix(".raw_response.json")
        try:
            raw_path.write_text(json.dumps(parsed, indent=2, default=str), encoding="utf-8")
            print(
                f"[plat-agent] {request.agent_name} response failed schema validation; "
                f"raw response saved to {raw_path}",
                file=sys.stderr,
                flush=True,
            )
        except Exception:  # noqa: BLE001 — best-effort diagnostic
            pass
        raise

    # Only delete the agent prompt tmpfile on a fully successful parse. Any
    # failure path above leaves it on disk alongside the stderr log so the
    # dispatch is reproducible from the recorded artifacts.
    try:
        agent_prompt_path.unlink()
    except FileNotFoundError:
        pass
    return response


def _extract_last_json_document(text: str) -> Optional[str]:
    """Return the last balanced top-level JSON object found in `text`, or None.

    Sibling agents may emit progress lines before the final response. We scan
    backwards for the last well-formed JSON object/array.
    """
    decoder = json.JSONDecoder()
    last: Optional[str] = None
    idx = 0
    while idx < len(text):
        ch = text[idx]
        if ch in "{[":
            try:
                _, end = decoder.raw_decode(text, idx)
            except json.JSONDecodeError:
                idx += 1
                continue
            last = text[idx:end]
            idx = end
            continue
        idx += 1
    return last
