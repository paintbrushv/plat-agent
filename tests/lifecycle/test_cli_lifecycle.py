from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner


def _data_room(tmp_path: Path) -> Path:
    src = tmp_path / "data_room_essex"
    src.mkdir()
    (src / "OM.pdf").write_bytes(b"x")
    return src


def test_lifecycle_cli_clean_run(tmp_path: Path) -> None:
    from plat_agent.lifecycle.cli import lifecycle_cmd
    from plat_agent.lifecycle.runner import LifecycleResult

    runner = CliRunner()
    fake_result = LifecycleResult(
        deal_slug="data_room_essex", run_id="run_001", status="memo_ready",
        memo_path=tmp_path / "memo.md", exit_code=0,
    )

    with patch("plat_agent.lifecycle.cli.run_lifecycle", return_value=fake_result) as m:
        result = runner.invoke(lifecycle_cmd, [str(_data_room(tmp_path))])

    assert result.exit_code == 0
    assert "data_room_essex" in result.output
    assert "run_001" in result.output
    assert "memo_ready" in result.output
    m.assert_called_once()
    # Must pass data_room as positional arg
    args, kwargs = m.call_args
    assert kwargs.get("resume_run_id") is None
    assert kwargs.get("rerun_from") is None


def test_lifecycle_cli_resume(tmp_path: Path) -> None:
    from plat_agent.lifecycle.cli import lifecycle_cmd
    from plat_agent.lifecycle.runner import LifecycleResult

    runner = CliRunner()
    fake_result = LifecycleResult(
        deal_slug="project_essex", run_id="run_002", status="memo_ready",
        exit_code=0,
    )
    src = _data_room(tmp_path)

    with patch("plat_agent.lifecycle.cli.run_lifecycle",
               return_value=fake_result) as m:
        result = runner.invoke(lifecycle_cmd, [
            str(src), "--resume", "project_essex/run_002",
        ])

    assert result.exit_code == 0
    args, kwargs = m.call_args
    assert kwargs["deal_slug"] == "project_essex"
    assert kwargs["resume_run_id"] == "run_002"


def test_lifecycle_cli_rerun_from(tmp_path: Path) -> None:
    from plat_agent.lifecycle.cli import lifecycle_cmd
    from plat_agent.lifecycle.runner import LifecycleResult

    runner = CliRunner()
    fake_result = LifecycleResult(
        deal_slug="project_essex", run_id="run_002", status="memo_ready",
        exit_code=0,
    )
    src = _data_room(tmp_path)

    with patch("plat_agent.lifecycle.cli.run_lifecycle",
               return_value=fake_result) as m:
        result = runner.invoke(lifecycle_cmd, [
            str(src), "--resume", "project_essex/run_002",
            "--rerun-from", "comps",
        ])

    assert result.exit_code == 0
    args, kwargs = m.call_args
    assert kwargs["resume_run_id"] == "run_002"
    assert kwargs["rerun_from"] == "comps"


def test_lifecycle_cli_propagates_exit_code(tmp_path: Path) -> None:
    from plat_agent.lifecycle.cli import lifecycle_cmd
    from plat_agent.lifecycle.runner import LifecycleResult

    runner = CliRunner()
    fake_result = LifecycleResult(
        deal_slug="d", run_id="r", status="failed_at_intake", exit_code=1,
    )
    with patch("plat_agent.lifecycle.cli.run_lifecycle", return_value=fake_result):
        result = runner.invoke(lifecycle_cmd, [str(_data_room(tmp_path))])
    assert result.exit_code == 1


def test_lifecycle_cli_no_judgment_mode_flag(tmp_path: Path) -> None:
    """§5.4 passthrough guardrail: CLI must NOT expose --judgment-mode."""
    from plat_agent.lifecycle.cli import lifecycle_cmd

    runner = CliRunner()
    result = runner.invoke(lifecycle_cmd, [str(_data_room(tmp_path)),
                                            "--judgment-mode", "passthrough"])
    # Click should reject the unknown option
    assert result.exit_code != 0
    assert "--judgment-mode" in result.output or "no such option" in result.output.lower()


def test_lifecycle_cli_rejects_rerun_from_without_resume(tmp_path: Path) -> None:
    """--rerun-from requires --resume."""
    from plat_agent.lifecycle.cli import lifecycle_cmd

    runner = CliRunner()
    result = runner.invoke(lifecycle_cmd, [str(_data_room(tmp_path)),
                                            "--rerun-from", "comps"])
    assert result.exit_code != 0
    assert "--resume" in result.output


