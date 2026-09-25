"""Client for multifamily-underwriting engine.

v2: session-scoped MCP client. Opens one stdio subprocess + one ClientSession
on first call, reuses them for all subsequent calls in the same process.
The previous per-call-spawn approach raced on the second call and returned
empty stdout intermittently after multiple prior anyio.run() calls; reusing
the session eliminates that and also removes ~1s cold-start per call.

Threading model: a daemon worker thread owns the event loop and the MCP
session. Sync methods submit requests via a queue and block on a Future.

Two access modes, split by payload weight:

MCP (lightweight, session-scoped):
  - validate_deal_inputs → validation report
  - run_deal_summary → key metrics only (IRR, EM, DSCR, cap rates)
  - check_deal_feasibility → pass/fail gate check

Direct (heavyweight):
  - run_full → complete cashflow, monthly detail, renovation tracking
  - Imports engine.api.handle_run_deal directly (requires engine on PYTHONPATH)

Configuration:
  - MCP server command: UNDERWRITING_MCP_CMD env var (default: python server.py)
  - Engine path: UNDERWRITING_ENGINE_PATH env var (default: ../multifamily-underwriting)

The MCP path is always available. The direct path requires the
multifamily-underwriting repo to be locally accessible.
"""

import concurrent.futures
import json
import os
import queue
import sys
import threading
from contextlib import AsyncExitStack
from pathlib import Path

import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def _default_mcp_command() -> list[str]:
    env_cmd = os.environ.get("UNDERWRITING_MCP_CMD")
    if env_cmd:
        return env_cmd.split()
    # Default: use the underwriting repo's own venv python to run server.py
    engine_path = _engine_path()
    venv_python = engine_path / ".venv" / "bin" / "python"
    python = str(venv_python) if venv_python.exists() else sys.executable
    return [python, str(engine_path / "server.py")]


def _engine_path() -> Path:
    env_path = os.environ.get("UNDERWRITING_ENGINE_PATH")
    if env_path:
        return Path(env_path)
    # Default: sibling directory
    return Path(__file__).resolve().parents[3] / "multifamily-underwriting"


