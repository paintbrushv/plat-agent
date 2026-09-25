"""Default subprocess command for plat-costmodel MCP server."""

import os
import sys

from plat_agent.costmodel_client import (
    PLAT_COSTMODEL_CMD_ENV,
    PLAT_COSTMODEL_PATH_ENV,
    _costmodel_path,
    _default_server_command,
)


def test_default_command_does_not_use_bare_python(monkeypatch, tmp_path):
    """Bare ``python`` is missing on macOS (only ``python3`` is on PATH).

    The default server command must use ``sys.executable`` or an explicit
    ``python3`` so the costmodel subprocess starts successfully. Regression
    surfaced by the plat-agent Phase 7 smoke test.
    """
    monkeypatch.delenv(PLAT_COSTMODEL_CMD_ENV, raising=False)
    # Force the sibling-venv path to NOT exist so we exercise the fallback.
    monkeypatch.setenv(PLAT_COSTMODEL_PATH_ENV, str(tmp_path / "missing-costmodel"))
    cmd = _default_server_command()
    assert cmd[0] != "python", (
        "Default interpreter should be sys.executable (or python3), not bare 'python'"
    )


def test_default_command_uses_sys_executable_when_venv_absent(monkeypatch, tmp_path):
    monkeypatch.delenv(PLAT_COSTMODEL_CMD_ENV, raising=False)
    monkeypatch.setenv(PLAT_COSTMODEL_PATH_ENV, str(tmp_path / "missing-costmodel"))
    cmd = _default_server_command()
    assert cmd == [sys.executable, "-m", "plat_costmodel.server"]


def test_env_override_wins(monkeypatch):
    monkeypatch.setenv(
        PLAT_COSTMODEL_CMD_ENV, "/some/venv/bin/python -m plat_costmodel.server"
    )
    cmd = _default_server_command()
    assert cmd == ["/some/venv/bin/python", "-m", "plat_costmodel.server"]


def test_default_command_prefers_sibling_venv_when_present(tmp_path, monkeypatch):
    """When ../plat-costmodel/.venv/bin/python exists, use it instead of sys.executable."""
    fake_costmodel = tmp_path / "plat-costmodel"
    fake_venv_python = fake_costmodel / ".venv" / "bin" / "python"
    fake_venv_python.parent.mkdir(parents=True)
    fake_venv_python.touch()
    fake_venv_python.chmod(0o755)
    monkeypatch.setenv(PLAT_COSTMODEL_PATH_ENV, str(fake_costmodel))
    monkeypatch.delenv(PLAT_COSTMODEL_CMD_ENV, raising=False)
    cmd = _default_server_command()
    assert cmd[0] == str(fake_venv_python)
    assert cmd[1:] == ["-m", "plat_costmodel.server"]


def test_default_command_falls_back_to_sys_executable_when_venv_absent(tmp_path, monkeypatch):
    """When sibling .venv doesn't exist, use sys.executable (preserves sys.executable fix)."""
    fake_costmodel = tmp_path / "plat-costmodel-missing"
    monkeypatch.setenv(PLAT_COSTMODEL_PATH_ENV, str(fake_costmodel))
    monkeypatch.delenv(PLAT_COSTMODEL_CMD_ENV, raising=False)
    cmd = _default_server_command()
    assert cmd[0] == sys.executable
    assert cmd[1:] == ["-m", "plat_costmodel.server"]


def test_PLAT_COSTMODEL_CMD_still_takes_precedence(tmp_path, monkeypatch):
    """Explicit env override beats sibling-venv detection."""
    fake_costmodel = tmp_path / "plat-costmodel"
    (fake_costmodel / ".venv" / "bin").mkdir(parents=True)
    (fake_costmodel / ".venv" / "bin" / "python").touch()
    monkeypatch.setenv(PLAT_COSTMODEL_PATH_ENV, str(fake_costmodel))
    monkeypatch.setenv(PLAT_COSTMODEL_CMD_ENV, "/explicit/python -m plat_costmodel.server")
    cmd = _default_server_command()
    assert cmd[0] == "/explicit/python"


def test_costmodel_path_default_is_sibling_directory():
    """When no env var is set, default to ../plat-costmodel sibling."""
    saved = os.environ.pop(PLAT_COSTMODEL_PATH_ENV, None)
    try:
        path = _costmodel_path()
        assert path.name == "plat-costmodel"
    finally:
        if saved is not None:
            os.environ[PLAT_COSTMODEL_PATH_ENV] = saved


# ---------------------------------------------------------------------------
# TaskGroup unwrap — the fix for today's smoke-test failure
# ---------------------------------------------------------------------------

def test_unwrap_exception_group_single_inner():
    """Single-exception groups (anyio's common shape) unwrap to the inner."""
    from plat_agent.costmodel_client import _unwrap_exception_group

    inner = ValueError("real error")
    eg = ExceptionGroup("wrapped", [inner])
    assert _unwrap_exception_group(eg) is inner


def test_unwrap_exception_group_nested():
    """Nested groups unwrap recursively to the leftmost leaf."""
    from plat_agent.costmodel_client import _unwrap_exception_group

    leaf = RuntimeError("the actual error")
    inner_eg = ExceptionGroup("inner", [leaf])
    outer_eg = ExceptionGroup("outer", [inner_eg])
    assert _unwrap_exception_group(outer_eg) is leaf


def test_unwrap_exception_group_handles_single_placeholder():
    """ExceptionGroup constructor rejects an empty exception tuple, so the
    'empty group' edge case is unrepresentable at runtime. This test
    confirms the helper handles the smallest legal group (single inner
    exception) — same shape as test_unwrap_exception_group_single_inner
    above but with a different inner type, as a quick sanity check that
    the args-extraction path works for arbitrary exception subclasses."""
    from plat_agent.costmodel_client import _unwrap_exception_group

    eg = ExceptionGroup("placeholder", [ValueError("placeholder")])
    assert _unwrap_exception_group(eg).args == ("placeholder",)


def test_async_call_tool_unwraps_taskgroup_error(monkeypatch):
    """Regression for today's smoke test: an exception raised inside the
    MCP TaskGroup must surface as the underlying error, not as the generic
    'unhandled errors in a TaskGroup (1 sub-exception)' string.

    Patches stdio_client (the source of the TaskGroup) to raise an
    ExceptionGroup, then runs the real _async_call_tool and asserts the
    inner exception comes through.
    """
    import asyncio
    from plat_agent.costmodel_client import CostModelClient

    class FakeAsyncCM:
        async def __aenter__(self):
            raise ExceptionGroup("simulated mcp failure", [
                ValueError("validation_error: cohort_index out of range"),
            ])

        async def __aexit__(self, *exc_info):
            return False

    def fake_stdio_client(_params):
        return FakeAsyncCM()

    monkeypatch.setattr(
        "plat_agent.costmodel_client.stdio_client",
        fake_stdio_client,
    )
    client = CostModelClient(server_command=["irrelevant"])

    try:
        asyncio.run(client._async_call_tool("any_tool", {}))
    except ValueError as exc:
        assert "cohort_index out of range" in str(exc)
    except BaseExceptionGroup:
        raise AssertionError("ExceptionGroup leaked through; unwrap failed")
    else:
        raise AssertionError("expected an exception to be raised")
