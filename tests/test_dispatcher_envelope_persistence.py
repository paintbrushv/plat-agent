"""Wave 4 Task 4.2 — dispatcher persists the full BridgeResponseV1 envelope.

Per Stage 6 M-5 (audit 2026-04-26): the deterministic verdict / sanity_flags
computed by sibling agents lived only on stdout, captured by the dispatcher
to its own log dir, NOT to the deal artifact tree. Memo composers walking
`outputs/<run_id>/` therefore had no machine-readable feed and either
(a) read sibling-shaped `_provenance.json` (non-uniform across siblings) or
(b) hallucinated bands from training-data memory.

This test file covers `_persist_response_envelope` and the dispatcher's
post-validation persistence path:
  - Successful dispatch → outputs/<run_id>/<domain>/_response_envelope.json
    is written with the full BridgeResponseV1 contents.
  - Atomic write: a mid-write interruption leaves NO partial file, only
    (at most) a `.tmp.<pid>` cleaned up on failure.
  - Persistence failure is non-fatal: the dispatcher still returns the
    response (with a warning logged).
  - Missing target directory is created on first dispatch.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional
from unittest.mock import patch

import pytest

from plat_agent.contracts.envelope import (
    ArtifactRef,
    BridgeRequestV1,
    BridgeResponseV1,
)
from plat_agent.dispatch.sibling import (
    SiblingRepo,
    _persist_response_envelope,
    dispatch_sibling_agent,
)


# ---------------------------------------------------------------------------
# Fixtures (mirrors test_dispatch_payload_validation.py to stay consistent)
# ---------------------------------------------------------------------------

def _make_sibling_repo(tmp_path: Path, agent_name: str = "underwriting-runner") -> SiblingRepo:
    repo_root = tmp_path / "fake-sibling"
    agents_dir = repo_root / ".claude" / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    (agents_dir / f"{agent_name}.md").write_text("# fake agent\n")
    return SiblingRepo(name="fake-sibling", path=repo_root)


def _make_request(
    deal_root: Path,
    agent_name: str = "underwriting-runner",
    run_id: str = "run_001",
) -> BridgeRequestV1:
    return BridgeRequestV1(
        deal_slug="test_deal",
        run_id=run_id,
        deal_root=str(deal_root),
        agent_name=agent_name,
        payload={"hello": "world"},
    )


def _make_response(
    *,
    artifacts: Optional[list[ArtifactRef]] = None,
    payload: Optional[dict] = None,
    status: str = "ok",
    agent_name: str = "underwriting-runner",
    run_id: str = "run_001",
) -> BridgeResponseV1:
    return BridgeResponseV1(
        status=status,
        deal_slug="test_deal",
        run_id=run_id,
        agent_name=agent_name,
        payload=payload or {"metrics": {"levered_irr": 0.18}},
        artifacts=artifacts or [],
        sanity_flags=["cap_rate_outside_4_7_band"],
    )


def _make_response_json(**kwargs) -> str:
    return _make_response(**kwargs).model_dump_json()


class _FakePopen:
    def __init__(self, *, stdout_text: str, returncode: int = 0) -> None:
        self._stdout_text = stdout_text
        self.returncode = returncode
        self.stderr = iter([])

    def communicate(self, timeout=None):
        return self._stdout_text, ""

    def kill(self):
        pass


def _make_deal_root(tmp_path: Path, run_id: str = "run_001") -> Path:
    deal_root = tmp_path / "deal"
    (deal_root / "outputs" / run_id).mkdir(parents=True)
    return deal_root


# ---------------------------------------------------------------------------
# 1. Envelope persisted after successful dispatch
# ---------------------------------------------------------------------------

def test_envelope_persisted_after_successful_dispatch(tmp_path: Path) -> None:
    """A clean dispatch writes <deal_root>/outputs/<run_id>/<domain>/_response_envelope.json
    matching the in-memory BridgeResponseV1. Domain is inferred from the first
    artifact's path — `outputs/run_001/underwriting/...` → `underwriting`."""
    deal_root = _make_deal_root(tmp_path)
    repo = _make_sibling_repo(tmp_path)
    request = _make_request(deal_root)

    domain_dir = deal_root / "outputs" / "run_001" / "underwriting"
    domain_dir.mkdir()
    artifact_file = domain_dir / "deal_summary.json"
    artifact_file.write_text("{}")

    artifact = ArtifactRef(
        relative_path="outputs/run_001/underwriting/deal_summary.json",
        kind="json",
        description="engine output",
    )
    fake = _FakePopen(stdout_text=_make_response_json(artifacts=[artifact]) + "\n")

    with patch("plat_agent.dispatch.sibling.subprocess.Popen", return_value=fake):
        resp = dispatch_sibling_agent(
            repo,
            request,
            timeout_seconds=30,
            log_dir=tmp_path / "logs",
        )

    assert resp.status == "ok"
    envelope_path = domain_dir / "_response_envelope.json"
    assert envelope_path.exists(), f"envelope not written at {envelope_path}"

    persisted = json.loads(envelope_path.read_text())
    assert persisted["agent_name"] == "underwriting-runner"
    assert persisted["status"] == "ok"
    assert persisted["deal_slug"] == "test_deal"
    assert persisted["run_id"] == "run_001"
    assert persisted["sanity_flags"] == ["cap_rate_outside_4_7_band"]
    # The persisted JSON must round-trip through BridgeResponseV1 — that's
    # the typed contract downstream consumers will rely on.
    rebuilt = BridgeResponseV1.model_validate(persisted)
    assert rebuilt.status == "ok"
    assert rebuilt.payload == {"metrics": {"levered_irr": 0.18}}


