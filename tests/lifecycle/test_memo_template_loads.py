# tests/lifecycle/test_memo_template_loads.py
"""Smoke test: the bundled Jinja2 templates load and render a minimal context."""

from __future__ import annotations

from jinja2 import Environment, FileSystemLoader, select_autoescape

from plat_agent.lifecycle.memo import _template_dir  # noqa: F401 — exists after Task 1


def test_memo_template_renders_minimal_context() -> None:
    env = Environment(
        loader=FileSystemLoader(_template_dir()),
        autoescape=select_autoescape(default=False),
        keep_trailing_newline=True,
    )
    tmpl = env.get_template("memo.md.j2")
    out = tmpl.render(
        deal={"slug": "x", "address": "", "units": 0, "vintage": None, "broker": "",
              "asking_price": 0, "ppu": 0},
        recommendation={"value": "NEEDS_DATA", "confidence": 0.0},
        returns={"levered_irr": None, "levered_em": None, "min_dscr": None,
                 "going_in_cap": None, "exit_cap": None},
        thesis_text="(thesis unavailable)",
        broker_claims=[],
        risks=[],
        blockers=[],
        artifact_paths={},
        passthrough_banner=None,
    )
    assert "Deal Memo" in out
    assert "NEEDS_DATA" in out


def test_passthrough_banner_renders() -> None:
    env = Environment(
        loader=FileSystemLoader(_template_dir()),
        autoescape=select_autoescape(default=False),
        keep_trailing_newline=True,
    )
    tmpl = env.get_template("passthrough_banner.md.j2")
    out = tmpl.render(judgment_mode="passthrough")
    assert "TEST RUN" in out
    assert "judgment layer bypassed" in out
