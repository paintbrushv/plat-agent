"""Sibling underwriting checkout resolution."""

from pathlib import Path

import pytest

from plat_agent.dispatch.sibling import (
    SiblingRepo,
    primary_checkout_root,
    underwriting_checkout,
)


def test_underwriting_checkout_prefers_historical_name(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PLAT_MULTIFAMILY_UNDERWRITING_PATH", raising=False)
    historical = tmp_path / "multifamily-underwriting"
    public_name = tmp_path / "plat-multifamily-underwriting"
    (historical / "engine").mkdir(parents=True)
    (public_name / "engine").mkdir(parents=True)

    assert underwriting_checkout(projects_dir=tmp_path) == historical.resolve()


def test_underwriting_checkout_uses_public_repo_name(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PLAT_MULTIFAMILY_UNDERWRITING_PATH", raising=False)
    public_name = tmp_path / "plat-multifamily-underwriting"
    (public_name / "engine").mkdir(parents=True)

    assert underwriting_checkout(projects_dir=tmp_path) == public_name.resolve()


def test_underwriting_checkout_env_overrides_directory_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    override = tmp_path / "override"
    override.mkdir()
    monkeypatch.setenv("PLAT_MULTIFAMILY_UNDERWRITING_PATH", str(override))
    public_name = tmp_path / "plat-multifamily-underwriting"
    (public_name / "engine").mkdir(parents=True)

    assert underwriting_checkout(projects_dir=tmp_path) == override.resolve()


def test_primary_checkout_root_follows_worktree_gitdir(tmp_path: Path) -> None:
    primary = tmp_path / "projects" / "plat-agent"
    gitdir = primary / ".git" / "worktrees" / "v3"
    gitdir.mkdir(parents=True)
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / ".git").write_text(f"gitdir: {gitdir}\n")

    assert primary_checkout_root(worktree) == primary.resolve()


def test_from_env_or_default_finds_public_underwriting_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("PLAT_MULTIFAMILY_UNDERWRITING_PATH", raising=False)
    primary = tmp_path / "projects" / "plat-agent"
    primary.mkdir(parents=True)
    public_name = tmp_path / "projects" / "plat-multifamily-underwriting"
    (public_name / "engine").mkdir(parents=True)
    monkeypatch.setattr(
        "plat_agent.dispatch.sibling.primary_checkout_root",
        lambda repo_root=None: primary,
    )

    repo = SiblingRepo.from_env_or_default(
        "multifamily-underwriting",
        "multifamily-underwriting",
    )

    assert repo.name == "multifamily-underwriting"
    assert repo.path == public_name.resolve()
