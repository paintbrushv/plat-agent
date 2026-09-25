"""Wave 2 Task 2.2 — payload validation, artifact-path scope, and existence
checks at the sibling-dispatch boundary.

Covers:
  - HIGH-1 (Stage 3): payload_model parameter validates response.payload
    when status='ok'.
  - HIGH-4 (Stage 3): ArtifactRef.relative_path must resolve under
    outputs/<run_id>/.
  - HIGH-2 (Stage 5) / HIGH-4 (Stage 3): declared artifacts must exist on
    disk after dispatch.
  - Backward compatibility: callers that don't pass payload_model still get
    the legacy behavior (no inner-payload validation).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional
from unittest.mock import patch

import pytest
from pydantic import BaseModel

from plat_agent.contracts.envelope import (
    ArtifactRef,
    BridgeRequestV1,
    BridgeResponseV1,
)
from plat_agent.dispatch.sibling import (
    SiblingRepo,
    dispatch_sibling_agent,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

class _SamplePayload(BaseModel):
    """Tiny payload model — proxy for CostBridgeResponse / UnderwritingResponse
    in these tests so we don't pull engine deps."""

    answer: int
    label: str


def _make_sibling_repo(tmp_path: Path, agent_name: str = "fake-agent") -> SiblingRepo:
    repo_root = tmp_path / "fake-sibling"
    agents_dir = repo_root / ".claude" / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    (agents_dir / f"{agent_name}.md").write_text("# fake agent\n")
    return SiblingRepo(name="fake-sibling", path=repo_root)


def _make_request(deal_root: Path, agent_name: str = "fake-agent") -> BridgeRequestV1:
    return BridgeRequestV1(
        deal_slug="test_deal",
        run_id="run_001",
        deal_root=str(deal_root),
        agent_name=agent_name,
        payload={"hello": "world"},
    )


def _make_response_json(
    *,
    payload: dict,
    artifacts: Optional[list[ArtifactRef]] = None,
    status: str = "ok",
    agent_name: str = "fake-agent",
) -> str:
    resp = BridgeResponseV1(
        status=status,
        deal_slug="test_deal",
        run_id="run_001",
        agent_name=agent_name,
        payload=payload,
        artifacts=artifacts or [],
    )
    return resp.model_dump_json()


class _FakePopen:
    def __init__(self, *, stdout_text: str, returncode: int = 0) -> None:
        self._stdout_text = stdout_text
        self.returncode = returncode
        self.stderr = iter([])

    def communicate(self, timeout=None):
        return self._stdout_text, ""

    def kill(self):
        pass


def _make_deal_root(tmp_path: Path) -> Path:
    """Create the outputs/<run_id>/ scope so existence checks have somewhere
    legitimate to look."""
    deal_root = tmp_path / "deal"
    (deal_root / "outputs" / "run_001").mkdir(parents=True)
    return deal_root


# ---------------------------------------------------------------------------
# payload_model validation
# ---------------------------------------------------------------------------

def test_payload_model_validates_when_provided(tmp_path: Path) -> None:
    """A malformed payload + payload_model=_SamplePayload → status='error',
    code='schema_violation'. The orchestrator gets a structured BridgeError
    rather than a Pydantic ValidationError raised through the call site."""
    deal_root = _make_deal_root(tmp_path)
    repo = _make_sibling_repo(tmp_path)
    request = _make_request(deal_root)

    bad_payload = {"answer": "not_an_int", "label": 42}  # both wrong types
    fake = _FakePopen(stdout_text=_make_response_json(payload=bad_payload) + "\n")

    with patch("plat_agent.dispatch.sibling.subprocess.Popen", return_value=fake):
        resp = dispatch_sibling_agent(
            repo,
            request,
            timeout_seconds=30,
            log_dir=tmp_path / "logs",
            payload_model=_SamplePayload,
        )

    assert resp.status == "error"
    assert resp.error is not None
    assert resp.error.code == "schema_violation"
    assert "_SamplePayload" in resp.error.message
    assert resp.error.recoverable is False


def test_payload_model_passes_on_valid_payload(tmp_path: Path) -> None:
    """A clean payload validates and is returned unchanged."""
    deal_root = _make_deal_root(tmp_path)
    repo = _make_sibling_repo(tmp_path)
    request = _make_request(deal_root)

    good_payload = {"answer": 42, "label": "ok"}
    fake = _FakePopen(stdout_text=_make_response_json(payload=good_payload) + "\n")

    with patch("plat_agent.dispatch.sibling.subprocess.Popen", return_value=fake):
        resp = dispatch_sibling_agent(
            repo,
            request,
            timeout_seconds=30,
            log_dir=tmp_path / "logs",
            payload_model=_SamplePayload,
        )

    assert resp.status == "ok"
    assert resp.payload == good_payload
    assert resp.error is None