def test_envelope_directory_created_if_missing(tmp_path: Path) -> None:
    """When `outputs/<run_id>/<domain>/` doesn't exist (e.g. first dispatch
    of a brand-new run, no artifacts yet), the dispatcher creates it before
    writing the envelope. Uses the agent_name → domain fallback since no
    artifacts are declared."""
    deal_root = tmp_path / "fresh_deal"  # NB: does not pre-create outputs/
    deal_root.mkdir()
    repo = _make_sibling_repo(tmp_path, agent_name="cost-bridge-analyst")
    request = _make_request(deal_root, agent_name="cost-bridge-analyst")

    # No artifacts — domain inferred from agent_name -> 'costmodel'.
    fake = _FakePopen(
        stdout_text=_make_response_json(
            artifacts=[],
            agent_name="cost-bridge-analyst",
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
    envelope_path = deal_root / "outputs" / "run_001" / "costmodel" / "_response_envelope.json"
    assert envelope_path.exists(), (
        f"directory was not created and envelope was not persisted at {envelope_path}"
    )
    persisted = json.loads(envelope_path.read_text())
    assert persisted["agent_name"] == "cost-bridge-analyst"


# ---------------------------------------------------------------------------
# 2. Atomic write — no partial file on interrupted write
# ---------------------------------------------------------------------------

def test_envelope_persistence_atomic(tmp_path: Path) -> None:
    """Simulate a write interruption mid-write. The atomic temp+rename pattern
    must leave NO `_response_envelope.json` (because rename never happened),
    and best-effort cleanup must remove the `.tmp.<pid>` litter."""
    deal_root = _make_deal_root(tmp_path)
    response = _make_response(agent_name="underwriting-runner")

    # Patch Path.write_text on the temp file to raise mid-write.
    real_write_text = Path.write_text

    def _faulty_write_text(self, content, *args, **kwargs):
        if ".tmp." in self.name:
            raise OSError("simulated write interruption")
        return real_write_text(self, content, *args, **kwargs)

    with patch.object(Path, "write_text", _faulty_write_text):
        # Persistence is best-effort — exception is swallowed and logged.
        # Returning None signals failure to the dispatcher.
        result = _persist_response_envelope(
            response,
            str(deal_root),
            "run_001",
            domain="underwriting",
        )

    assert result is None, "expected None return on interrupted write"

    domain_dir = deal_root / "outputs" / "run_001" / "underwriting"
    envelope_path = domain_dir / "_response_envelope.json"
    assert not envelope_path.exists(), "no _response_envelope.json should exist after interrupted write"

    # No leftover `.tmp.<pid>` files either — best-effort cleanup runs.
    if domain_dir.exists():
        leftovers = [p for p in domain_dir.iterdir() if ".tmp." in p.name]
        assert leftovers == [], f"temp file litter left behind: {leftovers}"


# ---------------------------------------------------------------------------
# 3. Persistence failure is non-fatal — dispatch still returns the response
# ---------------------------------------------------------------------------

def test_envelope_persistence_failure_not_fatal(tmp_path: Path, capsys) -> None:
    """If persistence fails (disk full, perms, etc.), the dispatcher still
    returns the parsed BridgeResponseV1 to the caller — a warning is logged
    to stderr but the exception is swallowed. The response is the source of
    truth; the on-disk envelope is best-effort.
    """
    deal_root = _make_deal_root(tmp_path)
    repo = _make_sibling_repo(tmp_path)
    request = _make_request(deal_root)

    domain_dir = deal_root / "outputs" / "run_001" / "underwriting"
    domain_dir.mkdir()
    artifact_file = domain_dir / "deal_summary.json"
    artifact_file.write_text("{}")
    artifact = ArtifactRef(
        relative_path="outputs/run_001/underwriting/deal_summary.json",
        kind="json",
        description="engine output",
    )

    fake = _FakePopen(stdout_text=_make_response_json(artifacts=[artifact]) + "\n")

    # Force the atomic write to fail unconditionally — simulates disk full,
    # read-only mount, etc.
    def _always_fail(self, content, *args, **kwargs):
        raise OSError("disk full simulated")

    with patch("plat_agent.dispatch.sibling.subprocess.Popen", return_value=fake), \
         patch.object(Path, "write_text", _always_fail):
        resp = dispatch_sibling_agent(
            repo,
            request,
            timeout_seconds=30,
            log_dir=tmp_path / "logs",
        )

    # Dispatch did NOT raise — caller still gets the structured response.
    assert resp.status == "ok"
    assert resp.agent_name == "underwriting-runner"
    assert resp.payload == {"metrics": {"levered_irr": 0.18}}

    # Envelope file must NOT exist (write failed) but dispatch succeeded.
    envelope_path = domain_dir / "_response_envelope.json"
    assert not envelope_path.exists()

    # A warning must have been printed to stderr.
    captured = capsys.readouterr()
    assert "WARNING" in captured.err
    assert "envelope" in captured.err.lower() or "persist" in captured.err.lower()


# ---------------------------------------------------------------------------
# 4. Domain inference fallback chain
# ---------------------------------------------------------------------------

def test_envelope_persistence_uses_artifact_path_for_domain(tmp_path: Path) -> None:
    """Domain inference precedence: artifact-path > agent_name fallback.
    A response from an agent with no entry in _AGENT_TO_DOMAIN should still
    persist correctly when its artifacts declare a clear `outputs/<run>/<domain>/...`
    layout."""
    deal_root = _make_deal_root(tmp_path)
    domain_dir = deal_root / "outputs" / "run_001" / "custom_domain"
    domain_dir.mkdir()
    art_file = domain_dir / "result.json"
    art_file.write_text("{}")

    response = _make_response(
        agent_name="not-in-agent-map",
        artifacts=[
            ArtifactRef(
                relative_path="outputs/run_001/custom_domain/result.json",
                kind="json",
                description="custom",
            )
        ],
    )

    persisted = _persist_response_envelope(
        response, str(deal_root), "run_001", domain=None
    )
    assert persisted is not None
    assert persisted == domain_dir / "_response_envelope.json"
    assert persisted.exists()


def test_envelope_persistence_skipped_when_domain_unresolvable(
    tmp_path: Path, capsys
) -> None:
    """If neither artifacts nor agent_name reveal a domain, persistence is
    skipped (with a warning) — we don't invent a default subdirectory under
    `outputs/<run_id>/`."""
    deal_root = _make_deal_root(tmp_path)
    response = _make_response(agent_name="nameless-agent", artifacts=[])

    result = _persist_response_envelope(response, str(deal_root), "run_001")
    assert result is None

    captured = capsys.readouterr()
    assert "WARNING" in captured.err
    assert "domain" in captured.err.lower()
