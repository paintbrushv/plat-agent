from __future__ import annotations

from pathlib import Path

from plat_agent.lifecycle.om_parsers import (
    GENERIC_OM_PARSER_FAMILY,
    extract_labelled_property_tax_evidence,
    looks_like_debt_guidance_text,
    parse_broker_om_snapshot_text,
)


FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "om_parsers"


def test_parse_broker_om_snapshot_text_maple_grove_fixture() -> None:
    text = (FIXTURE_ROOT / "maple_grove_om.txt").read_text()

    snapshot = parse_broker_om_snapshot_text(text, source_name="The Demo at Maple Grove OM.pdf")

    assert snapshot is not None
    assert snapshot["deal_name"] == "The Demo at Maple Grove"
    assert snapshot["address"] == "742 Sample Ave, Demo City, ST 00000"
    assert snapshot["year_built"] == 1984
    assert snapshot["unit_count"] == 238
    assert snapshot["trailing_noi"] == 1_785_165.0
    assert snapshot["noi"] == 2_429_328.0
    assert snapshot["expense_assumptions"]["insurance"] == 166_600.0
    assert snapshot["property_tax_context"]["local_tax_rate"] == 0.02019675
    assert "full_value_reassessment_supported" not in snapshot["property_tax_context"]
    assert snapshot["property_tax_context"]["property_tax_evidence_candidates"] == [
        {
            "millage_rate_mills": 20.19675,
            "source": "offering_memorandum",
            "source_locator": (
                "raw_inputs/The Demo at Maple Grove OM.pdf, page 1, "
                "line 276, table PROPERTY TAXES, label TAX RATE PER $100"
            ),
        }
    ]

    parser_metadata = snapshot["parser_metadata"]
    assert parser_metadata["parser_family"] == GENERIC_OM_PARSER_FAMILY
    assert "income_table" in parser_metadata["matched_sections"]
    assert "expense_table" in parser_metadata["matched_sections"]
    assert "property_tax_section" in parser_metadata["matched_sections"]
    assert "broker_noi" in parser_metadata["matched_sections"]


def test_looks_like_debt_guidance_text_maple_grove_fixture() -> None:
    text = (FIXTURE_ROOT / "maple_grove_debt_guidance.txt").read_text()

    assert looks_like_debt_guidance_text(text) is True


def test_parse_broker_om_snapshot_text_returns_none_for_noise() -> None:
    assert parse_broker_om_snapshot_text("hello world") is None


def test_parse_broker_om_snapshot_text_handles_unlabelled_address_year_block() -> None:
    text = """
A broker is pleased to present the Demo at Maple Grove,
a 238-unit multifamily opportunity located in Sample City, ST.

742 Sample Ave, Demo City, ST 00000
Sample County
1984

NUMBER OF UNITS
238
"""

    snapshot = parse_broker_om_snapshot_text(text, source_name="The Demo at Maple Grove OM.pdf")

    assert snapshot is not None
    assert snapshot["address"] == "742 Sample Ave, Demo City, ST 00000"
    assert snapshot["year_built"] == 1984
    assert snapshot["unit_count"] == 238


def test_extract_labelled_property_tax_evidence_normalizes_explicit_units() -> None:
    candidates, locations = extract_labelled_property_tax_evidence(
        """
        Combined property tax rate: 2.531 per $100
        Total property tax rate: 2.531%
        Aggregate ad valorem tax rate: 0.02531 decimal rate
        Combined millage: 25.31 mills per $1,000 of assessed value
        """,
        source="county_tax_notice",
        source_locator="raw_inputs/notice.pdf",
    )

    assert [candidate["millage_rate_mills"] for candidate in candidates] == [
        25.31, 25.31, 25.31, 25.31,
    ]
    assert all(
        candidate["source"] == "county_tax_notice"
        for candidate in candidates
    )
    assert locations == [
        "raw_inputs/notice.pdf, page 1, line 2, label Combined property tax rate",
        "raw_inputs/notice.pdf, page 1, line 3, label Total property tax rate",
        "raw_inputs/notice.pdf, page 1, line 4, label Aggregate ad valorem tax rate",
        "raw_inputs/notice.pdf, page 1, line 5, label Combined millage",
    ]


