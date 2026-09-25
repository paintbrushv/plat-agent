# tests/lifecycle/test_memo_pdf.py
"""PDF generation integration with mfu's pdf_onepager (with fallbacks)."""

from __future__ import annotations

import shutil
from pathlib import Path
from unittest.mock import patch

import pytest

from plat_agent.lifecycle.memo import (
    PDFRenderResult,
    assemble_memo_content,
    write_memo_pdf,
)


FIX = Path(__file__).parent / "fixtures" / "memo" / "clean"


def _build_run(tmp_path: Path) -> Path:
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
    return run


def test_pdf_uses_mfu_when_importable(tmp_path: Path) -> None:
    run = _build_run(tmp_path)
    content = assemble_memo_content(run)

    called = {}

    def fake_generate_onepager(results, inputs, output_path, **kwargs):
        called["args"] = (results, inputs, Path(output_path))
        Path(output_path).write_bytes(b"%PDF-1.4 fake")
        return Path(output_path)

    with patch("plat_agent.lifecycle.memo._import_mfu_onepager",
               return_value=fake_generate_onepager):
        result = write_memo_pdf(run, content)

    assert result.status == "ok"
    assert result.path == run / "memo" / "memo.pdf"
    assert result.path.read_bytes().startswith(b"%PDF")
    assert called["args"][2] == run / "memo" / "memo.pdf"


def test_pdf_falls_back_when_mfu_unavailable(tmp_path: Path) -> None:
    run = _build_run(tmp_path)
    content = assemble_memo_content(run)

    with patch("plat_agent.lifecycle.memo._import_mfu_onepager", return_value=None), \
         patch("plat_agent.lifecycle.memo._reportlab_fallback") as fallback:
        fallback.side_effect = lambda content, out: out.write_bytes(b"%PDF-fallback")
        result = write_memo_pdf(run, content)

    assert result.status == "fallback"
    assert "mfu" in (result.detail or "").lower()
    assert result.path.read_bytes().startswith(b"%PDF")


def test_pdf_returns_failed_when_both_paths_raise(tmp_path: Path) -> None:
    """Per §2.5: PDF failure does not invalidate the run."""
    run = _build_run(tmp_path)
    content = assemble_memo_content(run)

    with patch("plat_agent.lifecycle.memo._import_mfu_onepager", return_value=None), \
         patch("plat_agent.lifecycle.memo._reportlab_fallback",
               side_effect=RuntimeError("no reportlab")):
        result = write_memo_pdf(run, content)

    assert result.status == "failed"
    assert "no reportlab" in (result.detail or "")
    assert not (run / "memo" / "memo.pdf").exists()


def test_pdf_passthrough_uses_reportlab_so_banner_is_preserved(tmp_path: Path) -> None:
    """Per spec §5.4, the passthrough banner MUST appear in the PDF.

    mfu's pdf_onepager has no banner slot, so passthrough runs go straight
    to the reportlab fallback (which renders the full Markdown banner).
    """
    run = _build_run(tmp_path)
    content = assemble_memo_content(run)
    # Force passthrough on the assembled content
    content = content.__class__(**{**content.__dict__, "passthrough": True,
                                    "judgment_mode": "passthrough"})

    captured: dict[str, object] = {}

    def fake_generate_onepager(*args, **kwargs):
        captured["mfu_called"] = True
        raise AssertionError("mfu must NOT be called in passthrough mode")

    def fake_fallback(c, out):
        captured["fallback_called"] = True
        out.write_bytes(b"%PDF-fallback")

    with patch("plat_agent.lifecycle.memo._import_mfu_onepager",
               return_value=fake_generate_onepager), \
         patch("plat_agent.lifecycle.memo._reportlab_fallback",
               side_effect=fake_fallback):
        result = write_memo_pdf(run, content)

    assert "mfu_called" not in captured, "mfu path took over in passthrough mode"
    assert captured.get("fallback_called") is True
    assert result.status == "fallback"
    assert "passthrough" in (result.detail or "").lower()


def test_pdf_inputs_shape_matches_mfu_expectation(tmp_path: Path) -> None:
    """The synthesized 'inputs' dict mfu expects has metadata + purchase_assumptions."""
    run = _build_run(tmp_path)
    content = assemble_memo_content(run)

    captured = {}

    def fake_generate_onepager(results, inputs, output_path, **kwargs):
        captured["results"] = results
        captured["inputs"] = inputs
        Path(output_path).write_bytes(b"%PDF")
        return Path(output_path)

    with patch("plat_agent.lifecycle.memo._import_mfu_onepager",
               return_value=fake_generate_onepager):
        write_memo_pdf(run, content)

    assert "metadata" in captured["inputs"]
    assert captured["inputs"]["metadata"]["deal_id"] == "project_essex"
    # results must carry the metric tree pdf_onepager renders
    assert "metrics" in captured["results"] or "irr" in str(captured["results"])
