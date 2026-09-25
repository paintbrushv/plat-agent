"""Tests for the sibling-agent dispatch helper.

These tests mock subprocess.Popen so we can simulate slow siblings, stderr
output, and timeouts without spawning a real `claude` process.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Optional
from unittest.mock import patch

import pytest

from plat_agent.contracts.envelope import BridgeRequestV1, BridgeResponseV1
from plat_agent.dispatch.sibling import (
    COMP_FINDER_BOOTSTRAP_TIMEOUT_SECONDS,
    COMP_FINDER_TIMEOUT_SECONDS,
    DispatchError,
    SiblingRepo,
    _effective_timeout_seconds_for_request,
    _validate_artifact_paths,
    dispatch_sibling_agent,
)


def _make_sibling_repo(
    tmp_path: Path,
    agent_name: str,
    *,
    agent_body: str = "# fake agent\n",
) -> SiblingRepo:
    """Build a fake sibling repo on disk with a minimal agent definition."""
    repo_root = tmp_path / "fake-sibling"
    agents_dir = repo_root / ".claude" / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    (agents_dir / f"{agent_name}.md").write_text(agent_body)
    return SiblingRepo(name="fake-sibling", path=repo_root)


def _make_request(agent_name: str = "fake-agent") -> BridgeRequestV1:
    return BridgeRequestV1(
        deal_slug="test_deal",
        run_id="run_001",
        deal_root="/tmp/fake/runs/deals/test_deal",
        agent_name=agent_name,
        payload={"hello": "world"},
    )


def test_effective_timeout_uses_longer_window_for_comp_finder_bootstrap() -> None:
    request = BridgeRequestV1(
        deal_slug="test_deal",
        run_id="run_001",
        deal_root="/tmp/fake/runs/deals/test_deal",
        agent_name="comp-finder",
        payload={"bootstrap_plan": {"workflow_stage": "new_property"}},
    )
    assert _effective_timeout_seconds_for_request(request, 1800) == COMP_FINDER_BOOTSTRAP_TIMEOUT_SECONDS


def test_effective_timeout_keeps_fast_cap_for_existing_property_comp_refresh() -> None:
    request = BridgeRequestV1(
        deal_slug="test_deal",
        run_id="run_001",
        deal_root="/tmp/fake/runs/deals/test_deal",
        agent_name="comp-finder",
        payload={"bootstrap_plan": {"workflow_stage": "existing_property"}},
    )
    assert _effective_timeout_seconds_for_request(request, 1800) == COMP_FINDER_TIMEOUT_SECONDS


def _make_response_json(agent_name: str = "fake-agent") -> str:
    resp = BridgeResponseV1(
        deal_slug="test_deal",
        run_id="run_001",
        agent_name=agent_name,
        payload={"answer": 42},
    )
    return resp.model_dump_json()


class _FakePopen:
    """Minimal Popen stand-in.

    Writes `stderr_lines` to the pumped stderr pipe and returns `stdout_text`
    from communicate(). When `hang=True`, communicate() raises
    TimeoutExpired the way the real subprocess does.
    """

    def __init__(
        self,
        *,
        stdout_text: str,
        stderr_lines: list[str],
        returncode: int = 0,
        hang: bool = False,
    ) -> None:
        self._stdout_text = stdout_text
        self._stderr_lines = list(stderr_lines)
        self._hang = hang
        self.returncode = returncode

        # Iterating self.stderr in the pump thread should yield the lines
        # then EOF, just like a real text-mode pipe.
        self.stderr = iter(self._stderr_lines)
        self.stdout = None  # not iterated; communicate() returns the text

    def communicate(self, timeout: Optional[float] = None):
        if self._hang:
            # Mimic Popen.communicate's TimeoutExpired with no decoded stdout.
            raise subprocess.TimeoutExpired(cmd="claude", timeout=timeout or 0)
        return self._stdout_text, ""

    def poll(self):
        return None if self._hang else self.returncode

    def kill(self) -> None:
        # After kill, a real Popen.communicate() drains remaining buffers.
        # Our second communicate() call should just return what was buffered.
        self._hang = False


class _RecordingHangPopen(_FakePopen):
    def __init__(self, *, stdout_text: str, stderr_lines: list[str]) -> None:
        super().__init__(stdout_text=stdout_text, stderr_lines=stderr_lines, hang=True)
        self.timeouts: list[float | None] = []

    def communicate(self, timeout: Optional[float] = None):
        self.timeouts.append(timeout)
        return super().communicate(timeout=timeout)


def test_dispatch_streams_stderr_and_parses_response(tmp_path: Path) -> None:
    repo = _make_sibling_repo(tmp_path, "fake-agent")
    request = _make_request()
    log_dir = tmp_path / "logs"

    stderr_lines = [
        "starting fake agent\n",
        "doing some work\n",
        "about to emit response\n",
    ]
    response_json = _make_response_json()
    # Sibling may emit prose before the JSON; the helper extracts the last doc.
    stdout_text = f"progress line 1\nprogress line 2\n{response_json}\n"

    fake = _FakePopen(stdout_text=stdout_text, stderr_lines=stderr_lines)

    with patch("plat_agent.dispatch.sibling.subprocess.Popen", return_value=fake):
        resp = dispatch_sibling_agent(
            repo,
            request,
            timeout_seconds=30,
            log_dir=log_dir,
        )

    assert isinstance(resp, BridgeResponseV1)
    assert resp.deal_slug == "test_deal"
    assert resp.payload == {"answer": 42}

    # Locate the log file (timestamp is in the name).
    log_files = list(log_dir.glob("plat-dispatch-test_deal-fake-agent-*.log"))
    assert len(log_files) == 1, f"expected one log file, got {log_files}"
    log_contents = log_files[0].read_text()
    for line in stderr_lines:
        assert line.strip() in log_contents


def test_dispatch_timeout_preserves_log_and_raises(tmp_path: Path) -> None:
    repo = _make_sibling_repo(tmp_path, "fake-agent")
    request = _make_request()
    log_dir = tmp_path / "logs"

    stderr_lines = [
        "starting up\n",
        "stuck in a loop\n",
    ]
    fake = _FakePopen(stdout_text="", stderr_lines=stderr_lines, hang=True)

    with patch("plat_agent.dispatch.sibling.subprocess.Popen", return_value=fake):
        with pytest.raises(DispatchError) as excinfo:
            dispatch_sibling_agent(
                repo,
                request,
                timeout_seconds=1,
                log_dir=log_dir,
            )

    err = excinfo.value
    assert "timed out" in str(err).lower()
    assert err.log_path is not None
    assert err.log_path.exists()
    log_contents = err.log_path.read_text()
    for line in stderr_lines:
        assert line.strip() in log_contents


def test_dispatch_recovers_from_fresh_persisted_envelope_when_stdout_empty(tmp_path: Path) -> None:
    repo = _make_sibling_repo(tmp_path, "comp-finder")
    deal_root = tmp_path / "deal_root"
    envelope_dir = deal_root / "outputs" / "run_001" / "market_study"
    envelope_dir.mkdir(parents=True, exist_ok=True)
    request = BridgeRequestV1(
        deal_slug="test_deal",
        run_id="run_001",
        deal_root=str(deal_root),
        agent_name="comp-finder",
        payload={"hello": "world"},
    )
    log_dir = tmp_path / "logs"

    persisted = BridgeResponseV1(
        deal_slug="test_deal",
        run_id="run_001",
        agent_name="comp-finder",
        status="needs_analyst_input",
        payload={"answer": 42},
    )
    envelope_path = envelope_dir / "_response_envelope.json"

    class _PersistingFakePopen(_FakePopen):
        def communicate(self, timeout: Optional[float] = None):
            envelope_path.write_text(persisted.model_dump_json(indent=2), encoding="utf-8")
            return super().communicate(timeout=timeout)

    fake = _PersistingFakePopen(stdout_text="", stderr_lines=[])

    with patch("plat_agent.dispatch.sibling.subprocess.Popen", return_value=fake):
        resp = dispatch_sibling_agent(
            repo,
            request,
            timeout_seconds=30,
            log_dir=log_dir,
        )

    assert isinstance(resp, BridgeResponseV1)
    assert resp.status == "needs_analyst_input"
    assert resp.payload == {"answer": 42}


def test_dispatch_rebuilds_comp_finder_envelope_when_stdout_empty(tmp_path: Path) -> None:
    repo = _make_sibling_repo(tmp_path, "comp-finder")
    deal_root = tmp_path / "deal_root"
    request = BridgeRequestV1(
        deal_slug="test_deal",
        run_id="run_001",
        deal_root=str(deal_root),
        agent_name="comp-finder",
        payload={"hello": "world"},
    )
    log_dir = tmp_path / "logs"

    rebuilt = BridgeResponseV1(
        deal_slug="test_deal",
        run_id="run_001",
        agent_name="comp-finder",
        status="ok",
        payload={"answer": 42},
    )
    fake = _FakePopen(stdout_text="", stderr_lines=[])

    with patch("plat_agent.dispatch.sibling.subprocess.Popen", return_value=fake):
        with patch(
            "plat_agent.dispatch.sibling._rebuild_comp_finder_envelope_from_artifacts",
            return_value=rebuilt.model_dump(mode="json"),
        ) as rebuild:
            resp = dispatch_sibling_agent(
                repo,
                request,
                timeout_seconds=30,
                log_dir=log_dir,
            )

    assert isinstance(resp, BridgeResponseV1)
    assert resp.status == "ok"
    assert resp.payload == {"answer": 42}
    rebuild.assert_called_once()


def test_dispatch_rebuilds_comp_finder_envelope_after_timeout(tmp_path: Path) -> None:
    repo = _make_sibling_repo(tmp_path, "comp-finder")
    deal_root = tmp_path / "deal_root"
    request = BridgeRequestV1(
        deal_slug="test_deal",
        run_id="run_001",
        deal_root=str(deal_root),
        agent_name="comp-finder",
        payload={"hello": "world"},
    )
    log_dir = tmp_path / "logs"

    rebuilt = BridgeResponseV1(
        deal_slug="test_deal",
        run_id="run_001",
        agent_name="comp-finder",
        status="needs_analyst_input",
        payload={"answer": 42},
    )
    fake = _RecordingHangPopen(stdout_text="", stderr_lines=["still working\n"])

    with patch("plat_agent.dispatch.sibling.subprocess.Popen", return_value=fake):
        with patch(
            "plat_agent.dispatch.sibling._rebuild_comp_finder_envelope_from_artifacts",
            return_value=rebuilt.model_dump(mode="json"),
        ) as rebuild:
            resp = dispatch_sibling_agent(
                repo,
                request,
                timeout_seconds=999,
                log_dir=log_dir,
            )

    assert isinstance(resp, BridgeResponseV1)
    assert resp.status == "needs_analyst_input"
    assert resp.payload == {"answer": 42}
    assert fake.timeouts == [5]
    rebuild.assert_called_once()


def test_dispatch_short_circuits_when_comp_artifacts_persist_while_child_hangs(tmp_path: Path) -> None:
    repo = _make_sibling_repo(tmp_path, "comp-finder")
    deal_root = tmp_path / "deal_root"
    request = BridgeRequestV1(
        deal_slug="test_deal",
        run_id="run_001",
        deal_root=str(deal_root),
        agent_name="comp-finder",
        payload={"hello": "world"},
    )
    log_dir = tmp_path / "logs"

    persisted = BridgeResponseV1(
        deal_slug="test_deal",
        run_id="run_001",
        agent_name="comp-finder",
        status="needs_analyst_input",
        payload={"answer": 42},
    )
    fake = _RecordingHangPopen(stdout_text="", stderr_lines=["still working\n"])

    with patch("plat_agent.dispatch.sibling.subprocess.Popen", return_value=fake):
        with patch(
            "plat_agent.dispatch.sibling._load_persisted_response_envelope",
            return_value=persisted.model_dump(mode="json"),
        ):
            resp = dispatch_sibling_agent(
                repo,
                request,
                timeout_seconds=999,
                log_dir=log_dir,
            )

    assert isinstance(resp, BridgeResponseV1)
    assert resp.status == "needs_analyst_input"
    assert resp.payload == {"answer": 42}
    assert fake.timeouts == [5]


def test_dispatch_injects_agent_body_inline_via_append_system_prompt(tmp_path: Path) -> None:
    """The dispatch must wire the agent via inline --append-system-prompt.

    The file-variant flag (--append-system-prompt-file) showed inconsistent
    recognition across CLI versions, so the dispatcher passes the agent body
    as the inline value of --append-system-prompt instead. Confirms:
    (a) --append-system-prompt is present with the stripped body as its value;
    (b) --add-dir precedes --append-system-prompt so its variadic arg consumption
        is terminated before the prompt;
    (c) `--` separator precedes the positional prompt;
    (d) the legacy --agent and --append-system-prompt-file flags are gone.
    """
    agent_frontmatter = (
        "---\n"
        "name: fake-agent\n"
        "description: a fake agent for tests\n"
        "tools: Read, Write\n"
        "---\n"
    )
    agent_body_text = "# Fake Agent\n\nYou are a fake agent. Emit a BridgeResponseV1.\n"
    repo = _make_sibling_repo(
        tmp_path,
        "fake-agent",
        agent_body=agent_frontmatter + agent_body_text,
    )
    request = _make_request()
    log_dir = tmp_path / "logs"

    response_json = _make_response_json()
    fake = _FakePopen(stdout_text=response_json + "\n", stderr_lines=[])

    captured: dict[str, object] = {}

    def _capture_popen(cmd, **kwargs):
        captured["cmd"] = list(cmd)
        return fake

    with patch("plat_agent.dispatch.sibling.subprocess.Popen", side_effect=_capture_popen):
        resp = dispatch_sibling_agent(repo, request, timeout_seconds=30, log_dir=log_dir)

    assert isinstance(resp, BridgeResponseV1)

    cmd = captured["cmd"]
    assert "--append-system-prompt" in cmd, f"missing inline flag in cmd: {cmd}"
    assert "--append-system-prompt-file" not in cmd, f"file-variant flag should be gone: {cmd}"
    assert "--agent" not in cmd, f"legacy --agent flag should be gone: {cmd}"
    assert "--add-dir" in cmd, f"missing --add-dir for deal_root grant: {cmd}"
    # --add-dir must precede --append-system-prompt so its variadic arg is terminated.
    assert cmd.index("--add-dir") < cmd.index("--append-system-prompt")
    # `--` separator must precede the prompt (the last positional arg).
    assert "--" in cmd, f"missing -- separator before positional prompt: {cmd}"
    assert cmd.index("--") == len(cmd) - 2, "-- must be immediately before the prompt"

    # The stripped body must be the value of --append-system-prompt.
    body_value = cmd[cmd.index("--append-system-prompt") + 1]
    assert "# Fake Agent" in body_value
    assert "You are a fake agent" in body_value
    # Frontmatter stripped.
    assert "name: fake-agent" not in body_value
    assert "description: a fake agent for tests" not in body_value
    assert "tools: Read, Write" not in body_value
    assert not body_value.lstrip().startswith("---")


def test_dispatch_preserves_agent_tmpfile_on_subprocess_failure(tmp_path: Path) -> None:
    """A non-zero exit must leave the agent prompt tmpfile on disk for diagnostics."""
    repo = _make_sibling_repo(
        tmp_path,
        "fake-agent",
        agent_body="---\nname: fake-agent\n---\n# body kept on failure\n",
    )
    request = _make_request()
    log_dir = tmp_path / "logs"

    fake = _FakePopen(stdout_text="", stderr_lines=["boom\n"], returncode=2)

    captured: dict[str, object] = {}

    def _capture_popen(cmd, **kwargs):
        # The dispatcher still writes a diagnostic tmpfile under log_dir even
        # though the body is now passed inline. Locate it by scanning log_dir
        # for the most-recent .md file.
        files = sorted(log_dir.glob("*.md"))
        captured["tmpfile_path"] = files[-1] if files else None
        return fake

    with patch("plat_agent.dispatch.sibling.subprocess.Popen", side_effect=_capture_popen):
        with pytest.raises(DispatchError):
            dispatch_sibling_agent(repo, request, timeout_seconds=30, log_dir=log_dir)

    tmpfile_path = captured["tmpfile_path"]
    assert tmpfile_path.exists(), (
        f"tmpfile {tmpfile_path} should be preserved on dispatch failure"
    )
    # And it should still contain the stripped body.
    assert "# body kept on failure" in tmpfile_path.read_text(encoding="utf-8")


def test_dispatch_missing_agent_file_raises_before_subprocess(tmp_path: Path) -> None:
    """Sanity check: missing agent definition fails fast with a clear error."""
    repo_root = tmp_path / "no-agents"
    (repo_root / ".claude" / "agents").mkdir(parents=True)
    repo = SiblingRepo(name="no-agents", path=repo_root)
    request = _make_request(agent_name="nonexistent-agent")

    with pytest.raises(DispatchError) as excinfo:
        dispatch_sibling_agent(repo, request, log_dir=tmp_path / "logs")
    assert "not found" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Regression: sibling artifact `kind` normalization (commit 054d19b).
# Some siblings emit `kind="markdown"` instead of the schema literal `"md"`,
# and others emit `kind="yaml"` instead of `"json"`. The dispatcher MUST
# normalize these BEFORE strict envelope validation so productive sibling
# responses don't fail on cosmetic enum differences.
# ---------------------------------------------------------------------------


def _make_response_with_artifact_kind(kind: str) -> str:
    """Build a raw BridgeResponseV1 JSON document where artifacts[0].kind
    is the given string. We construct via dict (not the model) because the
    point of the regression is that the model would reject `kind="markdown"`
    if the dispatcher did NOT normalize first."""
    payload = {
        "contract_version": "v1",
        "deal_slug": "test_deal",
        "run_id": "run_001",
        "agent_name": "fake-agent",
        "status": "ok",
        "payload": {"answer": 42},
        "artifacts": [
            {
                "relative_path": "intake/manifest.md",
                "kind": kind,
                "description": "intake manifest",
            }
        ],
    }
    return json.dumps(payload)


def test_dispatch_normalizes_kind_markdown_to_md(tmp_path: Path) -> None:
    """Regression for 054d19b: sibling emits kind='markdown' → dispatcher
    normalizes to 'md' before BridgeResponseV1 validation."""
    repo = _make_sibling_repo(tmp_path, "fake-agent")
    request = _make_request()
    log_dir = tmp_path / "logs"

    raw_doc = _make_response_with_artifact_kind("markdown")
    fake = _FakePopen(stdout_text=raw_doc + "\n", stderr_lines=[])

    with patch("plat_agent.dispatch.sibling.subprocess.Popen", return_value=fake):
        resp = dispatch_sibling_agent(repo, request, timeout_seconds=30, log_dir=log_dir)

    assert isinstance(resp, BridgeResponseV1)
    assert len(resp.artifacts) == 1
    # 'markdown' is NOT a valid kind in the schema; dispatcher must have
    # normalized to 'md' before validation.
    assert resp.artifacts[0].kind == "md"
    assert resp.artifacts[0].relative_path == "intake/manifest.md"


def test_dispatch_normalizes_kind_yaml_to_json(tmp_path: Path) -> None:
    """Regression for 054d19b: sibling emits kind='yaml' → dispatcher
    normalizes to 'json' (closest valid schema literal). Same for 'yml'."""
    repo = _make_sibling_repo(tmp_path, "fake-agent")
    request = _make_request()
    log_dir = tmp_path / "logs"

    raw_doc = _make_response_with_artifact_kind("yaml")
    fake = _FakePopen(stdout_text=raw_doc + "\n", stderr_lines=[])

    with patch("plat_agent.dispatch.sibling.subprocess.Popen", return_value=fake):
        resp = dispatch_sibling_agent(repo, request, timeout_seconds=30, log_dir=log_dir)

    assert isinstance(resp, BridgeResponseV1)
    assert resp.artifacts[0].kind == "json"


def test_dispatch_normalizes_kind_yml_to_json(tmp_path: Path) -> None:
    """The 'yml' alias is normalized identically to 'yaml'."""
    repo = _make_sibling_repo(tmp_path, "fake-agent")
    request = _make_request()
    log_dir = tmp_path / "logs"

    raw_doc = _make_response_with_artifact_kind("yml")
    fake = _FakePopen(stdout_text=raw_doc + "\n", stderr_lines=[])

    with patch("plat_agent.dispatch.sibling.subprocess.Popen", return_value=fake):
        resp = dispatch_sibling_agent(repo, request, timeout_seconds=30, log_dir=log_dir)

    assert isinstance(resp, BridgeResponseV1)
    assert resp.artifacts[0].kind == "json"


def test_path_relaxation_gates_on_request_not_response(tmp_path: Path) -> None:
    deal_root = tmp_path / "deal"
    deal_root.mkdir()
    (deal_root / "deal_manifest.md").write_text("# manifest\n")

    response = BridgeResponseV1(
        deal_slug="test_deal",
        run_id="run_001",
        agent_name="deal-intake",
        payload={"answer": 42},
        artifacts=[
            {
                "relative_path": "deal_manifest.md",
                "kind": "md",
                "description": "deal-root artifact",
            }
        ],
    )

    with pytest.raises(ValueError, match="escapes outputs/run_001/ scope"):
        _validate_artifact_paths(
            response,
            str(deal_root),
            "run_001",
            "comp-finder",
        )
