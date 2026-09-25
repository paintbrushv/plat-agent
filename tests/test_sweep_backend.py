"""Tests for sweep backend protocol and local implementation."""

from unittest.mock import MagicMock

from plat_agent.sweep.backend import LocalRunBackend, RunBackend


class TestLocalRunBackend:
    def test_implements_run_backend_protocol(self):
        backend = LocalRunBackend(client=MagicMock())
        # Protocol check: has the method
        assert hasattr(backend, "run_deal")
        assert callable(backend.run_deal)

    def test_delegates_to_underwriting_client_run_summary(self):
        mock_client = MagicMock()
        mock_client.run_summary.return_value = {"status": "success", "irr": {"levered_irr": 0.15}}
        backend = LocalRunBackend(client=mock_client)

        deal = {"metadata": {"deal_id": "D1"}}
        result = backend.run_deal(deal)

        mock_client.run_summary.assert_called_once_with(deal)
        assert result == {"status": "success", "irr": {"levered_irr": 0.15}}

    def test_default_client_is_underwriting_client(self):
        """When no client is injected, LocalRunBackend instantiates UnderwritingClient."""
        from plat_agent.underwriting_client import UnderwritingClient

        backend = LocalRunBackend()
        assert isinstance(backend._client, UnderwritingClient)
