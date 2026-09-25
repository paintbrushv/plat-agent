"""Tests for sweep.make_backend factory."""

import pytest

from plat_agent.sweep import make_backend
from plat_agent.sweep.azure_backend import AzureRunBackend
from plat_agent.sweep.backend import LocalRunBackend


@pytest.fixture(autouse=True)
def _clear_azure_env(monkeypatch):
    monkeypatch.delenv("UNDERWRITING_AZURE_ENDPOINT", raising=False)
    monkeypatch.delenv("UNDERWRITING_AZURE_KEY", raising=False)


class TestMakeBackend:
    def test_explicit_local(self):
        assert isinstance(make_backend("local"), LocalRunBackend)

    def test_explicit_azure(self, monkeypatch):
        monkeypatch.setenv("UNDERWRITING_AZURE_ENDPOINT", "https://e.azurewebsites.net")
        monkeypatch.setenv("UNDERWRITING_AZURE_KEY", "K")
        assert isinstance(make_backend("azure"), AzureRunBackend)

    def test_explicit_azure_missing_env_raises(self):
        with pytest.raises(ValueError, match="endpoint"):
            make_backend("azure")

    def test_auto_without_env_picks_local(self):
        assert isinstance(make_backend("auto"), LocalRunBackend)
        assert isinstance(make_backend(None), LocalRunBackend)

    def test_auto_with_env_picks_azure(self, monkeypatch):
        monkeypatch.setenv("UNDERWRITING_AZURE_ENDPOINT", "https://e.azurewebsites.net")
        monkeypatch.setenv("UNDERWRITING_AZURE_KEY", "K")
        assert isinstance(make_backend("auto"), AzureRunBackend)
        assert isinstance(make_backend(), AzureRunBackend)

    def test_unknown_name_raises(self):
        with pytest.raises(ValueError, match="unknown backend"):
            make_backend("databricks")  # type: ignore[arg-type]
