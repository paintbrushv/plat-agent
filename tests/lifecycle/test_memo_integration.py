# tests/lifecycle/test_memo_integration.py
"""End-to-end memo pipeline against the clean fixture, no mocks (except mfu import)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from unittest.mock import patch

from plat_agent.lifecycle import MemoStep
from plat_agent.lifecycle.state import LifecycleState


FIX = Path(__file__).parent / "fixtures" / "memo" / "clean"


def test_full_memo_pipeline_clean_deal(tmp_path: Path) -> None:
    run = tmp_path / "run_002"
    (run / "intake").mkdir(parents=True)
    (run / "comps").mkdir()
    (run / "judgment").mkdir()
    (run / "underwriting").mkdir()
    for src, dst in [
        (FIX / "canonical_deal.json", run / "intake" / "canonical_deal.json"),
        (FIX / "comps.json", run / "comps" / "comps.json"),
        (FIX / "positioning.json", run / "judgment" / "positioning.json"),
        (FIX / "thesis.md", run / "judgment" / "thesis.md"),
        (FIX / "deal_summary.json", run / "underwriting" / "deal_summary.json"),
        (FIX / "_provenance.json", run / "underwriting" / "_provenance.json"),
        (FIX / "_lifecycle_state.json", run / "_lifecycle_state.json"),
    ]:
        shutil.copy(src, dst)

    state = LifecycleState(
        deal_slug="project_essex", run_id="run_002",
        steps_completed=["intake", "comps", "judgment", "underwriting"],
    )

    # Force the import to fail so we exercise the reportlab fallback path
    # (real reportlab — no mocks). If reportlab itself is missing this test
    # is skipped rather than failed.
    try:
        import reportlab  # noqa: F401
    except ImportError:
        import pytest
        pytest.skip("reportlab not installed in this env")

    with patch("plat_agent.lifecycle.memo._import_mfu_onepager", return_value=None):
        result = MemoStep().run(state, run)

    assert result.status == "ok"

    # Markdown content checks
    md = (run / "memo" / "memo.md").read_text()
    assert "# Deal Memo — project_essex" in md
    assert "**PROCEED**" in md
    assert "15.2%" in md
    assert "1234 Essex Ave" in md

    # PDF exists (fallback rendered it)
    assert (run / "memo" / "memo.pdf").exists()
    assert (run / "memo" / "memo.pdf").read_bytes().startswith(b"%PDF")

    # Provenance carries the right metadata
    prov = json.loads((run / "memo" / "_provenance.json").read_text())
    assert prov["pdf_status"] == "fallback"
    assert prov["judgment_mode"] == "deterministic_v1"
    assert prov["passthrough"] is False
    assert prov["draft_mode"] is False

    # _complete marker lists everything
    marker = json.loads((run / "memo" / "_complete").read_text())
    assert set(marker["file_manifest"]) >= {"memo.md", "memo.pdf", "_provenance.json"}


def test_public_api_exports() -> None:
    """All memo-related names appear in plat_agent.lifecycle.__all__."""
    from plat_agent import lifecycle
    expected = {
        "BrokerClaim", "MemoContent", "MemoStep", "PDFRenderResult",
        "assemble_memo_content", "render_broker_claims", "render_memo_markdown",
        "render_returns", "render_risks", "write_memo_markdown", "write_memo_pdf",
    }
    assert expected.issubset(set(lifecycle.__all__))
    for name in expected:
        assert hasattr(lifecycle, name), f"{name} missing from public API"
