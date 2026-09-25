from pathlib import Path

from plat_agent.lifecycle.protocol import LifecycleStep, StepResult, StepStatus
from plat_agent.lifecycle.state import LifecycleState, BlockerItem


def test_step_result_ok() -> None:
    r = StepResult(status="ok")
    assert r.status == "ok"
    assert r.blockers == []


def test_step_result_blocked() -> None:
    blocker = BlockerItem(step="intake", id="missing_t12", description="x")
    r = StepResult(status="blocked", blockers=[blocker])
    assert r.status == "blocked"
    assert r.blockers == [blocker]


def test_step_result_error() -> None:
    r = StepResult(status="error", error_message="kaboom")
    assert r.status == "error"
    assert r.error_message == "kaboom"


def test_step_protocol_implementable() -> None:
    """Verify a class implementing LifecycleStep type-checks correctly."""

    class FakeStep:
        name = "fake"

        def run(self, state: LifecycleState, run_dir: Path) -> StepResult:
            return StepResult(status="ok")

        def is_satisfied(self, state: LifecycleState, run_dir: Path) -> bool:
            return True

    # Should be usable as LifecycleStep without TypeError
    step: LifecycleStep = FakeStep()
    state = LifecycleState(deal_slug="x", run_id="r")
    result = step.run(state, Path("/tmp/x"))
    assert result.status == "ok"
    assert step.is_satisfied(state, Path("/tmp/x")) is True
