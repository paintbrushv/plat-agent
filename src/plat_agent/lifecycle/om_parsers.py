"""Deterministic OM harvesting utilities.

This module is the rules-based fallback for broker OMs when live extraction is
unavailable. Treat it like a parser library, not an ad hoc regex dump:

- classify/fingerprint likely OM and debt-guidance layouts from extracted text
- parse stable broker tables into structured snapshots
- emit parser metadata so downstream memo/judgment layers know what matched

New broker / template support should be added here with text fixtures and tests,
similar to how rent-roll parser support compounds over time.
"""

from __future__ import annotations

import re
import subprocess
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

GENERIC_OM_PARSER_FAMILY = "generic_table_om_v1"
GENERIC_DEBT_GUIDANCE_FAMILY = "generic_debt_guidance_v1"


def load_pdf_text(path: Path) -> str:
    try:
        completed = subprocess.run(
            ["pdftotext", "-layout", str(path), "-"],
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        return ""
    if completed.returncode != 0:
        return ""
    return completed.stdout or ""


def looks_like_debt_guidance_text(text: str) -> bool:
    lower = text.lower()
    score = sum(
        1
        for token in (
            "proposed financing terms",
            "loan option",
            "max ltv",
            "min dscr",
            "benchmark rate",
            "all-in-rate",
            "all in rate",
            "uw noi",
        )
        if token in lower
    )
    return score >= 3


def looks_like_debt_guidance(path: Path) -> bool:
    text = load_pdf_text(path)
    if not text:
        return False
    return looks_like_debt_guidance_text(text)


def _to_int(value: str | None) -> int | None:
    if value is None:
        return None
    text = str(value).replace(",", "").strip()
    if not text:
        return None
    try:
        return int(float(text))
    except ValueError:
        return None


def _to_float(value: str | None) -> float | None:
    if value is None:
        return None
    text = (
        str(value)
        .replace(",", "")
        .replace("$", "")
        .replace("%", "")
        .replace("(", "-")
        .replace(")", "")
        .strip()
    )
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _extract_currency_values(text: str) -> list[float]:
    return [
        value
        for value in (_to_float(token) for token in re.findall(r"\(?\$[\d,]+\)?", text))
        if value is not None
    ]


def _extract_pct_values(text: str) -> list[float]:
    values: list[float] = []
    for token in re.findall(r"-?\d+(?:\.\d+)?%", text):
        parsed = _to_float(token)
        if parsed is not None:
            values.append(parsed / 100.0)
    return values


def _extract_spaced_label_value(text: str, label: str) -> float | None:
    spaced = r"\s*".join(list(label))
    match = re.search(rf"{spaced}\s+(\d{{4}})", text, re.IGNORECASE)
    if not match:
        return None
    return _to_float(match.group(1))


def _extract_labeled_ints(text: str, label: str, *, min_digits: int = 1, max_digits: int = 6) -> list[int]:
    words = [word for word in label.split() if word]
    if not words:
        return []
    word_patterns = [r"\s*".join(re.escape(ch) for ch in word) for word in words]
    label_pattern = r"\s+".join(word_patterns)
    matches = re.findall(
        rf"{label_pattern}\s+(\d{{{min_digits},{max_digits}}})",
        text,
        re.IGNORECASE,
    )
    values: list[int] = []
    for match in matches:
        parsed = _to_int(match)
        if parsed is not None:
            values.append(parsed)
    return values


def _section_between(text: str, start_label: str, end_label: str | None) -> str:
    start = text.find(start_label)
    if start < 0:
        return ""
    if end_label is None:
        return text[start:]
    end = text.find(end_label, start)
    if end < 0:
        return text[start:]
    return text[start:end]


_PROPERTY_TAX_LABEL = re.compile(
    r"(?:"
    r"(?:combined|total|aggregate)\s+(?:(?:property|ad\s+valorem).*?"
    r"(?:rate|millage)|millage)"
    r"|property\s+tax\s+(?:rate|millage)"
    r")",
    re.IGNORECASE,
)
_DECIMAL_TOKEN = re.compile(r"(?<![\d,])-?\d[\d,]*(?:\.\d+)?")


def _labelled_locator(
    text: str,
    *,
    source_locator: str,
    label: str,
    table: str | None = None,
) -> str:
    for page_number, page_text in enumerate(text.split("\f"), start=1):
        for line_number, raw_line in enumerate(
            page_text.splitlines(), start=1
        ):
            if label.casefold() not in raw_line.casefold():
                continue
            parts = [
                source_locator,
                f"page {page_number}",
                f"line {line_number}",
            ]
            if table:
                parts.append(f"table {table}")
            parts.append(f"label {label}")
            return ", ".join(parts)
    parts = [source_locator, "page 1", "line 1"]
    if table:
        parts.append(f"table {table}")
    parts.append(f"label {label}")
    return ", ".join(parts)


def _positive_decimal_token(token: str) -> Decimal | None:
    try:
        value = Decimal(token.replace(",", ""))
    except InvalidOperation:
        return None
    return value if value.is_finite() and value > 0 else None


def _bound_millage_value(
    tail: str,
    *,
    label: str,
) -> Decimal | None:
    number = r"(-?\d[\d,]*(?:\.\d+)?)"
    unit_patterns: tuple[tuple[str, Decimal], ...] = (
        (rf"{number}\s*%", Decimal("10")),
        (rf"(?:%|percentage\s+points?)\s*[:=]?\s*{number}", Decimal("10")),
        (rf"{number}\s*per\s+\$?100\b", Decimal("10")),
        (rf"per\s+\$?100\b\s*[:=]?\s*{number}", Decimal("10")),
        (rf"{number}\s*(?:as\s+)?decimal\s+rate\b", Decimal("1000")),
        (rf"decimal\s+rate\b\s*[:=]?\s*{number}", Decimal("1000")),
        (
            rf"{number}\s*(?:mills?\s*)?per\s+\$?1,?000\b",
            Decimal("1"),
        ),
        (
            rf"per\s+\$?1,?000\b\s*[:=]?\s*{number}",
            Decimal("1"),
        ),
        (rf"{number}\s*mills?\b", Decimal("1")),
        (rf"\bmills?\b\s*[:=]?\s*{number}", Decimal("1")),
    )
    for pattern, multiplier in unit_patterns:
        match = re.search(pattern, tail, re.IGNORECASE)
        if match is None:
            continue
        value = _positive_decimal_token(match.group(1))
        if value is not None:
            return value * multiplier
    if label.casefold().endswith("millage"):
        values = [
            value
            for match in _DECIMAL_TOKEN.finditer(tail)
            if (value := _positive_decimal_token(match.group(0))) is not None
        ]
        if len(values) == 1:
            return values[0]
    return None


def extract_labelled_property_tax_evidence(
    text: str,
    *,
    source: str,
    source_locator: str,
) -> tuple[list[dict[str, object]], list[str]]:
    """Extract unit-labelled rates with occurrence-specific provenance."""
    candidates: list[dict[str, object]] = []
    locations: list[str] = []
    for page_number, page_text in enumerate(text.split("\f"), start=1):
        for line_number, raw_line in enumerate(
            page_text.splitlines(), start=1
        ):
            line = " ".join(raw_line.split())
            label_match = _PROPERTY_TAX_LABEL.search(line)
            if not line or label_match is None:
                continue
            label = " ".join(label_match.group(0).split())
            locator = (
                f"{source_locator}, page {page_number}, line {line_number}, "
                f"label {label}"
            )
            locations.append(locator)
            tail = line[label_match.end():]
            mills = _bound_millage_value(tail, label=label)
            if mills is None:
                continue
            candidates.append(
                {
                    "millage_rate_mills": float(mills),
                    "source": source,
                    "source_locator": locator,
                }
            )
    return candidates, locations


def _section_between_regex(text: str, start_pattern: str, end_pattern: str | None) -> str:
    start = re.search(start_pattern, text, re.IGNORECASE | re.MULTILINE)
    if not start:
        return ""
    if end_pattern is None:
        return text[start.start() :]
    end = re.search(end_pattern, text[start.end() :], re.IGNORECASE | re.MULTILINE)
    if not end:
        return text[start.start() :]
    return text[start.start() : start.end() + end.start()]


def parse_property_tax_context(text: str) -> dict[str, object] | None:
    section = _section_between(text, "PROPERTY TAXES", "RENT")
    if not section:
        return None
    local_tax_rate = None
    aggregate_match = re.search(
        r"\*Per [^\n]+\s+([0-9]+\.[0-9]+)\s+\*\*Property Account",
        section,
        re.IGNORECASE | re.MULTILINE,
    )
    if aggregate_match:
        parsed = _to_float(aggregate_match.group(1))
        if parsed is not None:
            local_tax_rate = parsed
    if local_tax_rate is None:
        rate_lines = re.findall(r"\n([0-9]+\.[0-9]+)\n", section)
        parsed_rates = [
            value
            for value in (_to_float(rate) for rate in rate_lines)
            if value is not None and value < 1.0
        ]
        if len(parsed_rates) >= 3:
            local_tax_rate = round(sum(parsed_rates) / 100.0, 8)
    if local_tax_rate is None:
        return None
    property_account_match = re.search(
        r"Property Account #:\s*([0-9]+)", section, re.IGNORECASE
    )
    return {
        "local_tax_rate": local_tax_rate,
        "property_account": property_account_match.group(1) if property_account_match else None,
    }


def parse_property_facts_block(text: str) -> dict[str, object] | None:
    asset_summary = _parse_berkadia_asset_summary(text)
    if asset_summary:
        return asset_summary

    start = text.find("A DDRES S")
    snippet = text[start : start + 1200] if start >= 0 else text[:2400]
    lines = [line.strip() for line in snippet.splitlines() if line.strip()]
    for idx, line in enumerate(lines):
        if not re.search(r"\d{2,5} .*?,\s*[A-Z]{2},?\s*\d{5}", line):
            continue
        address = line
        tail = lines[idx : idx + 10]
        year_built = None
        unit_count = None
        for j, candidate in enumerate(tail):
            compact = candidate.upper().replace(" ", "")
            if year_built is None and "YEAR" in compact and j + 1 < len(tail):
                year_built = _to_int(tail[j + 1])
            if unit_count is None and "TOTALUNITS" in compact and j + 1 < len(tail):
                unit_count = _to_int(tail[j + 1])
            # Some OMs list address / county / year on consecutive lines without labels.
            if year_built is None:
                parsed_year = _to_int(candidate)
                if parsed_year is not None and 1900 <= parsed_year <= 2100:
                    year_built = parsed_year
        result: dict[str, object] = {"address": address}
        if year_built and 1900 <= year_built <= 2100:
            result["year_built"] = year_built
        if unit_count and unit_count >= 10:
            result["unit_count"] = unit_count
        if len(result) > 1:
            return result
    return None


def _parse_berkadia_asset_summary(text: str) -> dict[str, object] | None:
    match = re.search(
        r"Property Description\s+(?P<name>[^\n]+)\s+(?P<address>\d{2,6}\s+[^\n|]+)\|\s*(?P<city>[^\n]+?)\s+ASSET SUMMARY(?P<section>.*?)(?:\n\s*Unit Mix|\n\s*PROPERTY OVERVIEW|\f)",
        text,
        re.IGNORECASE | re.DOTALL,
    )
    if not match:
        return None
    section = match.group("section")
    result: dict[str, object] = {
        "address": " ".join(f"{match.group('address').strip()}, {match.group('city').strip()}".split())
    }
    built = re.search(r"\bBuilt\s+((?:19|20)\d{2})\b", section, re.IGNORECASE)
    if built:
        result["year_built"] = int(built.group(1))
    units = re.search(r"\bUnits\s+(\d{2,4})\b", section, re.IGNORECASE)
    if units:
        result["unit_count"] = int(units.group(1))
    tax_millage = re.search(r"\bTax Millage Rate\s*\([^)]*\)\s+([0-9.]+)", section, re.IGNORECASE)
    if tax_millage:
        result["tax_millage_rate"] = float(tax_millage.group(1))
    return result if len(result) > 1 else None


def _extract_money_columns(line: str) -> list[float]:
    return [
        value
        for value in (
            _to_float(token)
            for token in re.findall(r"\(?\$[\d,]+(?:\.\d+)?\)?", line)
        )
        if value is not None
    ]


def _parse_berkadia_proforma_table(text: str) -> tuple[dict[str, float], dict[str, float], dict[str, str]]:
    section = _section_between_regex(
        text,
        r"^\s*Pro Forma\s*$",
        r"^\s*Income Notes\b",
    )
    if not section or "TOTAL OPERATING INCOME" not in section or "NET OPERATING INCOME" not in section:
        return {}, {}, {}

    revenue_labels = {
        "SCHEDULED RENT": "gross_scheduled_market_rent",
        "Less: Loss-to-Lease": "gain_loss_to_lease",
        "Less: Vacancy": "vacancy_loss",
        "Less: Concessions": "concessions",
        "Less: Bad Debt": "bad_debt",
        "Less: Model Units": "non_revenue_units",
        "NET RENTAL INCOME": "net_rental_income",
        "Plus: Fee Income": "fee_income",
        "Plus: Water/Sewer/Trash/Pest/Bulk Internet Income": "utility_reimbursements",
        "Plus: Valet Trash Income": "valet_trash_income",
        "Plus: Resident Insurance": "resident_insurance_income",
        "Plus: Garage Income": "garage_income",
        "Plus: Other Income": "other_income",
        "TOTAL OPERATING INCOME": "total_income",
    }
    expense_labels = {
        "Administrative": "general_administrative",
        "Bulk Internet Expense": "bulk_internet_expense",
        "Advertising & Promotion": "marketing",
        "Payroll": "payroll",
        "Repairs & Maintenance/Turnover": "repairs_maintenance",
        "Grounds & Landscaping": "grounds_landscaping",
        "Management Fee": "management_fees",
        "Utilities (Electric/Gas)": "utilities_electric_gas",
        "Utilities (Water/Sewer)": "utilities_water_sewer",
        "Pest Control/Trash": "pest_control_trash",
        "Real Estate Taxes": "real_estate_taxes",
        "Forced Place Insurance": "forced_place_insurance",
        "Insurance": "insurance",
        "Replacement Reserve": "replacement_reserves",
        "TOTAL EXPENSES": "total_expenses_before_reserves",
        "NET OPERATING INCOME": "noi",
    }

    revenue: dict[str, float] = {}
    expense: dict[str, float] = {}
    notes: dict[str, str] = {}
    for raw_line in section.splitlines():
        line = " ".join(raw_line.split())
        if not line:
            continue
        values = _extract_money_columns(line)
        if not values:
            continue
        for label, key in revenue_labels.items():
            if line.startswith(label) and len(values) >= 4:
                revenue[key] = abs(values[0]) if key in {"vacancy_loss", "concessions", "bad_debt", "non_revenue_units"} else values[0]
                notes[f"{key}_t3_stabilized"] = f"${values[1]:,.0f}"
                notes[f"{key}_t3_annualized"] = f"${values[2]:,.0f}"
                notes[f"{key}_t12"] = f"${values[3]:,.0f}"
                break
        else:
            for label, key in expense_labels.items():
                if not line.startswith(label):
                    continue
                annual_values = values[1:5] if len(values) >= 5 else values[:4]
                if len(annual_values) >= 4:
                    expense[key] = annual_values[0]
                    notes[f"{key}_t3_expenses"] = f"${annual_values[2]:,.0f}"
                    notes[f"{key}_t12_expenses"] = f"${annual_values[3]:,.0f}"
                break

    if "utilities_electric_gas" in expense or "utilities_water_sewer" in expense:
        expense["utilities"] = round(
            expense.get("utilities_electric_gas", 0.0)
            + expense.get("utilities_water_sewer", 0.0),
            2,
        )
    if "garage_income" in revenue:
        revenue["parking_storage_income"] = revenue["garage_income"]
    if revenue or expense:
        notes["broker_table_layout"] = "berkadia_proforma_v1"
    return revenue, expense, notes


def _om_parser_metadata(text: str, *, unit_count: int | None, trailing_noi: float | None, proforma_noi: float | None) -> dict[str, Any]:
    matched_sections: list[str] = []
    if ("INCOME:" in text and "EXPENSES:" in text) or re.search(r"^\s*INCOME\s*$", text, re.IGNORECASE | re.MULTILINE):
        matched_sections.extend(["income_table", "expense_table"])
    if re.search(r"Net Operating Income", text, re.IGNORECASE):
        matched_sections.append("noi_table")
    if "PROPERTY TAXES" in text or re.search(r"Tax Millage Rate", text, re.IGNORECASE):
        matched_sections.append("property_tax_section")
    if unit_count is not None or "TOTAL UNITS" in text:
        matched_sections.append("property_facts")
    if trailing_noi is not None or proforma_noi is not None:
        matched_sections.append("broker_noi")

    all_sections = {
        "income_table",
        "expense_table",
        "noi_table",
        "property_tax_section",
        "property_facts",
        "broker_noi",
    }
    return {
        "parser_family": GENERIC_OM_PARSER_FAMILY,
        "matched_sections": matched_sections,
        "missing_sections": sorted(all_sections - set(matched_sections)),
    }


def parse_broker_om_snapshot_text(text: str, *, source_name: str = "unknown.pdf") -> dict[str, object] | None:
    if not text:
        return None

    income_section = _section_between(text, "INCOME:", "EXPENSES:")
    expense_section = _section_between(text, "EXPENSES:", "Net Operating Income")
    noi_section = _section_between(text, "Net Operating Income", "REVENUES:")
    tax_context = parse_property_tax_context(text)
    if tax_context and tax_context.get("local_tax_rate") is not None:
        tax_context["property_tax_evidence_candidates"] = [
            {
                "millage_rate_mills": float(
                    Decimal(str(tax_context["local_tax_rate"]))
                    * Decimal("1000")
                ),
                "source": "offering_memorandum",
                "source_locator": _labelled_locator(
                    text,
                    source_locator=f"raw_inputs/{Path(source_name).name}",
                    label="TAX RATE PER $100",
                    table="PROPERTY TAXES",
                ),
            }
        ]
    property_facts = parse_property_facts_block(text) or {}

    year_built = _extract_spaced_label_value(text, "YEARBUILT")
    if year_built is None:
        year_candidates = [
            value
            for value in _extract_labeled_ints(text, "YEAR BUILT", min_digits=4, max_digits=4)
            if 1900 <= value <= 2100
        ]
        if year_candidates:
            year_built = float(year_candidates[0])
    if year_built is None and property_facts.get("year_built") is not None:
        year_built = float(property_facts["year_built"])

    unit_count_candidates = _extract_labeled_ints(text, "TOTAL UNITS", min_digits=2, max_digits=4)
    unit_count = max(unit_count_candidates) if unit_count_candidates else None
    if property_facts.get("unit_count") is not None:
        unit_count = max(unit_count or 0, int(property_facts["unit_count"]))
    direct_unit_count = re.search(r"\b([1-9]\d{1,3})\s*-\s*unit multifamily\b", text, re.IGNORECASE)
    if direct_unit_count:
        direct_count = _to_int(direct_unit_count.group(1))
        if direct_count is not None:
            unit_count = max(unit_count or 0, direct_count)

    trailing_noi = None
    proforma_noi = None
    if noi_section:
        noi_amounts = _extract_currency_values(noi_section)
        if len(noi_amounts) >= 9:
            trailing_noi = noi_amounts[0]
            proforma_noi = noi_amounts[6]

    revenue_assumptions: dict[str, float] = {}
    underwriting_notes: dict[str, str] = {}
    berkadia_revenue, berkadia_expenses, berkadia_notes = _parse_berkadia_proforma_table(text)
    if berkadia_revenue:
        revenue_assumptions.update(berkadia_revenue)
        underwriting_notes.update(berkadia_notes)
        if proforma_noi is None and "noi" in berkadia_expenses:
            proforma_noi = berkadia_expenses["noi"]
        t12_note = berkadia_notes.get("noi_t12_expenses")
        if trailing_noi is None and t12_note:
            trailing_noi = _to_float(t12_note)
    if income_section:
        income_amounts = _extract_currency_values(income_section)
        income_pcts = _extract_pct_values(income_section)
        if len(income_amounts) >= 58:
            revenue_assumptions = {
                "gross_scheduled_market_rent": income_amounts[8],
                "renovation_unit_premiums": income_amounts[9],
                "gain_loss_to_lease": income_amounts[10],
                "gross_potential_rent": income_amounts[11],
                "vacancy_loss": abs(income_amounts[26]),
                "concessions": abs(income_amounts[27]),
                "non_revenue_units": abs(income_amounts[28]),
                "bad_debt": abs(income_amounts[29]),
                "net_rental_income": income_amounts[30],
                "utility_reimbursements": income_amounts[47],
                "parking_storage_income": income_amounts[48],
                "internet_income": income_amounts[49],
                "other_income": income_amounts[50],
                "total_income": income_amounts[51],
                "monthly_collections": income_amounts[52],
            }
        if len(income_pcts) >= 31:
            underwriting_notes.update(
                {
                    "vacancy": f"{income_pcts[20] * 100:.2f}% of gross potential rent",
                    "concessions": f"{income_pcts[21] * 100:.2f}% of gross potential rent",
                    "non_revenue_units": f"{income_pcts[22] * 100:.2f}% of gross potential rent",
                    "bad_debt": f"{income_pcts[23] * 100:.2f}% of gross potential rent",
                }
            )
        if len(income_amounts) >= 58:
            underwriting_notes.update(
                {
                    "utility_reimbursements": f"${income_amounts[53]:,.0f}/unit",
                    "parking_storage_income": f"${income_amounts[54]:,.0f}/unit",
                    "internet_income": f"${income_amounts[55]:,.0f}/unit",
                    "other_income": f"${income_amounts[56]:,.0f}/unit",
                }
            )

    expense_assumptions: dict[str, float] = {}
    if berkadia_expenses:
        expense_assumptions.update(berkadia_expenses)
    if expense_section:
        expense_amounts = _extract_currency_values(expense_section)
        expense_labels = [
            "utilities",
            "repairs_maintenance",
            "make_ready",
            "contract_services",
            "marketing",
            "payroll",
            "general_administrative",
            "real_estate_taxes",
            "franchise_tax",
            "insurance",
            "management_fees",
            "miscellaneous",
            "total_expenses_before_reserves",
        ]
        if len(expense_amounts) >= 39:
            proforma_expenses = expense_amounts[26:39]
            expense_assumptions = dict(zip(expense_labels, proforma_expenses, strict=False))
            per_unit = expense_amounts[39:52]
            if len(per_unit) == len(expense_labels):
                underwriting_notes.update(
                    {
                        label: f"${value:,.0f}/unit"
                        for label, value in zip(expense_labels, per_unit, strict=False)
                        if label != "total_expenses_before_reserves"
                    }
                )

    payroll_note = re.search(
        r"WDIS Proforma assumes \$?([\d,]+)\/unit for Payroll",
        text,
        re.IGNORECASE,
    )
    if payroll_note:
        underwriting_notes["payroll"] = f"${payroll_note.group(1)}/unit"
    reserves_note = re.search(
        r"Replacement Reserves of \$?([\d,]+)\/unit",
        text,
        re.IGNORECASE,
    )
    if reserves_note:
        underwriting_notes["replacement_reserves"] = f"${reserves_note.group(1)}/unit"
        reserve_per_unit = _to_float(reserves_note.group(1))
        units = float(unit_count) if unit_count else None
        if reserve_per_unit is not None and units:
            expense_assumptions["replacement_reserves"] = reserve_per_unit * units
    if tax_context and tax_context.get("local_tax_rate") is not None:
        underwriting_notes["tax_rate"] = f"{float(tax_context['local_tax_rate']) * 100:.4f}%"
    elif property_facts.get("tax_millage_rate") is not None:
        millage = float(property_facts["tax_millage_rate"])
        tax_context = {
            "local_tax_rate": millage / 1000.0,
            "property_account": None,
            "full_value_reassessment_supported": False,
            "source": "asset_summary_tax_millage_rate",
        }
        underwriting_notes["tax_rate"] = f"{millage:.4f} mills"

    if not any([year_built, revenue_assumptions, expense_assumptions, trailing_noi, proforma_noi, tax_context]):
        return None

    snapshot: dict[str, object] = {
        "deal_name": Path(source_name).stem.replace(" OM", ""),
        "source": Path(source_name).name,
        "address": property_facts.get("address"),
        "year_built": int(year_built) if year_built is not None else None,
        "unit_count": unit_count,
        "trailing_noi": trailing_noi,
        "noi": proforma_noi,
        "revenue_assumptions": revenue_assumptions,
        "expense_assumptions": expense_assumptions,
        "underwriting_notes": underwriting_notes,
        "parser_metadata": _om_parser_metadata(
            text,
            unit_count=unit_count,
            trailing_noi=trailing_noi,
            proforma_noi=proforma_noi,
        ),
    }
    if tax_context:
        snapshot["property_tax_context"] = tax_context
    return snapshot


def parse_broker_om_snapshot(path: Path) -> dict[str, object] | None:
    text = load_pdf_text(path)
    return parse_broker_om_snapshot_text(text, source_name=path.name)