class UnderwritingClient:
    """Client for multifamily-underwriting with MCP + direct modes.

    MCP mode: lightweight summary metrics via a session-scoped tool calls.
    A daemon worker thread owns the event loop and the single long-lived MCP
    session. Sync methods submit requests via a queue and block on a Future.

    Direct mode: full cashflow engine via Python import.
    """

    def __init__(self, mcp_command: list[str] | None = None, engine_path: str | None = None):
        cmd = mcp_command or _default_mcp_command()
        self._engine_path = Path(engine_path) if engine_path else _engine_path()
        self._server_params = StdioServerParameters(
            command=cmd[0],
            args=cmd[1:],
            cwd=str(self._engine_path),
        )

        # Worker-thread state — lazily initialized on first MCP call
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._requests: queue.Queue | None = None
        self._ready_event: threading.Event | None = None
        self._stop_sentinel = object()  # sentinel placed on queue for shutdown

    # ------------------------------------------------------------------
    # MCP tools (lightweight)
    # ------------------------------------------------------------------

    def validate(self, inputs: dict) -> dict:
        """Validate deal inputs via MCP. Returns validation report."""
        return self._submit("validate_deal_inputs", {"inputs": inputs})

    def run_summary(self, inputs: dict) -> dict:
        """Run engine via MCP, return only key metrics."""
        return self._submit("run_deal_summary", {
            "inputs": inputs,
            "include_renovation_summary": True,
        })

    def check_feasibility(self, inputs: dict, **gates) -> dict:
        """Quick feasibility gate check via MCP."""
        args = {"inputs": inputs}
        args.update(gates)
        return self._submit("check_deal_feasibility", args)

    # ------------------------------------------------------------------
    # Direct engine call (heavyweight)
    # ------------------------------------------------------------------

    def run_full(self, inputs: dict, include_cashflow: bool = True, scenarios: bool = False) -> dict:
        """Run the full underwriting engine via direct Python import.

        Returns the complete results dict including monthly cashflow,
        revenue/opex/capex breakdowns, debt schedules, and renovation
        tracking. This is the heavyweight call for full deal analysis.

        Requires multifamily-underwriting to be on PYTHONPATH or at
        the configured engine_path.
        """
        handle_run_deal = self._import_engine_api()
        request = {
            "inputs": inputs,
            "options": {
                "include_cashflow": include_cashflow,
                "scenarios": scenarios,
            },
        }
        return handle_run_deal(request)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Shut down the worker thread if running. Idempotent."""
        with self._lock:
            if self._thread is None:
                return
            assert self._requests is not None
            self._requests.put(self._stop_sentinel)
            self._thread.join(timeout=10.0)
            self._thread = None
            self._requests = None
            self._ready_event = None

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _submit(self, tool_name: str, arguments: dict) -> dict:
        """Submit a request to the worker thread and block until result."""
        self._ensure_worker()
        fut: concurrent.futures.Future = concurrent.futures.Future()
        assert self._requests is not None
        self._requests.put((tool_name, arguments, fut))
        return fut.result()  # blocks until worker sets result or exception

    def _ensure_worker(self) -> None:
        """Start the worker thread if not already running (lazy init)."""
        with self._lock:
            if self._thread is not None:
                return

            req_queue: queue.Queue = queue.Queue()
            ready_event = threading.Event()
            startup_exc: dict = {}

            def _run() -> None:
                try:
                    anyio.run(self._worker_main, req_queue, ready_event, startup_exc)
                except BaseException as e:  # noqa: BLE001
                    startup_exc.setdefault("exc", e)
                    ready_event.set()

            t = threading.Thread(
                target=_run,
                daemon=True,
                name="UnderwritingClient-worker",
            )
            t.start()

            # Wait for session to open (or fail). Cold start is ~1-2s.
            if not ready_event.wait(timeout=30.0):
                raise TimeoutError("UnderwritingClient worker failed to start in 30s")
            if "exc" in startup_exc:
                raise RuntimeError(
                    f"worker startup failed: {startup_exc['exc']}"
                ) from startup_exc["exc"]

            self._thread = t
            self._requests = req_queue
            self._ready_event = ready_event

    async def _worker_main(
        self,
        requests: queue.Queue,
        ready_event: threading.Event,
        startup_exc: dict,
    ) -> None:
        """Runs inside the worker thread's event loop.

        Opens the MCP session once, sets ready_event, then services requests
        from the queue until the stop sentinel arrives.
        """
        try:
            async with AsyncExitStack() as stack:
                read, write = await stack.enter_async_context(
                    stdio_client(self._server_params)
                )
                session = await stack.enter_async_context(ClientSession(read, write))
                await session.initialize()
                ready_event.set()  # signal that we're ready for requests

                while True:
                    # queue.Queue.get is blocking sync — run in a thread to
                    # avoid starving the event loop.
                    item = await anyio.to_thread.run_sync(requests.get)

                    if item is self._stop_sentinel:
                        return

                    tool_name, arguments, fut = item
                    try:
                        result = await session.call_tool(tool_name, arguments)
                        raw = result.content[0].text if result.content else ""
                        if not raw:
                            fut.set_result({"status": "error", "error": "engine returned empty response"})
                        else:
                            try:
                                fut.set_result(json.loads(raw))
                            except json.JSONDecodeError:
                                # Engine returned plain-text error (e.g., validation failure)
                                fut.set_result({"status": "error", "error": raw.strip()})
                    except BaseException as e:  # noqa: BLE001
                        fut.set_exception(e)
        except BaseException as e:  # noqa: BLE001 — catch startup failures
            startup_exc.setdefault("exc", e)
            ready_event.set()

    def _import_engine_api(self):
        """Lazily import handle_run_deal from the engine repo."""
        engine_dir = str(self._engine_path)
        if engine_dir not in sys.path:
            sys.path.insert(0, engine_dir)
        from engine.api import handle_run_deal
        return handle_run_deal
