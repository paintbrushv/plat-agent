"""UnderwritingStep — dispatches to multifamily-underwriting/underwriting-runner.

Reads:  <run_dir>/judgment/engine_inputs.json (schema-v0.1 inputs)
Writes (via the sibling agent):
        <run_dir>/underwriting/deal_summary.json
        <run_dir>/underwriting/_provenance.json
        <run_dir>/underwriting/_response_envelope.json
        <run_dir>/underwriting/_complete  (lifecycle-owned, written here)

Per spec §2.4. Engine is owned by multifamily-underwriting; this adapter
is the lifecycle-side glue that dispatches the federated underwriting-runner
agent and verifies its on-disk artifacts.

History: an earlier shipped V1 called the legacy MCP `run_summary` path via
UnderwritingClient. That returned a different (lighter) payload with no
`_provenance.json` and no `_response_envelope.json`, breaking downstream
recommendation_patch / memo / CRM consumers that read `feasibility_sanity_flags`.
known-issues-v1 P1 #3 mandated the switch to federated dispatch.
"""

from __future__ import annotations

import hashlib
import os
import json
import subprocess
import shutil
import sys
import traceback
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from plat_agent.contracts.domain.underwriting import (
    UnderwritingProvenance,
    UnderwritingRequest,
    UnderwritingResponse,
)
from plat_agent.contracts.envelope import BridgeError, BridgeRequestV1, BridgeResponseV1
from plat_agent.dispatch.sibling import (
    DispatchError,
    SiblingRepo,
    dispatch_sibling_agent as _dispatch_sibling_agent,
)
from plat_agent.lifecycle.atomic import atomic_write_json
from plat_agent.lifecycle.cache import (
    compute_input_hash,
    is_satisfied as cache_is_satisfied,
    write_provenance,
)
from plat_agent.lifecycle.complete_marker import write_complete_marker
from plat_agent.lifecycle.defaults import CONTRACT_VERSION
from plat_agent.lifecycle.protocol import StepResult
from plat_agent.lifecycle.state import LifecycleState


# Sibling repo containing the underwriting-runner agent.
MFU_SIBLING_NAME = "multifamily-underwriting"


