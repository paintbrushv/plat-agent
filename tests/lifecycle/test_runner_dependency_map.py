from plat_agent.lifecycle.dependency_map import (
    DependencyDecision,
    decide_after_step,
)
from plat_agent.lifecycle.protocol import StepResult
from plat_agent.lifecycle.state import BlockerItem


def _ok() -> StepResult:
    return StepResult(status="ok")


def _blocked(step: str, *ids: str) -> StepResult:
    return StepResult(status="blocked",
                      blockers=[BlockerItem(step=step, id=i, description="x") for i in ids])


def _err(msg: str = "boom") -> StepResult:
    return StepResult(status="error", error_message=msg)


def test_intake_ok_continues() -> None:
    d = decide_after_step("intake", _ok())
    assert d.halt is False
    assert d.terminal_status is None


def test_intake_hard_error_halts_with_failed_at_intake() -> None:
    d = decide_after_step("intake", _err())
    assert d.halt is True
    assert d.terminal_status == "failed_at_intake"


def test_intake_blocker_continues_degraded() -> None:
    d = decide_after_step("intake", _blocked("intake", "missing_t12"))
    assert d.halt is False
    assert d.terminal_status_if_no_recovery == "memo_ready_with_blockers"


def test_intake_needs_analyst_input_continues_per_spec_4_3() -> None:
    """V1.2: spec §4.3 'intake blocker → comps + judgment continue degraded'.

    Specifically the needs_analyst_input flavor (analyst-required gaps
    surfaced after auto-defaults from rent roll / OM extraction). Must
    not halt the pipeline and must surface memo_ready_with_blockers.
    """
    d = decide_after_step("intake", _blocked("intake", "needs_analyst_input"))
    assert d.halt is False
    assert d.skip_underwriting is False
    assert d.run_memo is True
    assert d.terminal_status_if_no_recovery == "memo_ready_with_blockers"


def test_comps_hard_error_continues_with_comps_unavailable_flag() -> None:
    """§4.3: comps hard-error has a documented alternate path — judgment continues."""
    d = decide_after_step("comps", _err())
    assert d.halt is False
    assert d.comps_unavailable is True
    assert d.terminal_status_if_no_recovery == "memo_ready_with_blockers"


def test_comps_blocker_continues_degraded() -> None:
    d = decide_after_step("comps", _blocked("comps", "scraper_blocked"))
    assert d.halt is False
    assert d.comps_unavailable is False  # blocker, not hard error
    assert d.terminal_status_if_no_recovery == "memo_ready_with_blockers"


def test_judgment_hard_error_halts_underwriting_but_lets_memo_run() -> None:
    """§4.3 + §4.6: memo always attempts to render even on judgment hard error."""
    d = decide_after_step("judgment", _err())
    assert d.skip_underwriting is True
    assert d.run_memo is True
    assert d.terminal_status == "failed_at_judgment"


def test_judgment_blocker_halts_underwriting_in_v1() -> None:
    d = decide_after_step("judgment", _blocked("judgment", "broker_optimistic_capex"))
    assert d.skip_underwriting is True
    assert d.terminal_status_if_no_recovery == "memo_ready_with_blockers"


def test_underwriting_hard_error_runs_memo_in_draft_mode() -> None:
    """§4.3: underwriting hard-error → memo runs in draft. CRM logs failed_at_underwriting."""
    d = decide_after_step("underwriting", _err())
    assert d.halt is False
    assert d.run_memo is True
    assert d.terminal_status_if_no_recovery == "failed_at_underwriting"


def test_memo_hard_error_logs_failed_at_memo() -> None:
    d = decide_after_step("memo", _err())
    assert d.terminal_status == "failed_at_memo"


def test_crm_hard_error_warning_only() -> None:
    """§4.3: CRM hard-error → warning only; memo on disk; exit 0."""
    d = decide_after_step("crm", _err())
    assert d.halt is True  # nothing left to do
    # exit_code is 0 even on CRM failure since memo is what matters
    assert d.exit_code_override == 0