def test_lifecycle_cli_passes_address_and_market(tmp_path: Path) -> None:
    """V1.3 — --address / --market CLI overrides reach run_lifecycle."""
    from plat_agent.lifecycle.cli import lifecycle_cmd
    from plat_agent.lifecycle.runner import LifecycleResult

    runner = CliRunner()
    fake_result = LifecycleResult(
        deal_slug="d", run_id="run_001", status="memo_ready", exit_code=0,
    )

    with patch("plat_agent.lifecycle.cli.run_lifecycle",
               return_value=fake_result) as m:
        result = runner.invoke(lifecycle_cmd, [
            str(_data_room(tmp_path)),
            "--address", "1234 Foo St, Plano, TX 75024",
            "--market", "DFW",
        ])

    assert result.exit_code == 0
    _args, kwargs = m.call_args
    assert kwargs["address"] == "1234 Foo St, Plano, TX 75024"
    assert kwargs["market"] == "DFW"


def test_lifecycle_cli_address_and_market_default_to_none(tmp_path: Path) -> None:
    """V1.3 — when overrides are not supplied, both default to None."""
    from plat_agent.lifecycle.cli import lifecycle_cmd
    from plat_agent.lifecycle.runner import LifecycleResult

    runner = CliRunner()
    fake_result = LifecycleResult(
        deal_slug="d", run_id="run_001", status="memo_ready", exit_code=0,
    )

    with patch("plat_agent.lifecycle.cli.run_lifecycle",
               return_value=fake_result) as m:
        result = runner.invoke(lifecycle_cmd, [str(_data_room(tmp_path))])

    assert result.exit_code == 0
    _args, kwargs = m.call_args
    assert kwargs["address"] is None
    assert kwargs["market"] is None


def test_lifecycle_cli_passes_project_root_override(tmp_path: Path) -> None:
    from plat_agent.lifecycle.cli import lifecycle_cmd
    from plat_agent.lifecycle.runner import LifecycleResult

    runner = CliRunner()
    fake_result = LifecycleResult(
        deal_slug="d", run_id="run_001", status="memo_ready", exit_code=0,
    )
    project_root = tmp_path / "mfu"
    project_root.mkdir()

    with patch("plat_agent.lifecycle.cli.run_lifecycle",
               return_value=fake_result) as m:
        result = runner.invoke(lifecycle_cmd, [
            str(_data_room(tmp_path)),
            "--project-root", str(project_root),
        ])

    assert result.exit_code == 0
    _args, kwargs = m.call_args
    assert kwargs["project_root"] == project_root


def test_lifecycle_cli_defaults_to_sibling_mfu_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from plat_agent.lifecycle.cli import lifecycle_cmd
    from plat_agent.lifecycle.runner import LifecycleResult

    runner = CliRunner()
    fake_result = LifecycleResult(
        deal_slug="d", run_id="run_001", status="memo_ready", exit_code=0,
    )
    plat_root = tmp_path / "plat-agent"
    mfu_root = tmp_path / "multifamily-underwriting"
    plat_root.mkdir()
    (mfu_root / "engine").mkdir(parents=True)
    monkeypatch.chdir(plat_root)

    with patch("plat_agent.lifecycle.cli.run_lifecycle",
               return_value=fake_result) as m:
        result = runner.invoke(lifecycle_cmd, [str(_data_room(tmp_path))])

    assert result.exit_code == 0
    _args, kwargs = m.call_args
    assert kwargs["project_root"] == mfu_root.resolve()


def test_lifecycle_cli_passes_raw_millage_without_prompting(tmp_path: Path) -> None:
    from plat_agent.lifecycle.cli import lifecycle_cmd
    from plat_agent.lifecycle.runner import LifecycleResult

    runner = CliRunner()
    fake_result = LifecycleResult(
        deal_slug="d", run_id="run_001", status="memo_ready", exit_code=0,
    )

    with (
        patch("plat_agent.lifecycle.cli.run_lifecycle", return_value=fake_result) as run,
        patch("click.prompt") as prompt,
        patch("click.confirm") as confirm,
    ):
        result = runner.invoke(
            lifecycle_cmd,
            [str(_data_room(tmp_path)), "--millage-rate", "25.310"],
        )

    assert result.exit_code == 0
    assert run.call_args.kwargs["millage_rate"] == "25.310"
    prompt.assert_not_called()
    confirm.assert_not_called()


def test_lifecycle_cli_millage_defaults_to_none_without_prompting(
    tmp_path: Path,
) -> None:
    from plat_agent.lifecycle.cli import lifecycle_cmd
    from plat_agent.lifecycle.runner import LifecycleResult

    runner = CliRunner()
    fake_result = LifecycleResult(
        deal_slug="d", run_id="run_001", status="needs_analyst_input", exit_code=2,
    )

    with (
        patch("plat_agent.lifecycle.cli.run_lifecycle", return_value=fake_result) as run,
        patch("click.prompt") as prompt,
        patch("click.confirm") as confirm,
    ):
        result = runner.invoke(lifecycle_cmd, [str(_data_room(tmp_path))])

    assert result.exit_code == 2
    assert run.call_args.kwargs["millage_rate"] is None
    prompt.assert_not_called()
    confirm.assert_not_called()
