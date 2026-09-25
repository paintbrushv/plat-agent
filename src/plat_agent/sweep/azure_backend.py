"""Azure-backed RunBackend.

Submits a deal to the underwriting engine's async `/api/runs` endpoint, polls
for completion, and returns normalized nested metrics. Plugs into the existing
RunBackend protocol without any sweep-code changes.

The engine's handle_run_deal returns a flat metrics dict. This backend
normalizes to the nested shape the sweep layer expects — same reshape that
analyzer._normalize_full_metrics does for the direct-import path, so all
downstream code (feasibility gate, summary printer) stays mode-agnostic.

Config:
    UNDERWRITING_AZURE_ENDPOINT  — base URL (e.g. https://stack.azurewebsites.net)
    UNDERWRITING_AZURE_KEY       — function-level auth key

Opt-in — nothing in the sweep code picks this backend automatically; analysts
pass `backend=AzureRunBackend()` explicitly.
"""

import os
import time

import httpx


class AzureTimeoutError(Exception):
    """Raised internally when a run exceeds timeout_seconds."""


class AzureRunBackend:
    """RunBackend that submits to Azure Functions and polls for completion."""

    def __init__(
        self,
        endpoint: str | None = None,
        function_key: str | None = None,
        poll_interval_seconds: float = 2.0,
        timeout_seconds: float = 300.0,
    ):
        endpoint = endpoint if endpoint is not None else os.environ.get("UNDERWRITING_AZURE_ENDPOINT", "")
        function_key = function_key if function_key is not None else os.environ.get("UNDERWRITING_AZURE_KEY", "")
        if not endpoint:
            raise ValueError(
                "AzureRunBackend requires endpoint (pass explicitly or set "
                "UNDERWRITING_AZURE_ENDPOINT env var)."
            )
        if not function_key:
            raise ValueError(
                "AzureRunBackend requires function_key (pass explicitly or set "
                "UNDERWRITING_AZURE_KEY env var)."
            )
        self._endpoint = endpoint.rstrip("/")
        self._key = function_key
        self._poll_interval = poll_interval_seconds
        self._timeout = timeout_seconds

    def run_deal(self, deal_inputs: dict) -> dict:
        """Submit one deal, poll until done, return normalized metrics.

        Never raises for engine-level errors or timeouts — returns a
        structured {"status": "error", "error": "..."} dict so sweep code
        can treat errors uniformly.
        """
        try:
            return self._submit_and_poll(deal_inputs)
        except AzureTimeoutError as e:
            return {"status": "error", "error": f"timeout: {e}"}
        except httpx.HTTPError as e:
            return {"status": "error", "error": f"http error: {e}"}

    def _submit_and_poll(self, deal_inputs: dict) -> dict:
        headers = {
            "x-functions-key": self._key,
            "content-type": "application/json",
        }
        with httpx.Client(timeout=30.0) as client:
            enqueue = client.post(
                f"{self._endpoint}/api/runs",
                json={"inputs": deal_inputs, "options": {"include_cashflow": False}},
                headers=headers,
            )
            enqueue.raise_for_status()
            run_id = enqueue.json()["run_id"]

            deadline = time.time() + self._timeout
            while time.time() < deadline:
                r = client.get(
                    f"{self._endpoint}/api/runs/{run_id}",
                    headers=headers,
                )
                r.raise_for_status()
                body = r.json()
                status = body.get("status")
                if status == "succeeded":
                    return self._normalize(body.get("results", {}))
                if status == "failed":
                    results = body.get("results") or {}
                    return {
                        "status": "error",
                        "error": results.get("error", "run failed without detail"),
                    }
                time.sleep(self._poll_interval)
            raise AzureTimeoutError(f"run {run_id} did not complete in {self._timeout}s")

    @staticmethod
    def _normalize(flat_results: dict) -> dict:
        """Reshape engine's flat response into the nested summary shape
        used by MCP run_deal_summary. Same mapping as
        analyzer._normalize_full_metrics — keep in sync if one changes.
        """
        m = flat_results.get("metrics", {}) or {}
        return {
            "status": flat_results.get("status", "success"),
            "irr": {
                "levered_irr": m.get("levered_irr"),
                "unlevered_irr": m.get("unlevered_irr"),
                "partnership_irr": m.get("partnership_irr"),
            },
            "equity_multiple": {
                "levered_em": m.get("levered_em"),
                "unlevered_em": m.get("unlevered_em"),
                "partnership_em": m.get("partnership_em"),
            },
            "dscr": {
                "minimum": m.get("minimum_dscr"),
                "average": m.get("average_dscr"),
            },
            "yields": {
                "going_in_cap_rate": m.get("going_in_cap"),
            },
            "cashflow_summary": flat_results.get("cashflow_summary", {}),
        }