def test_extract_labelled_property_tax_evidence_preserves_unparseable_location() -> None:
    candidates, locations = extract_labelled_property_tax_evidence(
        "Combined property tax millage: see assessor schedule",
        source="offering_memorandum",
        source_locator="raw_inputs/OM.pdf",
    )

    assert candidates == []
    assert locations == [
        "raw_inputs/OM.pdf, page 1, line 1, "
        "label Combined property tax millage"
    ]


def test_extract_labelled_property_tax_evidence_binds_value_not_year_or_unit_denominator() -> None:
    candidates, locations = extract_labelled_property_tax_evidence(
        """2026 Combined property tax rate per $100: 2.531
2027 Total property tax millage mills per $1,000: 25.31""",
        source="county_tax_notice",
        source_locator="raw_inputs/notice.pdf",
    )

    assert [candidate["millage_rate_mills"] for candidate in candidates] == [
        25.31,
        25.31,
    ]
    assert [candidate["source_locator"] for candidate in candidates] == locations
    assert locations == [
        "raw_inputs/notice.pdf, page 1, line 1, label Combined property tax rate",
        "raw_inputs/notice.pdf, page 1, line 2, label Total property tax millage",
    ]


def test_extract_labelled_property_tax_evidence_accepts_explicit_raw_mills() -> None:
    candidates, locations = extract_labelled_property_tax_evidence(
        """Combined property tax millage: 25.31
Property tax rate per $1,000: 25.31""",
        source="county_tax_notice",
        source_locator="raw_inputs/notice.pdf",
    )

    assert [candidate["millage_rate_mills"] for candidate in candidates] == [
        25.31,
        25.31,
    ]
    assert [candidate["source_locator"] for candidate in candidates] == locations


def test_extract_labelled_property_tax_evidence_does_not_bind_post_label_year() -> None:
    candidates, _locations = extract_labelled_property_tax_evidence(
        "Combined property tax rate 2025: 2.531%",
        source="county_tax_notice",
        source_locator="raw_inputs/notice.pdf",
    )

    assert [candidate["millage_rate_mills"] for candidate in candidates] == [
        25.31
    ]


def test_same_document_conflicting_rates_have_distinct_stable_locations() -> None:
    candidates, locations = extract_labelled_property_tax_evidence(
        """Combined property tax rate: 2.490%
Combined property tax rate: 2.531%""",
        source="offering_memorandum",
        source_locator="raw_inputs/OM.pdf",
    )

    assert [candidate["millage_rate_mills"] for candidate in candidates] == [
        24.9,
        25.31,
    ]
    assert locations == [
        "raw_inputs/OM.pdf, page 1, line 1, label Combined property tax rate",
        "raw_inputs/OM.pdf, page 2, line 1, label Combined property tax rate",
    ]
    assert [candidate["source_locator"] for candidate in candidates] == locations


def test_extract_labelled_property_tax_evidence_ignores_unlabelled_magnitude() -> None:
    candidates, locations = extract_labelled_property_tax_evidence(
        "Annual property tax $413,000. Assessed value $18,500,000. Broker tax 25.31.",
        source="offering_memorandum",
        source_locator="raw_inputs/OM.pdf",
    )

    assert candidates == []
    assert locations == []


def test_parse_broker_om_snapshot_text_handles_berkadia_proforma_fixture() -> None:
    text = (FIXTURE_ROOT / "willow_court_berkadia_proforma.txt").read_text()

    snapshot = parse_broker_om_snapshot_text(text, source_name="Willow Court OM - Berkadia OM.pdf")

    assert snapshot is not None
    assert snapshot["deal_name"] == "Willow Court - Berkadia"
    assert snapshot["address"] == "11 Example Ln, Demoville, ST"
    assert snapshot["year_built"] == 2023
    assert snapshot["unit_count"] == 272
    assert snapshot["trailing_noi"] == 1_554_773.0  # T12 NOI column
    assert snapshot["noi"] == 2_972_498.0
    assert snapshot["revenue_assumptions"]["total_income"] == 5_301_798.0
    assert snapshot["revenue_assumptions"]["utility_reimbursements"] == 433_152.0
    assert snapshot["expense_assumptions"]["real_estate_taxes"] == 509_856.0
    assert snapshot["expense_assumptions"]["insurance"] == 145_444.0
    assert snapshot["property_tax_context"]["local_tax_rate"] == 0.058

    parser_metadata = snapshot["parser_metadata"]
    assert parser_metadata["missing_sections"] == []
