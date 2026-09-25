"""Agent-SDK @tool wrappers around the costmodel + underwriting clients.

Each handler returns an MCP-style payload (`{"content": [{"type": "text", ...}]}`).
Numeric metrics are serialized as space-separated `key=value` pairs so the
downstream `compare_scenarios` tool can parse them with `_parse_kv` without
either side needing to exchange dicts.
"""

import asyncio
import functools
import json
import os
from pathlib import Path
from typing import Any

from claude_agent_sdk import tool

from plat_agent.costmodel_client import CostModelClient
from plat_agent.underwriting_client import UnderwritingClient


# ---------------------------------------------------------------------------
# Lazy module-level client singletons.
#
# UnderwritingClient lazily spins up a daemon worker thread + long-lived MCP
# session on first call (see its docstring — ~1-2s cold start). Constructing
# a fresh client on every @tool invocation would re-pay that cost every time
# and largely defeat the asyncio.to_thread offload below. Cache one instance
# per process.
#
# CostModelClient is cheaper (each call_tool spawns a fresh subprocess), but
# the construction itself is also pure config; caching keeps the access
# pattern symmetric with underwriting and avoids surprising per-call
# allocation under high tool-call volume.
#
# Tests that need a clean instance call `_*.cache_clear()`.
# ---------------------------------------------------------------------------

@functools.lru_cache(maxsize=1)
def _cost_model_client() -> CostModelClient:
    """Lazily construct + cache the CostModelClient. Reused across tool invocations."""
    return CostModelClient()


@functools.lru_cache(maxsize=1)
def _underwriting_client() -> UnderwritingClient:
    """Lazily construct + cache the UnderwritingClient. Reused across tool invocations
    so the daemon thread + MCP session aren't cold-started on every call."""
    return UnderwritingClient()


# Repo root = parent of `src/plat_agent/orchestrator/tools.py` walked up 3 levels.
_REPO_ROOT = Path(__file__).resolve().parents[3]


def _underwriting_engine_path() -> Path:
    """Resolve the multifamily-underwriting engine path the same way
    UnderwritingClient does, so canonical-deal lookups under the engine's
    `runs/deals/` tree work without env juggling."""
    env_path = os.environ.get("UNDERWRITING_ENGINE_PATH")
    if env_path:
        return Path(env_path)
    return _REPO_ROOT.parent / "multifamily-underwriting"


def _DEAL_INPUT_CANDIDATES(deal_id: str) -> list[Path]:  # noqa: N802 (tests monkeypatch this name)
    """Ordered list of paths to probe when loading canonical deal JSON for
    `deal_id`. First match wins.

    Defined as a function (not a constant) so each call re-reads the env vars
    and the search starts from a deterministic root regardless of the agent's
    cwd. Tests monkeypatch this symbol on the module to inject fixtures.
    """
    engine = _underwriting_engine_path()
    return [
        _REPO_ROOT / "data" / "deals" / f"{deal_id}.json",
        _REPO_ROOT / "tests" / "fixtures" / "deals" / f"{deal_id}.json",
        engine / "runs" / "deals" / deal_id / "engine_inputs.json",
        engine / "data" / "deals" / f"{deal_id}.json",
    ]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ok(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}]}


def _fail(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "is_error": True}


# Cost-model tool shape map. Some MCP tools take a single dict-typed
# parameter (the canonical deal/property/scope dict), others take loose
# keyword args. The agent always passes the deal data via `inputs`; we
# wrap it correctly here so the agent doesn't have to know per-tool
# signatures.
#
# Keys are MCP tool names; values are the MCP parameter name that should
# receive the entire `inputs` dict. Tools not in this map receive `inputs`
# unwrapped (each top-level key becomes a kwarg).
_DICT_ARG_TOOLS: dict[str, str] = {
    "estimate_from_deal": "deal_dict",
    "estimate_scope": "scope_request_dict",
    "register_property": "property_data",
    "estimate_property_from_model": "property_data",
}


def _wrap_inputs_for_tool(tool_name: str, inputs: dict) -> dict:
    """Wrap the agent-provided `inputs` for the MCP call.

    For dict-arg tools, the costmodel tool's signature is
    ``def tool(<param_name>: dict)`` — MCP expects ``arguments`` to be
    ``{<param_name>: <inputs>}``. For everything else, ``inputs`` is the
    arguments dict directly.
    """
    param = _DICT_ARG_TOOLS.get(tool_name)
    if param is None:
        return inputs
    # If the agent already wrapped (e.g., legacy callers that knew the
    # param name), don't double-wrap. Use set equality so we don't depend
    # on dict iteration order (CPython 3.7+ is insertion-ordered, but the
    # contract should not). Pre-wrapped means exactly one key, == the
    # parameter name; a multi-key payload that happens to start with
    # ``param`` is NOT considered pre-wrapped.
    if set(inputs.keys()) == {param}:
        return inputs
    return {param: inputs}


