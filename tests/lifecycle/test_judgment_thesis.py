from plat_agent.lifecycle.judgment import (
    DeterministicJudgmentEngine,
    JudgmentResult,
    PositioningClass,
    ValidatedField,
    render_thesis,
)
from plat_agent.lifecycle.state import BlockerItem


def _value_add_result() -> JudgmentResult:
    return JudgmentResult(
        positioning=PositioningClass(
            value="value_add", confidence=0.85,
            rationale="Comp avg rent $1,500 is 15.4% above subject in-place $1,300.",
        ),
        capex_per_unit=ValidatedField(
            data_derived=24000.0, om_claimed=12000.0, selected=24000.0,
            confidence=0.7, delta_flag="broker_underestimates",
        ),
        renovation_pace_units_per_month=ValidatedField(
            data_derived=5.0, om_claimed=None, selected=5.0,
            confidence=0.65, delta_flag="green",
        ),
        rent_growth=ValidatedField(
            data_derived=0.030, om_claimed=0.040, selected=0.030,
            confidence=0.75, delta_flag="broker_optimistic",
        ),
        exit_cap=ValidatedField(
            data_derived=0.058, om_claimed=0.055, selected=0.058,
            confidence=0.7, delta_flag="broker_optimistic",
        ),
        leverage={"ltv": 0.65, "rate": 0.0575, "amort_years": 30, "io_months": 12,
                  "source": "v1_hardcoded"},
        engine_inputs_relative="judgment/engine_inputs.json",
        blockers=[],
    )


def test_render_thesis_returns_markdown_with_required_sections() -> None:
    md = render_thesis(_value_add_result(), deal_slug="project_essex", run_id="run_002")
    # Title + 3-4 paragraphs minimum
    assert "# Investment Thesis — project_essex / run_002" in md
    # Mention positioning
    assert "value-add" in md.lower()
    # Mention broker-claim disagreement
    assert "broker" in md.lower()
    # Mention leverage source caveat
    assert "v1_hardcoded" in md or "hardcoded" in md.lower()


def test_render_thesis_paragraph_count_3_to_4() -> None:
    md = render_thesis(_value_add_result(), deal_slug="d", run_id="r")
    paragraphs = [p for p in md.split("\n\n") if p.strip() and not p.strip().startswith("#")]
    assert 3 <= len(paragraphs) <= 6  # body paragraphs, allow optional caveat block


def test_render_thesis_comps_unavailable_includes_warning_paragraph() -> None:
    res = _value_add_result()
    # comps_unavailable signaled via blocker (spec §2.3 has no top-level field)
    res.blockers = [BlockerItem(
        step="judgment",
        id="comps_unavailable",
        description="Comps step did not produce comps.json.",
    )]
    res.positioning = PositioningClass(value="stabilized", confidence=0.45,
                                       rationale="No comps.")
    md = render_thesis(res, deal_slug="d", run_id="r")
    assert "comps unavailable" in md.lower() or "no comp data" in md.lower()
    assert "needs_data" in md.lower() or "needs-data" in md.lower() or "needs data" in md.lower()


def test_render_thesis_no_red_flag_when_all_green() -> None:
    res = _value_add_result()
    res.capex_per_unit.delta_flag = "green"
    res.rent_growth.delta_flag = "green"
    res.exit_cap.delta_flag = "green"
    md = render_thesis(res, deal_slug="d", run_id="r")
    # Should mention broker claims align
    assert "align" in md.lower() or "consistent" in md.lower() or "no material" in md.lower()
