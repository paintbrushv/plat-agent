"""The documented candidate install must reproduce CI's producer revisions."""

from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
PRODUCERS = {"plat-costmodel", "plat-multifamily-underwriting", "plat-harness"}
PIN_PATTERN = re.compile(
    r"git\+https://github\.com/paintbrushv/(plat-[a-z-]+)\.git@([^\s\"']+)"
)


def test_quick_start_uses_the_same_immutable_producer_pins_as_ci():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    quick_start = readme.split("## Quick start", 1)[1].split("## Environment variables", 1)[0]
    workflow = (ROOT / ".github/workflows/tests.yml").read_text(encoding="utf-8")
    documented = PIN_PATTERN.findall(quick_start)
    tested = PIN_PATTERN.findall(workflow)

    # Require each producer exactly once; a duplicate must not mask a stale pin.
    assert len(documented) == len(tested) == len(PRODUCERS)
    assert set(dict(documented)) == set(dict(tested)) == PRODUCERS
    assert dict(documented) == dict(tested)
    assert all(re.fullmatch(r"[0-9a-f]{40}", sha) for _, sha in documented + tested)
