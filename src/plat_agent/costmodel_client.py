"""MCP client wrapper for plat-costmodel.

Plat calls plat-costmodel as a subprocess MCP server, not via Python import.
This module provides a synchronous interface over the async MCP client.

Usage:
    client = CostModelClient()
    result = client.call_tool("estimate_property_from_model", {"property_id": ...})

Configuration:
    By default, spawns `<plat-costmodel-venv>/bin/python -m plat_costmodel.server`
    as a subprocess. Resolution order:
      1. PLAT_COSTMODEL_CMD env var (full command override)
      2. <PLAT_COSTMODEL_PATH or ../plat-costmodel sibling>/.venv/bin/python
      3. sys.executable (assumes plat_costmodel is installed in current env)

    PLAT_COSTMODEL_PATH env var: path to plat-costmodel repo (default: ../plat-costmodel)
    PLAT_COSTMODEL_CMD env var: full command override (e.g., for custom envs)

    PLAT_COSTMODEL_CMD example:
        export PLAT_COSTMODEL_CMD="/path/to/.venv/bin/python -m plat_costmodel.server"
"""

import json
import os
import sys
from pathlib import Path

import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


PLAT_COSTMODEL_PATH_ENV = "PLAT_COSTMODEL_PATH"
PLAT_COSTMODEL_CMD_ENV = "PLAT_COSTMODEL_CMD"


def _unwrap_exception_group(eg: BaseExceptionGroup) -> BaseException:
    """Recursively descend into nested ExceptionGroups to find the root cause.

    anyio TaskGroups can produce nested groups when multiple tasks fail; we
    surface the leftmost leaf, which is sufficient for the common single-
    exception case that motivated this unwrap.
    """
    current: BaseException = eg
    while isinstance(current, BaseExceptionGroup) and current.exceptions:
        current = current.exceptions[0]
    return current


def _costmodel_path() -> Path:
    """Resolve the plat-costmodel sibling repo path."""
    env_path = os.environ.get(PLAT_COSTMODEL_PATH_ENV)
    if env_path:
        return Path(env_path).expanduser().resolve()
    # Default: sibling directory next to plat-agent
    return Path(__file__).resolve().parents[3] / "plat-costmodel"


def _default_server_command() -> list[str]:
    """Build the subprocess command, preferring plat-costmodel's own .venv if present."""
    env_cmd = os.environ.get(PLAT_COSTMODEL_CMD_ENV)
    if env_cmd:
        return env_cmd.split()
    costmodel_path = _costmodel_path()
    venv_python = costmodel_path / ".venv" / "bin" / "python"
    # Prefer the sibling repo's venv (which has plat_costmodel installed).
    # Fall back to sys.executable so we run with the same interpreter that
    # imported us, avoiding ENOENT on macOS where bare `python` is not on PATH.
    python = str(venv_python) if venv_python.exists() else sys.executable
    return [python, "-m", "plat_costmodel.server"]


class CostModelClient:
    """Synchronous MCP client for plat-costmodel.

    Each call_tool() invocation opens the server subprocess, calls the tool,
    and closes the subprocess. This is intentionally simple for v0 — connection
    pooling can be added later if latency becomes a problem.
    """

    def __init__(
        self,
        server_command: list[str] | None = None,
        costmodel_path: str | None = None,
    ):
        self._costmodel_path = (
            Path(costmodel_path).expanduser().resolve()
            if costmodel_path
            else _costmodel_path()
        )
        cmd = server_command or _default_server_command()
        self._server_params = StdioServerParameters(
            command=cmd[0],
            args=cmd[1:],
            cwd=str(self._costmodel_path) if self._costmodel_path.exists() else None,
        )

    def call_tool(self, tool_name: str, arguments: dict) -> dict:
        """Call a plat-costmodel MCP tool and return the parsed result dict."""
        return anyio.run(self._async_call_tool, tool_name, arguments)

    async def _async_call_tool(self, tool_name: str, arguments: dict) -> dict:
        try:
            async with stdio_client(self._server_params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.call_tool(tool_name, arguments)
                    # MCP returns content as a list of TextContent items
                    raw = result.content[0].text if result.content else "{}"
                    return json.loads(raw)
        except BaseExceptionGroup as eg:
            # anyio's TaskGroup wraps inner exceptions in an ExceptionGroup.
            # Without this unwrap, callers see the generic
            # "unhandled errors in a TaskGroup (1 sub-exception)" string and
            # lose the actual underlying error (e.g. a Pydantic ValidationError
            # from the costmodel server). Surface the root cause so the agent
            # can report it usefully. ``from eg`` (not ``from None``)
            # preserves the original TaskGroup traceback as __cause__ for
            # debugging — production logs see the actual call stack from the
            # subprocess + anyio layers, not just the leaf message.
            raise _unwrap_exception_group(eg) from eg

    async def async_call_tool(self, tool_name: str, arguments: dict) -> dict:
        """Async version for callers already inside an event loop."""
        return await self._async_call_tool(tool_name, arguments)
