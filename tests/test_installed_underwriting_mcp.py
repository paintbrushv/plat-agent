"""Default underwriting MCP dispatch must use the reviewed installed wheel."""

import sys
from types import SimpleNamespace

import pytest

from plat_agent.underwriting_client import UnderwritingClient, _default_mcp_command


def test_default_command_needs_no_sibling_cwd(monkeypatch):
    monkeypatch.delenv("UNDERWRITING_MCP_CMD", raising=False)
    monkeypatch.delenv("UNDERWRITING_ENGINE_PATH", raising=False)
    assert _default_mcp_command() == [sys.executable, "-m", "engine.mcp_server"]
    client = UnderwritingClient()
    assert client._server_params.cwd is None
    assert client._server_params.args == ["-m", "engine.mcp_server"]


def test_default_refuses_unreviewed_package_before_spawning(monkeypatch):
    monkeypatch.delenv("UNDERWRITING_MCP_CMD", raising=False)
    monkeypatch.delenv("UNDERWRITING_ENGINE_PATH", raising=False)
    client = UnderwritingClient()

    def mismatch():
        raise RuntimeError("producer content mismatch")

    monkeypatch.setattr(
        "plat_agent.underwriting_client.UNDERWRITING_MCP_V2",
        SimpleNamespace(verify=mismatch),
    )
    with pytest.raises(RuntimeError, match="producer content mismatch"):
        client.validate({})
    assert client._thread is None


def test_explicit_host_command_keeps_explicit_working_directory(monkeypatch, tmp_path):
    monkeypatch.delenv("UNDERWRITING_MCP_CMD", raising=False)
    client = UnderwritingClient(mcp_command=[sys.executable, "-m", "custom.server"], engine_path=str(tmp_path))
    assert client._server_params.cwd == str(tmp_path)
    assert client._custom_mcp_command


def test_quoted_command_override_is_one_executable(monkeypatch):
    monkeypatch.setenv("UNDERWRITING_MCP_CMD", '"/path with spaces/python" -m engine.mcp_server')
    assert _default_mcp_command() == ["/path with spaces/python", "-m", "engine.mcp_server"]


def test_empty_explicit_command_is_refused():
    with pytest.raises(ValueError, match="cannot be empty"):
        UnderwritingClient(mcp_command=[])