def _flatten_numeric(d: dict, prefix: str = "") -> dict[str, float]:
    """Walk a nested dict (and lists of dicts) and pull out (key, float) leaves.

    Nested dict keys are joined with dots (``roi_result.roi_pct``). List
    elements get an indexed prefix (``renovation_programs.0.renovation_cost_per_unit``)
    so the new ``estimate_from_deal`` response shape — which puts the
    interesting numbers under ``estimates[]`` and ``renovation_programs[]``
    — produces useful summary lines for compare_scenarios.
    """
    out: dict[str, float] = {}
    for k, v in d.items():
        key = f"{prefix}{k}" if prefix else k
        if isinstance(v, dict):
            out.update(_flatten_numeric(v, prefix=f"{key}."))
        elif isinstance(v, list):
            for i, item in enumerate(v):
                if isinstance(item, dict):
                    out.update(_flatten_numeric(item, prefix=f"{key}.{i}."))
                elif isinstance(item, (int, float)) and not isinstance(item, bool):
                    out[f"{key}.{i}"] = float(item)
        elif isinstance(v, (int, float)) and not isinstance(v, bool):
            out[key] = float(v)
    return out


def _format_kv(numbers: dict[str, float]) -> str:
    """Render `{"k": 1.0}` as `k=1.0` (space-separated)."""
    parts = []
    for k, v in numbers.items():
        # Use repr for floats to avoid losing precision in round-trips, but
        # render integer-valued floats without trailing `.0` for readability.
        if v == int(v) and abs(v) >= 1:
            parts.append(f"{k}={int(v)}")
        else:
            parts.append(f"{k}={v}")
    return " ".join(parts)


def _format_underwriting_kv(summary: dict) -> str:
    """Curated subset of the underwriting summary as `key=value` pairs.

    Keys are the same ones a cost-model output might overlap with so
    compare_scenarios can compute meaningful deltas. Aliases:

      irr.levered_irr            → irr
      dscr.minimum               → dscr
      yields.going_in_cap_rate   → cap
      equity_multiple.levered_em → em
      noi_summary.total_noi      → noi
    """
    aliases = {
        "irr": ("irr", "levered_irr"),
        "dscr": ("dscr", "minimum"),
        "cap": ("yields", "going_in_cap_rate"),
        "em": ("equity_multiple", "levered_em"),
        "noi": ("noi_summary", "total_noi"),
    }
    flat: dict[str, float] = {}
    for alias, path in aliases.items():
        node = summary
        for part in path:
            if not isinstance(node, dict):
                node = None
                break
            node = node.get(part)
        if isinstance(node, (int, float)) and not isinstance(node, bool):
            flat[alias] = float(node)
    return _format_kv(flat)


# ---------------------------------------------------------------------------
# @tool wrappers
# ---------------------------------------------------------------------------

@tool(
    "load_deal_inputs",
    "Load canonical deal-JSON inputs for a deal_id from the local fixtures store",
    {"deal_id": str},
)
async def load_deal_inputs_tool(args: dict[str, Any]) -> dict[str, Any]:
    """Load a canonical underwriting-engine deal JSON for `deal_id`.

    Probes a fixed list of well-known locations (see `_DEAL_INPUT_CANDIDATES`)
    and returns the first match as a JSON envelope:

        {"path": "<absolute path>", "inputs": <decoded deal dict>}

    Failure modes:
      - empty `deal_id`         → is_error, "FAIL: deal_id required"
      - no candidate exists     → is_error, lists every path tried
      - candidate is bad JSON   → is_error, surfaces the JSON error

    The agent is expected to take the returned `inputs` dict and pass it
    verbatim to `run_underwriting` (and, where applicable, derive a
    cost-model `inputs` payload from it).
    """
    deal_id = (args.get("deal_id") or "").strip()
    if not deal_id:
        return _fail("FAIL: deal_id required")

    candidates = _DEAL_INPUT_CANDIDATES(deal_id)
    for path in candidates:
        if not path.exists():
            continue
        try:
            inputs = json.loads(path.read_text())
        except json.JSONDecodeError as exc:
            return _fail(
                f"FAIL load_deal_inputs: {path} contains invalid JSON: {exc}"
            )
        return _ok(json.dumps({"path": str(path), "inputs": inputs}))

    tried = [str(p) for p in candidates]
    return _fail(
        f"FAIL load_deal_inputs: no canonical deal JSON found for '{deal_id}' "
        f"(tried: {tried})"
    )


