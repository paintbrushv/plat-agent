"""Tests for AzureRunBackend — uses a mocked httpx client.

Mocked at the transport layer so no live Azure endpoint is required. Verifies
the enqueue-then-poll flow, error propagation, timeout handling, auth header,
and flat-to-nested metrics normalization.
"""

from unittest.mock import MagicMock, patch

import pytest

from plat_agent.sweep.azure_backend import AzureRunBackend


@pytest.fixture
def backend():
    return AzureRunBackend(
        endpoint="https://example.azurewebsites.net",
        function_key="FAKE_KEY",
        poll_interval_seconds=0.0,
        timeout_seconds=5.0,
    )


def _enqueue_response() -> MagicMock:
    r = MagicMock()
    r.status_code = 202
    r.json.return_value = {"run_id": "abc123"}
    return r


def _status_response(status: str, results: dict | None = None) -> MagicMock:
    r = MagicMock()
    r.status_code = 200
    body = {"status": status}
    if results is not None:
        body["results"] = results
    r.json.return_value = body
    return r


def _flat_success_results() -> dict:
    return {
        "status": "success",
        "metrics": {
            "levered_irr": 0.14, "unlevered_irr": 0.095,
            "levered_em": 1.8, "unlevered_em": 1.55,
            "partnership_irr": 0.16, "partnership_em": 1.9,
            "minimum_dscr": 1.25, "average_dscr": 1.45,
            "going_in_cap": 0.055,
        },
        "cashflow_summary": {"years": 7, "noi_year_1": 1_800_000},
    }


class TestAzureRunBackendConstruction:
    def test_requires_endpoint_and_key(self):
        with pytest.raises(ValueError, match="endpoint"):
            AzureRunBackend(endpoint="", function_key="x")
        with pytest.raises(ValueError, match="function_key"):
            AzureRunBackend(endpoint="https://e", function_key="")

    def test_reads_env_vars(self, monkeypatch):
        monkeypatch.setenv("UNDERWRITING_AZURE_ENDPOINT", "https://env.azurewebsites.net/")
        monkeypatch.setenv("UNDERWRITING_AZURE_KEY", "ENVKEY")
        b = AzureRunBackend()
        assert b._endpoint == "https://env.azurewebsites.net"  # trailing slash stripped
        assert b._key == "ENVKEY"


class TestAzureRunBackendHappyPath:
    def test_enqueues_then_polls_to_completion(self, backend):
        deal = {"metadata": {"deal_id": "D1"}}
        with patch("plat_agent.sweep.azure_backend.httpx.Client") as MockClient:
            client = MockClient.return_value.__enter__.return_value
            client.post.return_value = _enqueue_response()
            client.get.side_effect = [
                _status_response("queued"),
                _status_response("running"),
                _status_response("succeeded", _flat_success_results()),
            ]
            result = backend.run_deal(deal)

        assert result["status"] == "success"
        # Verify flat-to-nested normalization
        assert result["irr"]["levered_irr"] == 0.14
        assert result["irr"]["unlevered_irr"] == 0.095
        assert result["equity_multiple"]["levered_em"] == 1.8
        assert result["dscr"]["minimum"] == 1.25
        assert result["dscr"]["average"] == 1.45
        assert result["yields"]["going_in_cap_rate"] == 0.055
        # Verify the call pattern
        assert client.post.call_count == 1
        assert client.get.call_count == 3

    def test_post_payload_includes_inputs_and_options(self, backend):
        deal = {"metadata": {"deal_id": "D1"}}
        with patch("plat_agent.sweep.azure_backend.httpx.Client") as MockClient:
            client = MockClient.return_value.__enter__.return_value
            client.post.return_value = _enqueue_response()
            client.get.return_value = _status_response("succeeded", _flat_success_results())
            backend.run_deal(deal)

        _, kwargs = client.post.call_args
        body = kwargs["json"]
        assert body["inputs"] == deal
        assert "options" in body


class TestAzureRunBackendAuth:
    def test_function_key_header_on_post(self, backend):
        with patch("plat_agent.sweep.azure_backend.httpx.Client") as MockClient:
            client = MockClient.return_value.__enter__.return_value
            client.post.return_value = _enqueue_response()
            client.get.return_value = _status_response("succeeded", _flat_success_results())
            backend.run_deal({})

        _, kwargs = client.post.call_args
        assert kwargs["headers"].get("x-functions-key") == "FAKE_KEY"

    def test_function_key_header_on_get(self, backend):
        with patch("plat_agent.sweep.azure_backend.httpx.Client") as MockClient:
            client = MockClient.return_value.__enter__.return_value
            client.post.return_value = _enqueue_response()
            client.get.return_value = _status_response("succeeded", _flat_success_results())
            backend.run_deal({})

        _, kwargs = client.get.call_args
        assert kwargs["headers"].get("x-functions-key") == "FAKE_KEY"


class TestAzureRunBackendErrors:
    def test_propagates_engine_failure(self, backend):
        with patch("plat_agent.sweep.azure_backend.httpx.Client") as MockClient:
            client = MockClient.return_value.__enter__.return_value
            client.post.return_value = _enqueue_response()
            client.get.return_value = _status_response(
                "failed",
                {"status": "error", "error": "bad inputs"},
            )
            result = backend.run_deal({})

        assert result["status"] == "error"
        assert "bad inputs" in result["error"]

    def test_times_out_when_run_never_completes(self):
        backend = AzureRunBackend(
            endpoint="https://example.azurewebsites.net",
            function_key="FAKE",
            poll_interval_seconds=0.0,
            timeout_seconds=0.01,
        )
        with patch("plat_agent.sweep.azure_backend.httpx.Client") as MockClient:
            client = MockClient.return_value.__enter__.return_value
            client.post.return_value = _enqueue_response()
            client.get.return_value = _status_response("running")
            result = backend.run_deal({})

        assert result["status"] == "error"
        assert "timeout" in result["error"].lower()

    def test_http_error_becomes_error_status(self, backend):
        import httpx as _httpx

        with patch("plat_agent.sweep.azure_backend.httpx.Client") as MockClient:
            client = MockClient.return_value.__enter__.return_value
            client.post.side_effect = _httpx.HTTPError("network down")
            result = backend.run_deal({})

        assert result["status"] == "error"
        assert "network down" in result["error"]