def test_payload_model_optional(tmp_path: Path) -> None:
    """Legacy callers (no payload_model) get the same behavior as before:
    a payload that wouldn't satisfy any model is still accepted because no
    model validation runs."""
    deal_root = _make_deal_root(tmp_path)
    repo = _make_sibling_repo(tmp_path)
    request = _make_request(deal_root)

    arbitrary_payload = {"this": "would_fail_any_model", "if": ["validated"]}
    fake = _FakePopen(stdout_text=_make_response_json(payload=arbitrary_payload) + "\n")

    with patch("plat_agent.dispatch.sibling.subprocess.Popen", return_value=fake):
        resp = dispatch_sibling_agent(
            repo,
            request,
            timeout_seconds=30,
            log_dir=tmp_path / "logs",
            # NO payload_model → legacy path.
        )

    assert resp.status == "ok"
    assert resp.payload == arbitrary_payload


def test_payload_model_skipped_when_status_is_error(tmp_path: Path) -> None:
    """When the sibling already returned status='error', we don't run
    payload_model on the (likely empty) payload — the error is the signal."""
    deal_root = _make_deal_root(tmp_path)
    repo = _make_sibling_repo(tmp_path)
    request = _make_request(deal_root)

    error_response = BridgeResponseV1(
        status="error",
        deal_slug="test_deal",
        run_id="run_001",
        agent_name="fake-agent",
        payload={},  # empty — would fail _SamplePayload
        error={"code": "missing_input", "message": "no inputs", "recoverable": False},
    ).model_dump_json()
    fake = _FakePopen(stdout_text=error_response + "\n")

    with patch("plat_agent.dispatch.sibling.subprocess.Popen", return_value=fake):
        resp = dispatch_sibling_agent(
            repo,
            request,
            timeout_seconds=30,
            log_dir=tmp_path / "logs",
            payload_model=_SamplePayload,
        )

    # Original error preserved — payload_model did not overwrite with schema_violation.
    assert resp.status == "error"
    assert resp.error.code == "missing_input"


# ---------------------------------------------------------------------------
# Artifact path-scope validation
# ---------------------------------------------------------------------------

def test_artifact_path_outside_run_scope_rejected(tmp_path: Path) -> None:
    """A sibling that declares an artifact escaping outputs/<run_id>/ —
    e.g. via `..` traversal — gets a schema_violation."""
    deal_root = _make_deal_root(tmp_path)
    repo = _make_sibling_repo(tmp_path)
    request = _make_request(deal_root)

    bad_artifact = ArtifactRef(
        relative_path="../../etc/test",  # escapes deal_root entirely
        kind="json",
        description="malicious",
    )
    fake = _FakePopen(
        stdout_text=_make_response_json(
            payload={"answer": 1, "label": "x"},
            artifacts=[bad_artifact],
        ) + "\n"
    )

    with patch("plat_agent.dispatch.sibling.subprocess.Popen", return_value=fake):
        resp = dispatch_sibling_agent(
            repo,
            request,
            timeout_seconds=30,
            log_dir=tmp_path / "logs",
        )

    assert resp.status == "error"
    assert resp.error.code == "schema_violation"
    assert "escapes" in resp.error.message


def test_artifact_path_within_scope_accepted(tmp_path: Path) -> None:
    """An artifact declared under outputs/<run_id>/<domain>/ is fine, as long
    as it actually exists on disk."""
    deal_root = _make_deal_root(tmp_path)
    repo = _make_sibling_repo(tmp_path)
    request = _make_request(deal_root)

    domain_dir = deal_root / "outputs" / "run_001" / "underwriting"
    domain_dir.mkdir()
    artifact_file = domain_dir / "summary.json"
    artifact_file.write_text("{}")

    artifact = ArtifactRef(
        relative_path="outputs/run_001/underwriting/summary.json",
        kind="json",
        description="legit",
    )
    fake = _FakePopen(
        stdout_text=_make_response_json(
            payload={"answer": 1, "label": "x"},
            artifacts=[artifact],
        ) + "\n"
    )

    with patch("plat_agent.dispatch.sibling.subprocess.Popen", return_value=fake):
        resp = dispatch_sibling_agent(
            repo,
            request,
            timeout_seconds=30,
            log_dir=tmp_path / "logs",
        )

    assert resp.status == "ok"
    assert len(resp.artifacts) == 1


# ---------------------------------------------------------------------------
# Artifact existence post-dispatch
# ---------------------------------------------------------------------------

def test_artifact_existence_check(tmp_path: Path) -> None:
    """A path-valid artifact that doesn't exist on disk → schema_violation."""
    deal_root = _make_deal_root(tmp_path)
    repo = _make_sibling_repo(tmp_path)
    request = _make_request(deal_root)

    # Path is in-scope but file is never written.
    artifact = ArtifactRef(
        relative_path="outputs/run_001/underwriting/missing.json",
        kind="json",
        description="ghost",
    )
    fake = _FakePopen(
        stdout_text=_make_response_json(
            payload={"answer": 1, "label": "x"},
            artifacts=[artifact],
        ) + "\n"
    )

    with patch("plat_agent.dispatch.sibling.subprocess.Popen", return_value=fake):
        resp = dispatch_sibling_agent(
            repo,
            request,
            timeout_seconds=30,
            log_dir=tmp_path / "logs",
        )

    assert resp.status == "error"
    assert resp.error.code == "schema_violation"
    assert "not found on disk" in resp.error.message