@tool(
    "run_cost_model",
    "Run plat-costmodel for a deal+scenario",
    {"deal_id": str, "scenario": str, "tool_name": str, "inputs": dict},
)
async def run_cost_model_tool(args: dict[str, Any]) -> dict[str, Any]:
    if not args.get("deal_id"):
        return _fail("FAIL: deal_id required")

    deal_id = args["deal_id"]
    scenario = args.get("scenario", "base")
    # Default to estimate_from_deal: it consumes the canonical
    # multifamily-underwriting deal shape directly, which is what the
    # screen_deal flow already loads via load_deal_inputs. Callers that
    # need a different costmodel tool can still pass tool_name explicitly.
    tool_name = args.get("tool_name") or "estimate_from_deal"
    inputs = args.get("inputs") or {}

    # Map the agent's `inputs` dict into the MCP tool's named-arg shape.
    # estimate_from_deal/estimate_scope take a single dict-typed parameter
    # whose name matches the tool's contract; passing the deal verbatim as
    # MCP `arguments` would attempt to bind each top-level key as a kwarg
    # and fail (today's fixture has `unit_cohorts`, `metadata`, etc., none
    # of which are formal parameters). Other tools (the legacy loose-kwarg
    # ones like check_renovation_roi) take `inputs` flat.
    mcp_arguments = _wrap_inputs_for_tool(tool_name, inputs)

    client = _cost_model_client()
    try:
        result = await client.async_call_tool(tool_name, mcp_arguments)
    except Exception as exc:  # noqa: BLE001 — surface to agent as tool error
        return _fail(f"FAIL run_cost_model: {exc}")

    if not isinstance(result, dict):
        return _fail(f"FAIL run_cost_model: non-dict result: {result!r}")

    # Costmodel returns a ValidationProblem dict when input validation fails
    # ({error_type, message, field_errors, hint}). It's still a 200-OK MCP
    # response, but to the agent it's a failure that should not be silently
    # treated as success when no numeric fields happen to be present.
    if "error_type" in result and "message" in result:
        return _fail(
            f"FAIL run_cost_model: {result['error_type']}: {result['message']}"
        )

    metrics = _flatten_numeric(result)
    summary = _format_kv(metrics) if metrics else "(no numeric fields)"
    return _ok(f"deal_id={deal_id} scenario={scenario} {summary}")


@tool(
    "run_underwriting",
    "Run multifamily-underwriting for a deal",
    {"deal_id": str, "inputs": dict},
)
async def run_underwriting_tool(args: dict[str, Any]) -> dict[str, Any]:
    if not args.get("deal_id"):
        return _fail("FAIL: deal_id required")

    deal_id = args["deal_id"]
    inputs = args.get("inputs") or {}

    client = _underwriting_client()
    try:
        # run_summary is sync (queues to a daemon worker thread internally,
        # but the public method blocks). Offload so we don't stall the agent's
        # event loop for the ~1-2s engine cold start.
        result = await asyncio.to_thread(client.run_summary, inputs)
    except Exception as exc:  # noqa: BLE001
        return _fail(f"FAIL run_underwriting: {exc}")

    if not isinstance(result, dict):
        return _fail(f"FAIL run_underwriting: non-dict result: {result!r}")

    if result.get("status") == "error":
        return _fail(f"FAIL run_underwriting: {result.get('error', 'engine error')}")

    summary = _format_underwriting_kv(result) or "(no metrics)"
    return _ok(f"deal_id={deal_id} {summary}")


# ---------------------------------------------------------------------------
# compare_scenarios — unchanged contract: parses key=value strings.
# ---------------------------------------------------------------------------

def _parse_kv(text: str) -> dict[str, float]:
    out: dict[str, float] = {}
    for part in text.split():
        if "=" in part:
            k, v = part.split("=", 1)
            try:
                out[k] = float(v)
            except ValueError:
                continue
    return out


@tool(
    "compare_scenarios",
    "Compare cost-model vs underwriting summaries",
    {"cost_model_summary": str, "underwriting_summary": str},
)
async def compare_scenarios_tool(args: dict[str, Any]) -> dict[str, Any]:
    a = _parse_kv(args["cost_model_summary"])
    b = _parse_kv(args["underwriting_summary"])
    keys = sorted(set(a) & set(b))
    deltas = {k: round(b[k] - a[k], 4) for k in keys}
    return _ok(f"delta {deltas}")