def _direct_underwriting_response(
    repo: SiblingRepo,
    request: BridgeRequestV1,
    *,
    payload_model=None,
) -> BridgeResponseV1:
    request_payload = UnderwritingRequest.model_validate(request.payload)
    script_path = repo.path / "runs" / "federated_underwrite.py"
    if not script_path.exists():
        raise DispatchError(
            f"deterministic underwriting runner missing at {script_path}"
        )

    python_bin = repo.path / ".venv" / "bin" / "python"
    command = [
        str(python_bin if python_bin.exists() else Path(sys.executable)),
        str(script_path),
        "--deal-root",
        request.deal_root,
        "--deal-slug",
        request.deal_slug,
        "--run-id",
        request.run_id,
        "--canonical-relative",
        request_payload.canonical_deal_json_relative,
    ]
    if request_payload.renovation_programs_relative:
        command.extend(
            [
                "--renovation-programs-relative",
                request_payload.renovation_programs_relative,
            ]
        )
    if request_payload.output_workbook:
        command.append("--output-workbook")

    timeout_seconds = int(os.environ.get("PLAT_UNDERWRITING_TIMEOUT_SECONDS", "900"))

    try:
        completed = subprocess.run(
            command,
            cwd=str(repo.path),
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        raise DispatchError(
            f"deterministic underwriting runner timed out after {timeout_seconds}s"
        ) from exc
    stdout = (completed.stdout or "").strip()
    stderr = (completed.stderr or "").strip()
    if not stdout:
        raise DispatchError(
            "deterministic underwriting runner produced no stdout"
            + (f"; stderr={stderr}" if stderr else "")
        )
    try:
        parsed = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise DispatchError(
            f"deterministic underwriting runner emitted invalid JSON: {exc}"
        ) from exc

    try:
        response = BridgeResponseV1.model_validate(parsed)
    except ValidationError:
        error_payload = parsed.get("error")
        if (
            isinstance(error_payload, dict)
            and parsed.get("status") == "error"
            and isinstance(error_payload.get("details"), list)
        ):
            repaired = dict(parsed)
            repaired["error"] = {
                "code": error_payload.get("code", "unknown"),
                "message": error_payload.get("message")
                or "deterministic underwriting runner returned an invalid BridgeError payload",
                "recoverable": bool(error_payload.get("recoverable", False)),
                "details": {"issues": error_payload.get("details", [])},
            }
            response = BridgeResponseV1.model_validate(repaired)
        else:
            raise

    if payload_model is not None and response.payload is not None and response.status == "ok":
        response = response.model_copy(
            update={
                "payload": payload_model.model_validate(response.payload).model_dump(
                    mode="json"
                )
            }
        )
    return response


def dispatch_sibling_agent(repo, request, **kwargs):
    if request.agent_name == "underwriting-runner":
        return _direct_underwriting_response(
            repo, request, payload_model=kwargs.get("payload_model")
        )
    return _dispatch_sibling_agent(repo, request, **kwargs)


def _compute_engine_inputs_hash(inputs: dict[str, Any]) -> str:
    """Match multifamily-underwriting engine.api._compute_inputs_hash."""
    payload = json.dumps(inputs, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _validate_engine_input_artifacts(
    *,
    judgment_inputs_path: Path,
    underwriting_inputs_path: Path,
    engine_provenance: dict[str, Any],
) -> None:
    judgment_inputs = json.loads(judgment_inputs_path.read_text())
    underwriting_inputs = json.loads(underwriting_inputs_path.read_text())
    if underwriting_inputs != judgment_inputs:
        raise ValueError(
            "underwriting/inputs.json does not semantically match "
            "judgment/engine_inputs.json"
        )
    expected_hash = _compute_engine_inputs_hash(underwriting_inputs)
    if engine_provenance.get("inputs_hash_sha256") != expected_hash:
        raise ValueError(
            "underwriting provenance inputs_hash_sha256 does not match "
            f"underwriting/inputs.json: expected {expected_hash}"
        )


def synchronize_underwriting_provenance(run_dir: Path) -> dict[str, Any]:
    """Combine verified engine provenance with lifecycle cache metadata."""
    step_dir = run_dir / "underwriting"
    provenance_path = step_dir / "_provenance.json"
    manifest = [
        "deal_summary.json",
        "inputs.json",
        "_provenance.json",
        "_response_envelope.json",
    ]
    missing = [name for name in manifest if not (step_dir / name).exists()]
    if missing:
        raise FileNotFoundError(
            f"underwriting provenance synchronization missing: {', '.join(missing)}"
        )

    current = json.loads(provenance_path.read_text())
    engine_provenance = current.get("federated_provenance") or current
    UnderwritingProvenance.model_validate(engine_provenance)
    _validate_engine_input_artifacts(
        judgment_inputs_path=run_dir / "judgment" / "engine_inputs.json",
        underwriting_inputs_path=step_dir / "inputs.json",
        engine_provenance=engine_provenance,
    )

    write_provenance(
        step_dir,
        input_hash=compute_input_hash([run_dir / "judgment" / "engine_inputs.json"]),
        contract_version=CONTRACT_VERSION,
        status="ok",
        extra={
            **engine_provenance,
            "federated_provenance": engine_provenance,
        },
    )
    write_complete_marker(
        step_dir,
        step="underwriting",
        file_manifest=manifest,
    )
    return json.loads(provenance_path.read_text())


class UnderwritingStep:
    """LifecycleStep implementation for the federated underwriting subsystem."""

    name = "underwriting"

    def __init__(
        self,
        client: Any | None = None,
        *,
        sibling_repo: SiblingRepo | None = None,
        dispatcher: Any = None,
    ) -> None:
        # `client` is retained for backward compatibility with older fixtures /
        # tests that injected a mock with `.run_summary(inputs) -> dict`. When
        # provided, the client path runs (legacy MCP) and the sibling-dispatch
        # path is bypassed. Production callers MUST use the dispatcher path.
        self._client = client
        self._sibling_repo = sibling_repo
        self._dispatcher = dispatcher or dispatch_sibling_agent

    # ------------------------------------------------------------------ paths
    def _engine_inputs_path(self, run_dir: Path) -> Path:
        return run_dir / "judgment" / "engine_inputs.json"

    def _input_hash(self, run_dir: Path) -> str:
        return compute_input_hash([self._engine_inputs_path(run_dir)])

    def _derive_deal_root(self, run_dir: Path) -> Path:
        """run_dir = <deal_root>/outputs/<run_id>/. Walk up two parents."""
        return run_dir.parent.parent

    def _resolve_sibling_repo(self) -> SiblingRepo:
        if self._sibling_repo is not None:
            return self._sibling_repo
        return SiblingRepo.from_env_or_default(
            MFU_SIBLING_NAME, default_relative=MFU_SIBLING_NAME
        )

    # -------------------------------------------------------- federated path
    def _build_request(
        self, state: LifecycleState, run_dir: Path
    ) -> BridgeRequestV1:
        deal_root = self._derive_deal_root(run_dir)
        engine_inputs_rel = (
            self._engine_inputs_path(run_dir)
            .relative_to(deal_root)
            .as_posix()
        )
        payload = UnderwritingRequest(
            canonical_deal_json_relative=engine_inputs_rel,
            output_workbook=False,
        )
        return BridgeRequestV1(
            deal_slug=state.deal_slug,
            run_id=state.run_id,
            deal_root=str(deal_root),
            agent_name="underwriting-runner",
            payload=payload.model_dump(mode="json"),
        )

    def _verify_response_artifacts(
        self,
        deal_root: Path,
        run_dir: Path,
        payload: UnderwritingResponse,
    ) -> str | None:
        """Verify the underwriting-runner wrote the artifacts it claimed.

        The runner contract guarantees deal_summary.json + _provenance.json +
        _response_envelope.json under outputs/<run>/underwriting/. We check
        the deal-root-relative path from `payload.summary_relative`; the
        other two are conventionally co-located (verified by relative).
        """
        missing: list[str] = []
        summary_abs = deal_root / payload.summary_relative
        if not summary_abs.exists():
            missing.append(f"summary_relative={payload.summary_relative}")

        # Provenance + response envelope: located alongside summary by
        # convention. Use summary's parent so we tolerate non-standard layouts.
        underwriting_dir = summary_abs.parent
        if not (underwriting_dir / "_provenance.json").exists():
            missing.append(f"_provenance.json under {underwriting_dir}")
        if not (underwriting_dir / "inputs.json").exists():
            missing.append(f"inputs.json under {underwriting_dir}")
        if not (underwriting_dir / "_response_envelope.json").exists():
            missing.append(f"_response_envelope.json under {underwriting_dir}")
        provenance_path = underwriting_dir / "_provenance.json"
        if provenance_path.exists() and (underwriting_dir / "inputs.json").exists():
            try:
                engine_provenance = json.loads(provenance_path.read_text())
                UnderwritingProvenance.model_validate(engine_provenance)
                _validate_engine_input_artifacts(
                    judgment_inputs_path=self._engine_inputs_path(run_dir),
                    underwriting_inputs_path=underwriting_dir / "inputs.json",
                    engine_provenance=engine_provenance,
                )
            except (OSError, json.JSONDecodeError, ValidationError, ValueError) as exc:
                missing.append(f"valid engine input/provenance contract: {exc}")

        if missing:
            return (
                "underwriting-runner claimed status=ok but required artifacts "
                f"missing under {deal_root}: {', '.join(missing)}"
            )
        return None

    def _mirror_artifacts_into_run_dir(
        self,
        deal_root: Path,
        run_dir: Path,
        payload: UnderwritingResponse,
    ) -> None:
        """Copy sibling-emitted underwriting artifacts into outputs/<run>/underwriting/.

        Downstream consumers (recommendation_patch.py, memo.py, crm.py) read
        from a fixed run-scoped path. The runner contract typically writes
        directly to that path already, but we mirror defensively when
        `summary_relative` resolves elsewhere.
        """
        local_dir = run_dir / "underwriting"
        local_dir.mkdir(parents=True, exist_ok=True)

        summary_src = deal_root / payload.summary_relative
        summary_dst = local_dir / "deal_summary.json"
        if summary_src.exists() and summary_src.resolve() != summary_dst.resolve():
            shutil.copy2(summary_src, summary_dst)

        # Mirror co-located provenance + envelope when they exist outside
        # the lifecycle-owned local dir.
        src_dir = summary_src.parent
        for fname in ("inputs.json", "_provenance.json", "_response_envelope.json"):
            src = src_dir / fname
            dst = local_dir / fname
            if not src.exists():
                continue
            if src.resolve() == dst.resolve():
                continue
            shutil.copy2(src, dst)

    def _run_via_dispatcher(
        self, state: LifecycleState, run_dir: Path
    ) -> StepResult:
        deal_root = self._derive_deal_root(run_dir)
        step_dir = run_dir / "underwriting"
        step_dir.mkdir(parents=True, exist_ok=True)
        input_hash = self._input_hash(run_dir)

        try:
            request = self._build_request(state, run_dir)
        except Exception as exc:  # noqa: BLE001
            err_msg = f"{type(exc).__name__}: {exc}"
            tb = traceback.format_exc()
            write_provenance(
                step_dir,
                input_hash=input_hash,
                contract_version=CONTRACT_VERSION,
                status="error",
                extra={"error_message": err_msg, "error_traceback": tb},
            )
            return StepResult(status="error", error_message=err_msg, error_traceback=tb)

        repo = self._resolve_sibling_repo()
        try:
            response = self._dispatcher(
                repo, request, payload_model=UnderwritingResponse
            )
        except DispatchError as exc:
            err_msg = f"underwriting-runner dispatch failed: {exc}"
            write_provenance(
                step_dir,
                input_hash=input_hash,
                contract_version=CONTRACT_VERSION,
                status="error",
                extra={"error_message": err_msg},
            )
            return StepResult(status="error", error_message=err_msg)

        if response.status == "error":
            err = response.error
            err_msg = (err.message if err else "unknown underwriting error")
            write_provenance(
                step_dir,
                input_hash=input_hash,
                contract_version=CONTRACT_VERSION,
                status="error",
                extra={
                    "error_code": (err.code if err else "unknown"),
                    "error_message": err_msg,
                },
            )
            return StepResult(
                status="error",
                error_message=f"underwriting-runner error: {err_msg}",
            )

        # V1.4 — None payload short-circuit. The sibling reports status=ok or
        # status=needs_analyst_input but did not produce a summary body. We
        # cannot continue (no metrics → no memo → no CRM row), so flag this
        # as an error with a clear message; the orchestrator can surface
        # the upstream agent's own error if present.
        if response.payload is None:
            err = response.error
            agent_msg = (
                err.message
                if err is not None
                else f"underwriting-runner returned status={response.status} with payload=None"
            )
            err_msg = (
                "underwriting-runner produced no payload — "
                f"{agent_msg}. Engine likely failed silently or hit an "
                "unrecoverable input gap. Inspect judgment/engine_inputs.json "
                "and rerun underwriting in isolation."
            )
            write_provenance(
                step_dir,
                input_hash=input_hash,
                contract_version=CONTRACT_VERSION,
                status="error",
                extra={
                    "error_code": "empty_payload_response",
                    "error_message": err_msg,
                },
            )
            return StepResult(status="error", error_message=err_msg)

        # Parse + verify artifacts emitted by the sibling.
        try:
            payload = UnderwritingResponse.model_validate(response.payload or {})
        except ValidationError as exc:
            err_msg = f"underwriting-runner returned malformed payload: {exc}"
            write_provenance(
                step_dir,
                input_hash=input_hash,
                contract_version=CONTRACT_VERSION,
                status="error",
                extra={"error_message": err_msg},
            )
            return StepResult(status="error", error_message=err_msg)

        artifact_err = self._verify_response_artifacts(deal_root, run_dir, payload)
        if artifact_err is not None:
            write_provenance(
                step_dir,
                input_hash=input_hash,
                contract_version=CONTRACT_VERSION,
                status="error",
                extra={"error_message": artifact_err},
            )
            return StepResult(status="error", error_message=artifact_err)

        # Mirror sibling artifacts into the lifecycle-owned location.
        self._mirror_artifacts_into_run_dir(deal_root, run_dir, payload)

        # Preserve the standardized engine provenance at top level while adding
        # lifecycle cache fields and a nested immutable engine copy.
        synchronize_underwriting_provenance(run_dir)
        return StepResult(status="ok")

    # ------------------------------------------------------------- legacy path
    def _run_via_client(
        self, state: LifecycleState, run_dir: Path
    ) -> StepResult:
        """Legacy MCP path. Kept for backward compatibility with fixtures /
        tests that inject a mock client. Production code should use the
        federated dispatcher path.
        """
        engine_inputs_path = self._engine_inputs_path(run_dir)
        step_dir = run_dir / "underwriting"
        step_dir.mkdir(parents=True, exist_ok=True)
        input_hash = self._input_hash(run_dir)

        try:
            inputs = json.loads(engine_inputs_path.read_text())
            summary = self._client.run_summary(inputs)
        except Exception as exc:  # noqa: BLE001
            err_msg = f"{type(exc).__name__}: {exc}"
            tb = traceback.format_exc()
            write_provenance(
                step_dir,
                input_hash=input_hash,
                contract_version=CONTRACT_VERSION,
                status="error",
                extra={"error_message": err_msg, "error_traceback": tb},
            )
            return StepResult(status="error", error_message=err_msg, error_traceback=tb)

        atomic_write_json(step_dir / "deal_summary.json", summary)
        # Legacy provenance does NOT carry feasibility_sanity_flags. That's
        # the wiring bug known-issues-v1 P1 #3 calls out — it's why we want
        # to migrate callers to the dispatcher path.
        write_provenance(
            step_dir,
            input_hash=input_hash,
            contract_version=CONTRACT_VERSION,
            status="ok",
            # Surface sanity flags from the summary if the legacy client
            # carries them at top level (older MCP responses occasionally do).
            extra={
                "feasibility_sanity_flags": list(summary.get("sanity_flags", []) or []),
            },
        )
        write_complete_marker(
            step_dir,
            step="underwriting",
            file_manifest=["deal_summary.json", "_provenance.json"],
        )
        return StepResult(status="ok")

    # ------------------------------------------------------------------- run
    def run(self, state: LifecycleState, run_dir: Path) -> StepResult:
        if self._client is not None:
            # Backward-compat path for tests / fixtures.
            return self._run_via_client(state, run_dir)
        return self._run_via_dispatcher(state, run_dir)

    def is_satisfied(self, state: LifecycleState, run_dir: Path) -> bool:
        return cache_is_satisfied(
            state, run_dir, step="underwriting",
            current_input_hash=self._input_hash(run_dir),
        )
