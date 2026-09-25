"""Lifecycle Step 1 — Intake.

Dispatches to the `deal-intake` agent in the multifamily-underwriting
sibling repo. The agent classifies docs in raw_inputs/ and writes
canonical_deal.json + manifest.md + intake_punchlist.md. This module is
the lifecycle ADAPTER: it builds the BridgeRequestV1 payload, parses
BridgeResponseV1 back into typed domain objects, maps blockers into the
run-level punchlist, verifies artifacts exist, and writes the cache-
validity sidecars (_provenance.json + _complete).

Spec: docs/superpowers/specs/2026-05-05-deal-lifecycle-design.md §2.1 + §3 Step 1.
"""

from __future__ import annotations

import csv
import json
import re
import subprocess
import shutil
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import ClassVar

from pydantic import ValidationError

from plat_agent.contracts.domain.intake import DealIntakeRequest, DealIntakeResponse
from plat_agent.contracts.envelope import ArtifactRef, BridgeError, BridgeRequestV1, BridgeResponseV1
from plat_agent.dispatch.sibling import SiblingRepo, dispatch_sibling_agent as _dispatch_sibling_agent
from plat_agent.lifecycle.atomic import atomic_write_json, atomic_write_text
from plat_agent.lifecycle.cache import (
    compute_input_hash,
    is_satisfied as _cache_is_satisfied,
    write_provenance,
)
from plat_agent.lifecycle.complete_marker import write_complete_marker
from plat_agent.lifecycle.defaults import CONTRACT_VERSION
from plat_agent.lifecycle.om_parsers import (
    extract_labelled_property_tax_evidence as _extract_labelled_property_tax_evidence,
    load_pdf_text as _shared_load_pdf_text,
    looks_like_debt_guidance as _shared_looks_like_debt_guidance,
    parse_broker_om_snapshot as _shared_parse_broker_om_snapshot,
    parse_property_facts_block as _shared_parse_property_facts_block,
    parse_property_tax_context as _shared_parse_property_tax_context,
)
from plat_agent.lifecycle.protocol import StepResult
from plat_agent.lifecycle.punchlist import (
    read_punchlist_json,
    write_punchlist_json,
    write_punchlist_markdown,
)
from plat_agent.lifecycle.state import BlockerItem, LifecycleState


# Constant: name of the sibling repo on the SiblingRepo.from_env_or_default contract.
# Resolves PLAT_MULTIFAMILY_UNDERWRITING_PATH env var, then falls back to
# ../multifamily-underwriting relative to plat-agent's own root.
MFU_SIBLING_NAME = "multifamily-underwriting"
MARKET_STUDY_SIBLING_NAME = "market-study-agent"

# Lifecycle-local artifact filenames. Note: per the deal-intake.md sibling
# contract, the agent emits the manifest at the deal root as `deal_manifest.md`
# and the canonical JSON wherever ingest_deal.py decided (typically
# `runs/deals/<slug>/standardized/canonical_deal.json`). We mirror BOTH into
# `outputs/<run_id>/intake/` after a successful dispatch so downstream steps
# (judgment, memo, CRM) can read from a stable run-scoped location.
LIFECYCLE_INTAKE_ARTIFACTS = ("canonical_deal.json", "manifest.md")

BOX_SCORE_FILENAME_TOKENS = (
    "box score",
    "boxscore",
    "unit mix summary",
    "availability report",
)
DEBT_GUIDANCE_FILENAME_TOKENS = (
    "debt guidance",
    "debt matrix",
    "debtmatrix",
    "term sheet",
    "termsheet",
    "loan quote",
    "loan information",
    "loan terms",
    "terms",
    "financing terms",
    "debt quote",
    "financing",
    "agency quote",
    "lender quote",
    "cm quote",
)


def _classify_document_type(path: Path) -> str:
    stem = re.sub(r"[^a-z0-9]+", " ", path.stem.lower())
    suffix = path.suffix.lower()
    spreadsheet = {".xlsx", ".xls", ".csv"}
    office_doc = {".docx", ".doc"}

    if suffix == ".pdf" and any(
        token in stem
        for token in (
            "commercial rent roll",
            "retail rent roll",
            "tenant schedule",
            "lease schedule",
            "loi",
        )
    ):
        return "commercial_rent_roll"
    if suffix == ".pdf" and (
        any(token in stem for token in ("offering", "memorandum", "broker", "final"))
        or re.search(r"(^| )om($| )", stem) is not None
        or ("pack" in stem and "norman" in stem)
    ):
        return "om"
    if (suffix in spreadsheet or suffix == ".pdf") and any(token in stem for token in BOX_SCORE_FILENAME_TOKENS):
        return "box_score"
    if (suffix in spreadsheet or suffix == ".pdf") and (
        "rent roll" in stem
        or "rentroll" in path.stem.lower()
        or re.search(r"(^| )rr($| )", stem) is not None
    ):
        return "rent_roll"
    if (suffix in spreadsheet or suffix == ".pdf") and (
        "concession" in stem
        or "burn off" in stem
        or "burnoff" in path.stem.lower()
    ):
        return "concession_support"
    if (suffix in spreadsheet or suffix == ".pdf") and (
        "rentable items" in stem
        or "assignable items" in stem
        or "parking" in stem
        or "garage" in stem
        or "storage" in stem
    ):
        return "rentable_items"
    if (suffix in spreadsheet or suffix == ".pdf") and any(
        token in stem
        for token in (
            "t12",
            "t 12",
            "trailing 12",
            "12 month recap",
            "income statement",
            "income statment",
            "profit loss",
            "p l",
            "financial",
            "operating",
        )
    ):
        return "t12"
    if suffix in spreadsheet and (
        any(token in stem for token in ("aged del", "delinquency", "deleinquency", "delinquent", "receivable", "accounts receivable"))
        or re.search(r"(^| )ar($| )", stem) is not None
    ):
        return "delinquency_report"
    if suffix == ".pdf" and any(token in stem for token in ("statement", "servicer loan", "rate cap", "sofr", "loan details")):
        return "existing_debt"
    if suffix in spreadsheet and any(token in stem for token in ("loan details", "existing loan", "current loan")):
        return "existing_debt"
    if suffix == ".pdf" and any(token in stem for token in DEBT_GUIDANCE_FILENAME_TOKENS):
        return "debt_guidance"
    if suffix == ".pdf" and _looks_like_debt_guidance(path):
        return "debt_guidance"
    if suffix == ".pdf" and any(token in stem for token in ("insurance", "indication", "premium", "lossrun", "loss run")):
        return "insurance_quote"
    if suffix in {".pdf", *spreadsheet} and any(token in stem for token in ("vacancy", "occupancy")):
        return "occupancy_support"
    if suffix == ".pdf" and "flyer" in stem:
        return "marketing_flyer"
    if suffix in {".pdf", *office_doc, *spreadsheet} and any(
        token in stem for token in ("capex", "capital expense", "capital expenses", "quote", "bid", "improvement", "improvements")
    ):
        return "capex_quote"
    if suffix == ".pdf" and any(token in stem for token in ("tax", "assessment", "appraisal", "valorem")):
        return "tax_doc"
    if suffix == ".pdf" and "costar" in stem:
        return "market_data"
    if suffix == ".md" and any(token in stem for token in ("source onedrive inventory", "onedrive inventory", "deal inventory")):
        return "source_inventory"
    if suffix == ".pdf" and any(token in stem for token in ("title", "commitment")):
        return "title_doc"
    if suffix == ".pdf" and "survey" in stem:
        return "survey"
    if suffix == ".pdf" and any(
        token in stem
        for token in (
            "electricity",
            "trash",
            "republic services",
            "spectrum",
            "chiller",
            "maintenance agreement",
            "service contract",
            "contract",
        )
    ):
        return "service_contract"
    if suffix == ".pdf" and any(token in stem for token in ("delinquency", "deleinquency", "delinquent", "receivable")):
        return "delinquency_report"
    if suffix in spreadsheet and _looks_like_box_score(path):
        return "box_score"
    return "unknown"


def _dated_document_sort_key(path: Path) -> tuple[int, int, int, int, float]:
    """Sort key for duplicate document types, preferring the newest file.

    Brokers often post multiple rent rolls or T12s in the same data room. The
    stale file can parse cleanly enough to poison intake, so prefer an explicit
    date, then an analyst-cleaned copy for that date, before mtime ordering.
    """
    stem = path.stem.lower()
    candidates: list[tuple[int, int, int]] = []
    for month, day, year in re.findall(r"(?<!\d)(\d{1,2})[._-](\d{1,2})[._-](\d{2,4})(?!\d)", stem):
        y = int(year)
        if y < 100:
            y += 2000
        candidates.append((y, int(month), int(day)))
    for year, month, day in re.findall(r"(?<!\d)(20\d{2})[._-](\d{1,2})[._-](\d{1,2})(?!\d)", stem):
        candidates.append((int(year), int(month), int(day)))
    dated = max(candidates) if candidates else (0, 0, 0)
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = 0.0
    return (*dated, int(bool(candidates) and "cleaned" in stem), mtime)


def _select_document_by_type(classified: dict[str, list[Path]]) -> dict[str, Path]:
    selected: dict[str, Path] = {}
    for kind, paths in classified.items():
        if not paths:
            continue
        selected[kind] = max(paths, key=_dated_document_sort_key)
    return selected


def _parser_ready_rent_roll_path(deal_root: Path, selected_path: Path) -> Path:
    """Return a parser-ready rent roll, using staged fallbacks for source PDFs.

    Scanned/image PDF rent rolls are valid source evidence but the deterministic
    ingestion parser accepts CSV/Excel. When an analyst-created or deterministic
    OM-unit-mix fallback has been staged under `standardized/`, use that file
    for ingestion while preserving the PDF in the manifest as source evidence.
    """
    if selected_path.suffix.lower() != ".pdf":
        return selected_path

    standardized = deal_root / "standardized"
    if not standardized.is_dir():
        return selected_path

    candidates = sorted(
        (
            path
            for path in standardized.iterdir()
            if path.is_file()
            and path.suffix.lower() in {".csv", ".xlsx", ".xls", ".xlsm"}
            and "rent_roll" in path.stem.lower()
        ),
        key=lambda path: path.name.lower(),
    )
    return candidates[0] if candidates else selected_path


def _load_pdf_text(path: Path) -> str:
    return _shared_load_pdf_text(path)


def _load_pdf_layout_text(path: Path) -> str:
    pdftotext = shutil.which("pdftotext")
    if not pdftotext:
        return _load_pdf_text(path)
    completed = subprocess.run(
        [pdftotext, "-layout", str(path), "-"],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode == 0 and completed.stdout:
        return completed.stdout
    return _load_pdf_text(path)


def _looks_like_yardi_lease_charges(path: Path) -> bool:
    rows = _read_spreadsheet_preview(path, max_rows=6, max_cols=4)
    if not rows:
        return False
    first = " ".join(cell for row in rows[:2] for cell in row if cell).lower()
    return "rent roll with lease charges" in first


def _looks_like_debt_guidance(path: Path) -> bool:
    return _shared_looks_like_debt_guidance(path)


def _read_spreadsheet_preview(path: Path, max_rows: int = 12, max_cols: int = 12) -> list[list[str]]:
    suffix = path.suffix.lower()
    if suffix == ".csv":
        try:
            rows: list[list[str]] = []
            for idx, line in enumerate(path.read_text(errors="ignore").splitlines()):
                if idx >= max_rows:
                    break
                rows.append([cell.strip() for cell in line.split(",")[:max_cols]])
            return rows
        except OSError:
            return []
    try:
        from openpyxl import load_workbook
    except Exception:
        return []
    try:
        wb = load_workbook(path, read_only=True, data_only=True)
    except Exception:
        return []
    ws = wb[wb.sheetnames[0]]
    rows = []
    for idx, row in enumerate(ws.iter_rows(min_row=1, max_row=max_rows, values_only=True)):
        if idx >= max_rows:
            break
        values = []
        for cell in row[:max_cols]:
            if cell is None:
                values.append("")
            else:
                values.append(str(cell).strip())
        rows.append(values)
    return rows


def _looks_like_box_score(path: Path) -> bool:
    rows = _read_spreadsheet_preview(path)
    if not rows:
        return False
    flat = " ".join(" ".join(row).lower() for row in rows)
    if "boxscore summary" in flat or "box score summary" in flat:
        return True
    header_hits = [
        "avg sq ft" in flat or "avg. sq ft." in flat,
        "avg rent" in flat or "avg. rent" in flat,
        "occupied no notice" in flat,
        "vacant unrented" in flat,
        "resident activity" in flat,
    ]
    return sum(1 for hit in header_hits if hit) >= 3


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


def _parse_property_tax_context(text: str) -> dict[str, object] | None:
    return _shared_parse_property_tax_context(text)


def _parse_property_facts_block(text: str) -> dict[str, int] | None:
    return _shared_parse_property_facts_block(text)


def _parse_broker_om_snapshot(path: Path) -> dict[str, object] | None:
    return _shared_parse_broker_om_snapshot(path)


def _parse_box_score(path: Path) -> dict[str, object] | None:
    if path.suffix.lower() == ".pdf":
        return _parse_resman_pdf_box_score(path)
    rows = _read_spreadsheet_preview(path, max_rows=40, max_cols=12)
    if not rows:
        return None
    summary: dict[str, object] = {
        "source_file": path.name,
        "floorplans": [],
    }
    if rows and rows[0] and rows[0][0]:
        summary["report_title"] = rows[0][0]
    if len(rows) > 1 and rows[1] and rows[1][0]:
        summary["property_name"] = rows[1][0]
    if len(rows) > 2 and rows[2] and rows[2][0]:
        summary["date_range"] = rows[2][0]

    for idx, row in enumerate(rows):
        first = (row[0] or "").strip().lower()
        if first != "code":
            continue
        if len(row) < 11:
            continue
        for data_row in rows[idx + 1 :]:
            code = (data_row[0] or "").strip()
            label = (data_row[1] or "").strip()
            if (code.lower() == "resident activity") or (label.lower() == "resident activity"):
                break
            if code.lower() == "total" or label.lower() == "total":
                total_row = data_row
                if not code and label.lower() == "total":
                    summary["total_units"] = _to_int(total_row[4])
                    summary["available_units"] = _to_int(total_row[10])
                    summary["model_units"] = _to_int(total_row[11])
                else:
                    summary["total_units"] = _to_int(total_row[3])
                    summary["available_units"] = _to_int(total_row[9])
                    summary["model_units"] = _to_int(total_row[10])
                break
            if not code:
                continue
            summary["floorplans"].append(
                {
                    "code": code,
                    "name": (data_row[1] or "").strip(),
                    "avg_sqft": _to_float(data_row[2]),
                    "avg_rent": _to_float(data_row[3]),
                    "units": _to_int(data_row[4]),
                    "available_units": _to_int(data_row[10]),
                    "model_units": _to_int(data_row[11]),
                }
            )
        break
    if not summary["floorplans"]:
        return None
    return summary


def _parse_resman_pdf_box_score(path: Path) -> dict[str, object] | None:
    text = _load_pdf_layout_text(path)
    if "box score" not in text.lower() and "occupancy" not in text.lower():
        return None
    section = _section_between(text, "Occupancy", "Applications and Renewals") or text
    floorplans: list[dict[str, object]] = []
    total_units = None
    available_units = None
    lines = section.splitlines()
    idx = 0
    while idx < len(lines):
        line = lines[idx]
        stripped = line.strip()
        wrapped = re.match(r"^([ABC]\dU\s*-)$", stripped, re.IGNORECASE)
        if wrapped and idx + 2 < len(lines):
            suffix = lines[idx + 2].strip()
            if re.match(r"^\d[xX]\d$", suffix):
                line = f"{wrapped.group(1)}{suffix}        {lines[idx + 1].strip()}"
                idx += 3
            else:
                idx += 1
        else:
            idx += 1
        parts = re.split(r"\s{2,}", line.strip())
        if len(parts) < 3:
            continue
        code = parts[0].strip()
        if re.match(r"^[ABC]\dU\s*-$", code, re.IGNORECASE):
            suffix = {"A": "1x1", "B": "2x1", "C": "3X2"}.get(code[0].upper(), "")
            code = f"{code}{suffix}".replace("-2", "- 2").replace("-1", "- 1").replace("-3", "- 3")
        if not re.match(r"^(?:[A-Z]\d[A-Z]?\s*-?\s*\d[xX]\d|Total)$", code):
            continue
        units = _to_int(parts[1])
        if units is None:
            continue
        vacant = None
        percent_idx = next((idx for idx, value in enumerate(parts) if "%" in value), None)
        if percent_idx is not None and percent_idx + 1 < len(parts):
            vacant = _to_int(parts[percent_idx + 1])
        if code.lower() == "total":
            total_units = units
            available_units = vacant
            continue
        if units <= 0:
            continue
        floorplans.append(
            {
                "code": code,
                "name": code,
                "avg_sqft": None,
                "avg_rent": None,
                "units": units,
                "available_units": vacant,
                "model_units": None,
            }
        )
    if not floorplans:
        return None
    return {
        "source_file": path.name,
        "report_title": "Box Score",
        "summary_type": "resman_pdf_occupancy",
        "floorplans": floorplans,
        "total_units": total_units or sum(int(row.get("units") or 0) for row in floorplans),
        "available_units": available_units,
        "model_units": None,
    }


def _pick_hold_matched_option(options: list[dict[str, object]], hold_years: int | None) -> dict[str, object] | None:
    if not options:
        return None
    if hold_years is None:
        for option in options:
            label = str(option.get("loan_option", "")).lower()
            if "5 year" in label and "agency" in label:
                return option
        return options[0]
    target = str(hold_years)
    for option in options:
        label = str(option.get("loan_option", "")).lower()
        if f"{target} year" in label and "agency" in label:
            return option
    return options[0]


def _parse_debt_guidance(path: Path, *, start_period: str | None, end_period: str | None) -> dict[str, object] | None:
    text = _load_pdf_text(path)
    if text and "loan option" not in text.lower() and "terms" in path.stem.lower():
        try:
            fallback = subprocess.run(
                ["pdftotext", str(path), "-"],
                capture_output=True,
                text=True,
            )
        except FileNotFoundError:
            fallback = None
        if fallback is not None and fallback.returncode == 0 and fallback.stdout:
            text = fallback.stdout
    if not text:
        return None
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    joined = "\n".join(lines)
    row_labels = [
        "Loan Option",
        "Rate Type",
        "Total Loan Amount",
        "Max LTV",
        "Min DSCR",
        "U W NOI",
        "Benchmark",
        "Benchmark Rate",
        "Benchmark Floor",
        "Min DY",
        "Spread",
        "All-in-Rate",
        "Interest Rate Protection",
        "Required Strike Rate",
    ]
    label_to_values: dict[str, list[str]] = {}
    label_positions = [
        (idx, line) for idx, line in enumerate(lines) if line in row_labels
    ]
    for pos, (idx, label) in enumerate(label_positions):
        next_idx = label_positions[pos + 1][0] if pos + 1 < len(label_positions) else len(lines)
        label_to_values[label] = lines[idx + 1 : next_idx]

    option_names = [
        value
        for value in label_to_values.get("Loan Option", [])
        if any(token in value.lower() for token in ("agency", "freddie", "cmbs", "bridge"))
    ]
    options = []
    amounts = label_to_values.get("Total Loan Amount", [])
    ltvs = label_to_values.get("Max LTV", [])
    dscrs = label_to_values.get("Min DSCR", [])
    benchmarks = label_to_values.get("Benchmark", [])
    benchmark_rates = label_to_values.get("Benchmark Rate", [])
    spreads = label_to_values.get("Spread", [])

    if option_names:
        try:
            option_start = lines.index("Loan Option") + 1
            max_ltv_idx = lines.index("Max LTV")
        except ValueError:
            option_start = None
            max_ltv_idx = None
        if option_start is not None and max_ltv_idx is not None:
            unlabeled_amounts = [
                line for line in lines[option_start:max_ltv_idx] if line.startswith("$")
            ]
            if len(unlabeled_amounts) >= len(option_names):
                amounts = unlabeled_amounts[: len(option_names)]

    for idx, option_name in enumerate(option_names):
        spread_low = None
        spread_high = None
        if idx < len(spreads):
            match = re.search(r"([\d.]+)%\s*-\s*([\d.]+)%", spreads[idx])
            if match:
                spread_low = _to_float(match.group(1))
                spread_high = _to_float(match.group(2))
        options.append(
            {
                "loan_option": option_name,
                "loan_amount": _to_float(amounts[idx]) if idx < len(amounts) else None,
                "max_ltv": (_to_float(ltvs[idx]) / 100.0) if idx < len(ltvs) and _to_float(ltvs[idx]) is not None else None,
                "min_dscr": _to_float(dscrs[idx].replace("x", "")) if idx < len(dscrs) else None,
                "benchmark": benchmarks[idx] if idx < len(benchmarks) else None,
                "benchmark_rate": (_to_float(benchmark_rates[idx]) / 100.0) if idx < len(benchmark_rates) and _to_float(benchmark_rates[idx]) is not None else None,
                "spread_low": spread_low / 100.0 if spread_low is not None else None,
                "spread_high": spread_high / 100.0 if spread_high is not None else None,
            }
        )

    hold_years = None
    if start_period and end_period:
        try:
            sy, sm = int(start_period[:4]), int(start_period[5:7])
            ey, em = int(end_period[:4]), int(end_period[5:7])
            hold_years = ((ey - sy) * 12 + (em - sm) + 1) // 12
        except Exception:
            hold_years = None

    uw_noi_match = re.search(r"U W NOI\s+\$?([\d,]+)", joined, re.IGNORECASE)
    parsed = {
        "source_file": path.name,
        "uw_noi": _to_float(uw_noi_match.group(1)) if uw_noi_match else None,
        "options": options,
    }
    selected = _pick_hold_matched_option(options, hold_years)
    if selected is not None:
        parsed["hold_matched_recommendation"] = selected
    if not parsed["uw_noi"] and not parsed["options"]:
        return None
    return parsed


def _append_intake_flag(canonical: dict, flag: str) -> None:
    metadata = canonical.setdefault("metadata", {})
    flags = list(metadata.get("intake_sanity_flags") or [])
    if flag not in flags:
        flags.append(flag)
    metadata["intake_sanity_flags"] = flags


def _read_csv_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _build_house_box_score_from_standardized(
    summary_rows: list[dict[str, str]],
    standardized_rows: list[dict[str, str]],
) -> dict[str, object] | None:
    if not summary_rows:
        return None
    standardized_by_plan: dict[str, list[dict[str, str]]] = {}
    for row in standardized_rows:
        code = (row.get("floorplan_code") or "").strip()
        if not code:
            continue
        standardized_by_plan.setdefault(code, []).append(row)

    floorplans: list[dict[str, object]] = []
    total_units = 0
    for row in summary_rows:
        units = _to_int(row.get("Units"))
        if units in (None, 0):
            continue
        code = (row.get("PlanCode") or "").strip()
        plan_rows = standardized_by_plan.get(code, [])
        occupied_lease_rents = [
            _to_float(plan_row.get("lease_rent"))
            for plan_row in plan_rows
            if (plan_row.get("status") or "").strip().lower() == "occupied"
            and _to_float(plan_row.get("lease_rent")) is not None
        ]
        all_market_rents = [
            _to_float(plan_row.get("market_rent"))
            for plan_row in plan_rows
            if _to_float(plan_row.get("market_rent")) is not None
        ]
        avg_in_place_rent = (
            round(sum(occupied_lease_rents) / len(occupied_lease_rents), 2)
            if occupied_lease_rents
            else None
        )
        avg_market_rent = (
            round(sum(all_market_rents) / len(all_market_rents), 2)
            if all_market_rents
            else _to_float(row.get("AvgMarketRent"))
        )
        bath_counts = [
            _to_float(plan_row.get("bath_count"))
            for plan_row in plan_rows
            if _to_float(plan_row.get("bath_count")) is not None
        ]
        bath_count = (
            max(set(bath_counts), key=bath_counts.count)
            if bath_counts
            else None
        )
        bed_label = (row.get("BedType") or "").strip()
        bed_match = re.match(r"^(\d+)BR$", bed_label, re.IGNORECASE)
        bedroom_count = int(bed_match.group(1)) if bed_match else None

        floorplans.append(
            {
                "code": code or None,
                "name": ((row.get("BedType") or "").strip() or code or None),
                "avg_sqft": _to_float(row.get("SqFt")),
                "avg_rent": avg_in_place_rent if avg_in_place_rent is not None else avg_market_rent,
                "avg_in_place_rent": avg_in_place_rent,
                "avg_market_rent": avg_market_rent,
                "units": units,
                "bedrooms": bedroom_count,
                "bathrooms": bath_count,
                "available_units": None,
                "model_units": None,
            }
        )
        total_units += units
    if not floorplans or total_units <= 0:
        return None
    status_counts: dict[str, int] = {}
    if standardized_rows:
        for key, value in __import__("collections").Counter(
            ((row.get("status") or "").strip() or "Unknown") for row in standardized_rows
        ).items():
            status_counts[str(key)] = int(value)
    return {
        "source_file": "derived_from_market_study_standardized_rent_roll",
        "report_title": "House Box Score",
        "summary_type": "derived",
        "derivation_basis": "market-study-agent standardized rent roll",
        "notes": "Derived from the shared market-study-agent rent-roll standardization path. Prefer this over local parser heuristics when available.",
        "floorplans": floorplans,
        "total_units": total_units,
        "available_units": None,
        "model_units": None,
        "status_counts": status_counts,
    }


def _has_matching_vertical_charge_config(repo_path: Path, rent_roll_path: Path) -> bool:
    config_dir = repo_path / "configs" / "vertical_charges"
    if not config_dir.exists():
        return False
    try:
        import yaml
    except Exception:
        return False
    haystack = re.sub(r"[^a-z0-9]+", " ", rent_roll_path.stem.lower())
    for config_path in config_dir.glob("*.yaml"):
        try:
            config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        except Exception:
            continue
        candidates = [
            config.get("property_name"),
            config.get("property_id"),
            *(config.get("property_aliases") or []),
        ]
        for candidate in candidates:
            needle = re.sub(r"[^a-z0-9]+", " ", str(candidate or "").lower()).strip()
            if needle and needle in haystack:
                return True
    return False


def _matching_rent_roll_mapping_name(
    repo_path: Path,
    rent_roll_path: Path,
) -> str | None:
    """Resolve a property mapping from the canonical deal path when available."""
    parts = rent_roll_path.resolve().parts
    try:
        deals_index = parts.index("deals")
        deal_slug = parts[deals_index + 1]
    except (ValueError, IndexError):
        return None

    def property_key(value: object) -> str:
        key = re.sub(r"[^a-z0-9]+", "", str(value or "").lower())
        return key.removeprefix("the")

    target = property_key(deal_slug)
    config_dir = repo_path / "configs" / "rent_roll_mappings"
    if not target or not config_dir.is_dir():
        return None
    for config_path in sorted(config_dir.glob("*.yaml")):
        candidates: list[object] = [config_path.stem]
        try:
            config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        except Exception:
            config = {}
        candidates.extend([
            config.get("property_id"),
            *(config.get("property_aliases") or []),
        ])
        if any(property_key(candidate) == target for candidate in candidates):
            return config_path.stem
    return None


def _normalize_rent_roll_with_market_study(
    rent_roll_path: Path,
    *,
    output_dir: Path,
) -> dict[str, object] | None:
    repo = SiblingRepo.from_env_or_default(
        MARKET_STUDY_SIBLING_NAME,
        default_relative=MARKET_STUDY_SIBLING_NAME,
    )
    python_bin = repo.path / ".venv" / "bin" / "python"
    parser_script = repo.path / "etl" / "standardize_rent_roll.py"

    attempts: list[tuple[str, Path, list[str]]] = []
    vertical_script = repo.path / "etl" / "parse_vertical_charges.py"
    if vertical_script.exists() and _has_matching_vertical_charge_config(repo.path, rent_roll_path):
        attempts.append((
            "parse_vertical_charges",
            vertical_script,
            [
                str(python_bin if python_bin.exists() else Path(sys.executable)),
                str(vertical_script),
                str(rent_roll_path),
                "--auto-detect",
                "--output",
                str(output_dir),
                "--summary",
            ],
        ))

    mapping_name = _matching_rent_roll_mapping_name(repo.path, rent_roll_path)
    mapping_args = (
        ["--config", mapping_name]
        if mapping_name
        else ["--auto-detect"]
    )
    command = [
        str(python_bin if python_bin.exists() else Path(sys.executable)),
        str(parser_script),
        str(rent_roll_path),
        *mapping_args,
        "--output-dir",
        str(output_dir),
        "--summary",
    ]
    parser_name = "standardize_rent_roll"
    if _looks_like_yardi_lease_charges(rent_roll_path):
        parser_script = repo.path / "etl" / "parse_yardi_lease_charges.py"
        command = [
            str(python_bin if python_bin.exists() else Path(sys.executable)),
            str(parser_script),
            str(rent_roll_path),
            "--auto-detect",
            "--output",
            str(output_dir),
            "--summary",
        ]
        parser_name = "parse_yardi_lease_charges"
    attempts.append((parser_name, parser_script, command))

    failures: list[dict[str, object]] = []
    standardized_path = output_dir / "rent_roll_standardized.csv"
    summary_path = output_dir / "floorplan_summary.csv"
    for attempt_name, _attempt_script, attempt_command in attempts:
        if output_dir.exists():
            shutil.rmtree(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        completed = subprocess.run(
            attempt_command,
            cwd=str(repo.path),
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0:
            failures.append({
                "parser": attempt_name,
                "status": "error",
                "stderr": (completed.stderr or "").strip(),
                "stdout": (completed.stdout or "").strip(),
            })
            continue
        standardized_rows = _read_csv_rows(standardized_path)
        summary_rows = _read_csv_rows(summary_path)
        if not standardized_rows and not summary_rows:
            failures.append({
                "parser": attempt_name,
                "status": "empty",
                "stdout": (completed.stdout or "").strip(),
            })
            continue
        return {
            "status": "ok",
            "parser": attempt_name,
            "standardized_path": str(standardized_path),
            "summary_path": str(summary_path),
            "standardized_rows": standardized_rows,
            "summary_rows": summary_rows,
            "attempts": failures,
        }

    if failures:
        last = dict(failures[-1])
        last["attempts"] = failures
        return last
    return None


def _build_house_box_score(canonical: dict) -> dict[str, object] | None:
    cohorts = canonical.get("unit_cohorts") or []
    if not cohorts:
        return None
    floorplans: list[dict[str, object]] = []
    total_units = 0
    rent_numerator = 0.0
    sqft_numerator = 0.0
    for row in cohorts:
        units = int(row.get("unit_count", 0) or 0)
        if units <= 0:
            continue
        code = str(row.get("unit_type") or row.get("cohort_id") or "").strip()
        avg_in_place_rent = row.get("initial_inplace_rent")
        avg_market_rent = row.get("target_monthly_rent")
        if avg_in_place_rent in (None, ""):
            avg_in_place_rent = avg_market_rent
        avg_in_place_value = float(avg_in_place_rent) if avg_in_place_rent not in (None, "") else None
        avg_market_value = float(avg_market_rent) if avg_market_rent not in (None, "") else avg_in_place_value
        avg_rent_value = avg_in_place_value if avg_in_place_value is not None else avg_market_value
        avg_sqft = row.get("sqft")
        avg_sqft_value = float(avg_sqft) if avg_sqft not in (None, "") else None
        beds = row.get("bedrooms")
        baths = row.get("bathrooms")
        name_parts = []
        if beds not in (None, "") and baths not in (None, ""):
            name_parts.append(f"{beds}x{baths}")
        if avg_sqft_value is not None:
            name_parts.append(f"{int(avg_sqft_value) if avg_sqft_value.is_integer() else round(avg_sqft_value, 1)} sqft")
        floorplans.append(
            {
                "code": code or None,
                "name": " ".join(name_parts) if name_parts else code or None,
                "avg_sqft": avg_sqft_value,
                "avg_rent": avg_rent_value,
                "avg_in_place_rent": avg_in_place_value,
                "avg_market_rent": avg_market_value,
                "units": units,
                "available_units": None,
                "model_units": None,
                "bedrooms": beds,
                "bathrooms": baths,
            }
        )
        total_units += units
        if avg_rent_value is not None:
            rent_numerator += avg_rent_value * units
        if avg_sqft_value is not None:
            sqft_numerator += avg_sqft_value * units
    if not floorplans or total_units <= 0:
        return None
    weighted_avg_rent = rent_numerator / total_units if rent_numerator else None
    weighted_avg_sqft = sqft_numerator / total_units if sqft_numerator else None
    return {
        "source_file": "derived_from_rent_roll",
        "report_title": "House Box Score",
        "summary_type": "derived",
        "derivation_basis": "canonical_unit_cohorts",
        "notes": "Derived from rent roll / canonical cohorts. Availability and model-unit counts require an explicit box score or unit-status source.",
        "floorplans": floorplans,
        "total_units": total_units,
        "available_units": None,
        "model_units": None,
        "weighted_avg_rent": weighted_avg_rent,
        "weighted_avg_sqft": weighted_avg_sqft,
    }


def _harvest_property_tax_evidence(
    canonical: dict,
    *,
    classified: dict[str, list[Path]],
    raw_inputs_dir: Path,
) -> dict:
    """Persist all unit-labelled tax candidates and inspected source paths."""
    if not isinstance(canonical, dict):
        return canonical
    metadata = canonical.setdefault("metadata", {})
    property_summary = metadata.setdefault("property_summary", {})
    existing_candidates = property_summary.get(
        "property_tax_evidence_candidates"
    ) or []
    candidates = [
        dict(candidate)
        for candidate in existing_candidates
        if isinstance(candidate, dict)
    ]
    evidence_locations = {
        item for item in (
            property_summary.get("property_tax_evidence_locations") or []
        )
        if isinstance(item, str) and item
    }
    inspected_paths: set[str] = set()
    seen_candidates = {
        (
            str(candidate.get("millage_rate_mills")),
            candidate.get("source"),
            candidate.get("source_locator"),
        )
        for candidate in candidates
    }
    source_by_kind = {
        "om": "offering_memorandum",
        "tax_doc": "county_tax_notice",
        "debt_guidance": "debt_guidance",
    }
    for kind in ("om", "tax_doc", "debt_guidance"):
        for path in sorted(
            classified.get(kind) or [],
            key=lambda item: item.relative_to(raw_inputs_dir).as_posix(),
        ):
            relative = f"raw_inputs/{path.relative_to(raw_inputs_dir).as_posix()}"
            inspected_paths.add(relative)
            text = _shared_load_pdf_text(path)
            parsed, locations = _extract_labelled_property_tax_evidence(
                text,
                source=source_by_kind[kind],
                source_locator=relative,
            )
            if kind == "om":
                snapshot = _shared_parse_broker_om_snapshot(path)
                tax_context = (
                    snapshot.get("property_tax_context")
                    if isinstance(snapshot, dict)
                    else None
                )
                if isinstance(tax_context, dict):
                    parsed.extend(
                        candidate
                        for candidate in (
                            tax_context.get(
                                "property_tax_evidence_candidates"
                            ) or []
                        )
                        if isinstance(candidate, dict)
                    )
            for candidate in parsed:
                key = (
                    str(candidate.get("millage_rate_mills")),
                    candidate.get("source"),
                    candidate.get("source_locator"),
                )
                if key in seen_candidates:
                    continue
                candidates.append(candidate)
                seen_candidates.add(key)
                locator = candidate.get("source_locator")
                if isinstance(locator, str) and locator:
                    evidence_locations.add(locator)
            evidence_locations.update(locations)
    property_summary["property_tax_evidence_candidates"] = candidates
    property_summary["property_tax_evidence_inspected_paths"] = sorted(
        inspected_paths
    )
    property_summary["property_tax_evidence_locations"] = sorted(
        evidence_locations
    )
    metadata["property_summary"] = property_summary
    canonical["metadata"] = metadata
    return canonical


def _normalize_floorplan_code(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def _merge_house_box_with_box_score_counts(
    house_box: dict[str, object],
    parsed_box: dict[str, object],
) -> dict[str, object]:
    floorplans = house_box.get("floorplans")
    parsed_floorplans = parsed_box.get("floorplans")
    if not isinstance(floorplans, list) or not isinstance(parsed_floorplans, list):
        return house_box

    parsed_by_code = {
        _normalize_floorplan_code(row.get("code") or row.get("name")): row
        for row in parsed_floorplans
        if isinstance(row, dict) and _normalize_floorplan_code(row.get("code") or row.get("name"))
    }
    if not parsed_by_code:
        return house_box

    merged_floorplans: list[dict[str, object]] = []
    matched_keys: set[str] = set()
    for row in floorplans:
        if not isinstance(row, dict):
            continue
        key = _normalize_floorplan_code(row.get("code") or row.get("name"))
        parsed_row = parsed_by_code.get(key)
        if parsed_row is None:
            merged_floorplans.append(row)
            continue
        matched_keys.add(key)
        merged = dict(row)
        for field in ("units", "available_units", "model_units"):
            if parsed_row.get(field) is not None:
                merged[field] = parsed_row.get(field)
        merged_floorplans.append(merged)

    for key, parsed_row in parsed_by_code.items():
        if key in matched_keys:
            continue
        merged_floorplans.append(dict(parsed_row))

    merged_box = dict(house_box)
    merged_box["floorplans"] = merged_floorplans
    merged_box["total_units"] = int(
        parsed_box.get("total_units")
        or sum(int(row.get("units") or 0) for row in merged_floorplans if isinstance(row, dict))
    )
    if parsed_box.get("available_units") is not None:
        merged_box["available_units"] = parsed_box.get("available_units")
    if parsed_box.get("model_units") is not None:
        merged_box["model_units"] = parsed_box.get("model_units")
    merged_box["box_score_count_source"] = parsed_box.get("source_file")
    merged_box["notes"] = (
        f"{merged_box.get('notes') or ''} Unit counts and availability reconciled to parsed broker box score."
    ).strip()
    return merged_box


def _infer_bed_bath_from_floorplan(floorplan: dict[str, object]) -> tuple[int | None, float | None]:
    text = " ".join(
        str(value or "")
        for value in (
            floorplan.get("code"),
            floorplan.get("name"),
        )
    ).lower()
    bedrooms: int | None = None
    bathrooms: float | None = None
    explicit_bedrooms = _to_int(floorplan.get("bedrooms"))
    explicit_bathrooms = _to_float(floorplan.get("bathrooms"))
    if explicit_bedrooms is not None:
        bedrooms = explicit_bedrooms
    if explicit_bathrooms is not None:
        bathrooms = explicit_bathrooms


    bed_match = re.search(r"(\d+)\s*br", text)
    if bedrooms is None and bed_match:
        bedrooms = int(bed_match.group(1))
    elif bedrooms is None and ("studio" in text or "eff" in text):
        bedrooms = 0
    elif bedrooms is None:
        code = str(floorplan.get("code") or "").lower()
        for token, beds in (("blm-1", 1), ("blm-2", 2), ("blm-3", 3)):
            if code.startswith(token):
                bedrooms = beds
                break

    bath_match = re.search(r"(\d+(?:\.\d+)?)\s*ba", text)
    if bathrooms is None and bath_match:
        bathrooms = float(bath_match.group(1))
    elif bathrooms is None and bedrooms is not None:
        bathrooms = 1.0 if bedrooms <= 1 else 2.0
    return bedrooms, bathrooms


def _remove_intake_flags(canonical: dict, flags_to_remove: set[str]) -> None:
    metadata = canonical.setdefault("metadata", {})
    flags = [flag for flag in metadata.get("intake_sanity_flags", []) or [] if flag not in flags_to_remove]
    metadata["intake_sanity_flags"] = flags


def _rebase_canonical_to_house_box_score(
    canonical: dict,
    house_box: dict[str, object],
    *,
    start_period: str,
    end_period: str,
) -> None:
    """Replace local-parser cohorts with the standardized house box score.

    The market-study standardized rent-roll sidecar is the preferred truth
    surface when the local engine parser drifts. Rebase here so every later
    lifecycle step, not just pricing backsolve, sees consistent unit counts.
    """
    floorplans = house_box.get("floorplans") or []
    if not isinstance(floorplans, list) or not floorplans:
        return

    existing_by_code = {
        re.sub(r"[^a-z0-9]+", "", str(row.get("unit_type") or row.get("cohort_id") or "").lower()): row
        for row in canonical.get("unit_cohorts") or []
    }
    rebuilt: list[dict[str, object]] = []
    for idx, floorplan in enumerate(floorplans):
        if not isinstance(floorplan, dict):
            continue
        units = int(floorplan.get("units") or 0)
        if units <= 0:
            continue
        code = str(floorplan.get("code") or floorplan.get("name") or f"house_box_{idx + 1}").strip()
        key = re.sub(r"[^a-z0-9]+", "", code.lower())
        existing = existing_by_code.get(key, {})
        avg_in_place = floorplan.get("avg_in_place_rent")
        avg_market = floorplan.get("avg_market_rent")
        avg_rent = floorplan.get("avg_rent")
        in_place = _to_float(avg_in_place) or _to_float(avg_rent) or _to_float(avg_market) or 0.0
        target = _to_float(avg_market) or _to_float(avg_rent) or in_place
        bedrooms, bathrooms = _infer_bed_bath_from_floorplan(floorplan)
        cohort_id = str(existing.get("cohort_id") or key or f"house_box_{idx + 1}")
        row: dict[str, object] = {
            **existing,
            "cohort_id": cohort_id,
            "unit_type": code,
            "unit_count": units,
            "initial_inplace_rent": float(in_place),
            "target_monthly_rent": float(target),
            "target_monthly_rent_source": "rent_roll_avg",
        }
        sqft = _to_float(floorplan.get("avg_sqft"))
        if sqft is not None:
            row["sqft"] = float(sqft)
        if bedrooms is not None:
            row["bedrooms"] = int(bedrooms)
        if bathrooms is not None:
            row["bathrooms"] = float(bathrooms)
        rebuilt.append(row)

    if not rebuilt:
        return

    canonical["unit_cohorts"] = rebuilt
    canonical["market_rent_curve"] = [
        {
            "cohort_id": row["cohort_id"],
            "start_period": start_period,
            "end_period": end_period,
            "market_rent": float(row.get("target_monthly_rent") or row.get("initial_inplace_rent") or 0.0),
        }
        for row in rebuilt
    ]
    canonical["loss_to_lease"] = [
        {
            "cohort_id": row["cohort_id"],
            "start_period": start_period,
            "end_period": end_period,
            "ltl_percent": 0.0,
        }
        for row in rebuilt
    ]
    status_counts = house_box.get("status_counts") if isinstance(house_box.get("status_counts"), dict) else {}
    total_units = int(house_box.get("total_units") or sum(int(row.get("unit_count") or 0) for row in rebuilt))
    unavailable = sum(
        int(value or 0)
        for key, value in status_counts.items()
        if str(key).strip().lower() in {"vacant", "non-revenue", "non revenue"}
    )
    vacancy_rate = float(unavailable / total_units) if total_units else 0.0
    canonical["physical_vacancy_curve"] = [
        {
            "cohort_id": row["cohort_id"],
            "start_period": start_period,
            "end_period": end_period,
            "vacancy_rate": vacancy_rate,
        }
        for row in rebuilt
    ]
    _remove_intake_flags(
        canonical,
        {
            "target_monthly_rent_missing_for_cohort_0br1ba",
        },
    )
    _append_intake_flag(canonical, "unit_cohorts_rebased_to_standardized_rent_roll")


def _enrich_canonical_supporting_docs(
    canonical: dict,
    *,
    classified_single: dict[str, Path],
    start_period: str,
    end_period: str,
    intake_dir: Path | None = None,
) -> dict:
    if not isinstance(canonical, dict) or not canonical:
        return canonical
    metadata = canonical.setdefault("metadata", {})
    property_summary = dict(metadata.get("property_summary") or {})
    house_box = None
    standardization_meta: dict[str, object] | None = None
    rent_roll = classified_single.get("rent_roll")
    if intake_dir is not None and rent_roll is not None:
        standardization = _normalize_rent_roll_with_market_study(
            rent_roll,
            output_dir=intake_dir / "rent_roll_standardized",
        )
        if standardization:
            standardization_meta = {
                k: v
                for k, v in standardization.items()
                if k not in {"standardized_rows", "summary_rows"}
            }
            if standardization.get("status") == "ok":
                house_box = _build_house_box_score_from_standardized(
                    standardization.get("summary_rows", []),
                    standardization.get("standardized_rows", []),
                )
                if house_box:
                    cohort_units = sum(int(row.get("unit_count", 0) or 0) for row in canonical.get("unit_cohorts") or [])
                    derived_units = house_box.get("total_units")
                    if isinstance(derived_units, int) and cohort_units and derived_units != cohort_units:
                        _append_intake_flag(canonical, "standardized_rent_roll_unit_count_mismatch")
                    _rebase_canonical_to_house_box_score(
                        canonical,
                        house_box,
                        start_period=start_period,
                        end_period=end_period,
                    )
            elif standardization.get("status") in {"empty", "error"}:
                _append_intake_flag(
                    canonical,
                    f"rent_roll_standardization_{standardization.get('status')}",
                )
    box_score = classified_single.get("box_score")
    parsed_box = None
    if box_score is not None:
        parsed_box = _parse_box_score(box_score)
        if parsed_box:
            property_summary["box_score"] = parsed_box
            if house_box:
                house_box = _merge_house_box_with_box_score_counts(house_box, parsed_box)
                _rebase_canonical_to_house_box_score(
                    canonical,
                    house_box,
                    start_period=start_period,
                    end_period=end_period,
                )

    if house_box is None:
        house_box = _build_house_box_score(canonical)
    if house_box:
        property_summary["house_box_score"] = house_box
    if standardization_meta:
        property_summary["rent_roll_standardization"] = standardization_meta

    if parsed_box:
        cohort_units = sum(int(row.get("unit_count", 0) or 0) for row in canonical.get("unit_cohorts") or [])
        box_units = parsed_box.get("total_units")
        if isinstance(box_units, int) and cohort_units and box_units != cohort_units:
            _append_intake_flag(canonical, "box_score_unit_count_mismatch")
    elif house_box:
        property_summary["box_score"] = house_box

    debt_guidance = classified_single.get("debt_guidance")
    if debt_guidance is not None:
        parsed_debt = _parse_debt_guidance(
            debt_guidance,
            start_period=start_period,
            end_period=end_period,
        )
        if parsed_debt:
            property_summary["debt_guidance"] = parsed_debt

    om_doc = classified_single.get("om")
    if om_doc is not None:
        broker_snapshot = _parse_broker_om_snapshot(om_doc)
        if broker_snapshot:
            property_summary["broker_underwriting_snapshot"] = broker_snapshot
            if metadata.get("year_built") in (None, "") and broker_snapshot.get("year_built") is not None:
                metadata["year_built"] = int(broker_snapshot["year_built"])
            tax_context = broker_snapshot.get("property_tax_context")
            if isinstance(tax_context, dict):
                property_summary["property_tax_context"] = tax_context

    if property_summary:
        metadata["property_summary"] = property_summary
    canonical["metadata"] = metadata
    return canonical


def _preserve_subject_metadata(
    canonical: dict,
    *,
    prior_canonical: dict | None,
) -> dict:
    metadata = canonical.setdefault("metadata", {})
    prior_metadata = (prior_canonical or {}).get("metadata") or {}
    property_summary = metadata.get("property_summary") or {}
    broker_snapshot = property_summary.get("broker_underwriting_snapshot") or {}

    if not metadata.get("address"):
        for candidate in (
            broker_snapshot.get("address"),
            prior_metadata.get("address"),
        ):
            if candidate:
                metadata["address"] = candidate
                break

    if not metadata.get("market"):
        for candidate in (
            prior_metadata.get("market"),
        ):
            if candidate:
                metadata["market"] = candidate
                break

    canonical["metadata"] = metadata
    return canonical


def _add_months(year: int, month: int, delta_months: int) -> tuple[int, int]:
    total = year * 12 + (month - 1) + delta_months
    return total // 12, total % 12 + 1


def _default_analysis_window(today: date | None = None) -> tuple[str, str]:
    today = today or date.today()
    start_year, start_month = _add_months(today.year, today.month, 1)
    end_year, end_month = _add_months(start_year, start_month, 59)
    return f"{start_year:04d}-{start_month:02d}", f"{end_year:04d}-{end_month:02d}"


def _derive_property_id(request_payload: DealIntakeRequest, request: BridgeRequestV1, classified: dict[str, list[Path]]) -> tuple[str, bool]:
    if request_payload.property_id_hint:
        return request_payload.property_id_hint, False
    om_files = classified.get("om") or []
    if om_files:
        stem = om_files[0].stem
        stem = re.sub(r"\b(om|offering|memorandum|final)\b", "", stem, flags=re.IGNORECASE)
        stem = re.sub(r"[_-]+", " ", stem).strip()
        if stem:
            return stem, True
    return request.deal_slug.replace("_", " ").title(), True


def _build_manifest_markdown(
    *,
    deal_slug: str,
    classified_single: dict[str, Path],
    unknown_files: list[Path],
    start_period: str,
    end_period: str,
) -> str:
    lines = [
        f"# Deal Manifest: {deal_slug.replace('_', ' ').title()}",
        "",
        "## Intake Summary",
        f"- Analysis period: `{start_period}` to `{end_period}`",
        "",
        "## Raw Input Checklist",
        "| Type | File | Status |",
        "|------|------|--------|",
    ]
    for doc_type in (
        "om",
        "rent_roll",
        "t12",
        "box_score",
        "debt_guidance",
        "capex_quote",
        "tax_doc",
        "occupancy_support",
        "marketing_flyer",
    ):
        path = classified_single.get(doc_type)
        status = "Loaded" if path else "Missing"
        lines.append(f"| {doc_type} | `{path.name}` | {status} |" if path else f"| {doc_type} | — | {status} |")
    if unknown_files:
        lines.extend([
            "",
            "## Unknown Documents",
        ])
        lines.extend(f"- `{path.name}`" for path in unknown_files)
    return "\n".join(lines) + "\n"


def _has_deterministic_broker_snapshot(canonical: dict) -> bool:
    metadata = canonical.get("metadata", {}) if isinstance(canonical, dict) else {}
    property_summary = metadata.get("property_summary", {}) or {}
    snapshot = property_summary.get("broker_underwriting_snapshot") or {}
    if not isinstance(snapshot, dict):
        return False
    return any(
        snapshot.get(key) not in (None, "", {}, [])
        for key in ("year_built", "unit_count", "trailing_noi", "noi", "expense_assumptions", "revenue_assumptions")
    )


def _has_unit_count_consensus(canonical: dict) -> bool:
    metadata = canonical.get("metadata", {}) if isinstance(canonical, dict) else {}
    property_summary = metadata.get("property_summary", {}) or {}
    counts: list[int] = []
    for candidate in (
        (property_summary.get("house_box_score") or {}).get("total_units"),
        (property_summary.get("box_score") or {}).get("total_units"),
        (property_summary.get("broker_underwriting_snapshot") or {}).get("unit_count"),
    ):
        try:
            value = int(candidate)
        except (TypeError, ValueError):
            continue
        if value > 0:
            counts.append(value)
    if len(counts) < 2:
        return False
    return any(counts.count(value) >= 2 for value in set(counts))


def _build_blockers_and_punchlist(
    *,
    classified_single: dict[str, Path],
    unknown_files: list[Path],
    canonical: dict,
    used_period_default: bool,
    property_id_was_derived: bool,
    start_period: str,
    end_period: str,
) -> tuple[list[dict[str, str]], str]:
    blockers: list[dict[str, str]] = []
    notes: list[str] = []

    if "rent_roll" not in classified_single:
        blockers.append({"id": "missing_rent_roll", "description": "Rent roll not detected in raw_inputs/."})
    if "t12" not in classified_single:
        blockers.append({"id": "missing_t12", "description": "T12 not detected in raw_inputs/."})
    if used_period_default:
        notes.append(
            f"`confirm_analysis_period` resolved by house default: using `{start_period}` → `{end_period}` unless analyst overrides."
        )
    if property_id_was_derived:
        notes.append(
            "`confirm_property_id` downgraded: property ID was derived heuristically, but this is a naming note rather than a valuation blocker."
        )
    for path in unknown_files:
        blockers.append({
            "id": f"unknown_document_{re.sub(r'[^a-z0-9]+', '_', path.stem.lower()).strip('_') or 'file'}",
            "description": f"Document `{path.name}` could not be classified automatically.",
        })

    metadata = canonical.get("metadata", {}) if isinstance(canonical, dict) else {}
    property_summary = metadata.get("property_summary", {}) or {}
    deterministic_om = _has_deterministic_broker_snapshot(canonical)
    unit_count_consensus = _has_unit_count_consensus(canonical)
    for flag in metadata.get("intake_sanity_flags", []) or []:
        if flag == "unit_cohorts_rebased_to_standardized_rent_roll":
            notes.append(
                "`unit_cohorts_rebased_to_standardized_rent_roll` resolved: canonical cohorts were rebuilt from the shared standardized rent-roll surface."
            )
            continue
        if flag == "om_extraction_unavailable" and deterministic_om:
            notes.append(
                "`om_extraction_unavailable` downgraded: deterministic OM harvesting captured core broker underwriting facts."
            )
            continue
        if flag == "unit_type_codes_unrecognized":
            house_box = property_summary.get("house_box_score") or {}
            floorplans = house_box.get("floorplans") or []
            canonical_units = sum(int(row.get("unit_count", 0) or 0) for row in canonical.get("unit_cohorts") or [])
            standardized_units = int(house_box.get("total_units") or 0)
            if (
                house_box.get("derivation_basis") == "market-study-agent standardized rent roll"
                and standardized_units > 0
                and standardized_units == canonical_units
                and all((row.get("name") or "").strip() for row in floorplans)
            ):
                notes.append(
                    "`unit_type_codes_unrecognized` resolved by shared standardized rent roll: floorplan summary recovered bedroom labels and unit counts."
                )
                continue
        if flag in {"standardized_rent_roll_unit_count_mismatch", "box_score_unit_count_mismatch"} and unit_count_consensus:
            notes.append(
                f"`{flag}` resolved by consensus: shared standardized rent roll, broker box score, and broker OM agree on unit count."
            )
            continue
        blockers.append({
            "id": str(flag),
            "description": f"ingest_deal flagged `{flag}`; analyst review required.",
        })
    if metadata.get("analyst_review_required"):
        blockers.append({
            "id": "analyst_review_required",
            "description": "Canonical metadata marked analyst_review_required=true; confirm OM-extracted assumptions.",
        })

    lines = ["# Intake Punchlist", ""]
    if blockers:
        lines.append("## Analyst-Required Follow-Up")
        lines.extend(f"- `{item['id']}`: {item['description']}" for item in blockers)
    else:
        lines.append("- No blocking intake issues detected.")
    if notes:
        lines.extend(["", "## Resolved Or Downgraded Notes"])
        lines.extend(f"- {note}" for note in notes)
    return blockers, "\n".join(lines) + "\n"


def _direct_ingest_response(
    repo: SiblingRepo,
    request: BridgeRequestV1,
    *,
    payload_model=None,
) -> BridgeResponseV1:
    request_payload = DealIntakeRequest.model_validate(request.payload)
    deal_root = Path(request.deal_root)
    run_dir = deal_root / "outputs" / request.run_id
    intake_dir = run_dir / "intake"
    intake_dir.mkdir(parents=True, exist_ok=True)

    raw_inputs_dir = deal_root / request_payload.raw_inputs_dir_relative
    raw_files = sorted((p for p in raw_inputs_dir.rglob("*") if p.is_file()), key=lambda p: p.relative_to(raw_inputs_dir).as_posix()) if raw_inputs_dir.is_dir() else []

    classified: dict[str, list[Path]] = {}
    for path in raw_files:
        classified.setdefault(_classify_document_type(path), []).append(path)
    classified_single = _select_document_by_type(classified)
    unknown_files = classified.get("unknown", [])

    if request_payload.period_start and request_payload.period_end:
        start_period = request_payload.period_start
        end_period = request_payload.period_end
        used_period_default = False
    else:
        start_period, end_period = _default_analysis_window()
        used_period_default = True

    property_id, property_id_was_derived = _derive_property_id(request_payload, request, classified)

    canonical_path = intake_dir / "canonical_deal.json"
    manifest_path = intake_dir / "manifest.md"
    punchlist_path = intake_dir / "intake_punchlist.md"
    deal_manifest_path = deal_root / "deal_manifest.md"
    deal_punchlist_path = deal_root / "intake_punchlist.md"
    prior_canonical: dict | None = None
    if canonical_path.exists():
        try:
            prior_canonical = json.loads(canonical_path.read_text())
        except Exception:
            prior_canonical = None

    canonical: dict = {}
    stderr_text = ""
    if "rent_roll" in classified_single and "t12" in classified_single:
        ingest_rent_roll = _parser_ready_rent_roll_path(
            deal_root,
            classified_single["rent_roll"],
        )
        python_bin = repo.path / ".venv" / "bin" / "python"
        command = [
            str(python_bin if python_bin.exists() else Path(sys.executable)),
            str(repo.path / "runs" / "ingest_deal.py"),
            "--property-id",
            property_id,
            "--rent-roll",
            str(ingest_rent_roll),
            "--t12",
            str(classified_single["t12"]),
            "--start",
            start_period,
            "--end",
            end_period,
            "--output",
            str(canonical_path),
            "--analyst",
            "plat-agent",
        ]
        if "om" in classified_single:
            command.extend(["--om", str(classified_single["om"])])
        completed = subprocess.run(
            command,
            cwd=str(repo.path),
            capture_output=True,
            text=True,
        )
        stderr_text = completed.stderr or ""
        if completed.returncode != 0:
            manifest_md = _build_manifest_markdown(
                deal_slug=request.deal_slug,
                classified_single=classified_single,
                unknown_files=unknown_files,
                start_period=start_period,
                end_period=end_period,
            )
            atomic_write_text(manifest_path, manifest_md)
            atomic_write_text(deal_manifest_path, manifest_md)
            atomic_write_text(
                punchlist_path,
                "# Intake Punchlist\n\n- `ingest_deal_failed`: ingest_deal.py failed. Inspect stderr before retrying.\n",
            )
            atomic_write_text(deal_punchlist_path, punchlist_path.read_text())
            return BridgeResponseV1(
                status="error",
                deal_slug=request.deal_slug,
                run_id=request.run_id,
                agent_name=request.agent_name,
                payload={},
                error=BridgeError(
                    code="ingest_deal_failed",
                    message=(stderr_text.strip() or completed.stdout.strip() or "ingest_deal.py failed").strip(),
                    recoverable=False,
                ),
            )
        canonical = json.loads(canonical_path.read_text())
        classified_for_ingest = dict(classified_single)
        classified_for_ingest["rent_roll"] = ingest_rent_roll
        canonical = _enrich_canonical_supporting_docs(
            canonical,
            classified_single=classified_for_ingest,
            start_period=start_period,
            end_period=end_period,
            intake_dir=intake_dir,
        )
        canonical = _harvest_property_tax_evidence(
            canonical,
            classified=classified,
            raw_inputs_dir=raw_inputs_dir,
        )
        canonical = _preserve_subject_metadata(
            canonical,
            prior_canonical=prior_canonical,
        )
        atomic_write_json(canonical_path, canonical)
    else:
        atomic_write_text(canonical_path, "{}\n")

    manifest_md = _build_manifest_markdown(
        deal_slug=request.deal_slug,
        classified_single=classified_single,
        unknown_files=unknown_files,
        start_period=start_period,
        end_period=end_period,
    )
    atomic_write_text(manifest_path, manifest_md)
    atomic_write_text(deal_manifest_path, manifest_md)

    blockers, punchlist_md = _build_blockers_and_punchlist(
        classified_single=classified_single,
        unknown_files=unknown_files,
        canonical=canonical,
        used_period_default=used_period_default,
        property_id_was_derived=property_id_was_derived,
        start_period=start_period,
        end_period=end_period,
    )
    atomic_write_text(punchlist_path, punchlist_md)
    atomic_write_text(deal_punchlist_path, punchlist_md)

    payload_dict = {
        "canonical_deal_json_relative": f"outputs/{request.run_id}/intake/canonical_deal.json",
        "manifest_relative": f"outputs/{request.run_id}/intake/manifest.md",
        "punchlist_relative": f"outputs/{request.run_id}/intake/intake_punchlist.md",
        "cohorts_identified": len((canonical.get("unit_cohorts") or []) if isinstance(canonical, dict) else []),
        "documents_classified": {
            path.name: doc_type
            for doc_type, paths in classified.items()
            for path in paths
        },
    }
    if payload_model is not None:
        payload_dict = payload_model.model_validate(payload_dict).model_dump(mode="json")

    artifacts = [
        ArtifactRef(
            relative_path=f"outputs/{request.run_id}/intake/canonical_deal.json",
            kind="json",
            description="Canonical deal JSON produced by ingest_deal.py.",
        ),
        ArtifactRef(
            relative_path=f"outputs/{request.run_id}/intake/manifest.md",
            kind="md",
            description="Run-scoped intake manifest.",
        ),
    ]

    if blockers:
        return BridgeResponseV1(
            status="needs_analyst_input",
            deal_slug=request.deal_slug,
            run_id=request.run_id,
            agent_name=request.agent_name,
            payload=payload_dict,
            artifacts=artifacts,
            error=BridgeError(
                code="needs_analyst_input",
                message="Intake completed with analyst follow-up items.",
                recoverable=True,
                details={"blockers": blockers},
            ),
        )

    return BridgeResponseV1(
        status="ok",
        deal_slug=request.deal_slug,
        run_id=request.run_id,
        agent_name=request.agent_name,
        payload=payload_dict,
        artifacts=artifacts,
    )


def dispatch_sibling_agent(repo, request, **kwargs):
    if request.agent_name == "deal-intake":
        return _direct_ingest_response(repo, request, payload_model=kwargs.get("payload_model"))
    return _dispatch_sibling_agent(repo, request, **kwargs)


@dataclass
class IntakeStep:
    """Lifecycle adapter for the deal-intake sibling agent."""

    name: ClassVar[str] = "intake"

    def _build_request(
        self,
        state: LifecycleState,
        *,
        run_dir: Path,
        deal_root: Path,
    ) -> BridgeRequestV1:
        """Build the BridgeRequestV1 envelope wrapping a DealIntakeRequest.

        The agent reads from outputs/<run_id>/raw_inputs/ (run-scoped per spec
        §3 pre-step) — slug-level raw_inputs/ is staging-only.
        """
        # raw_inputs_dir_relative is relative to deal_root, so we render it as
        # "outputs/<run_id>/raw_inputs" rather than the absolute run_dir/raw_inputs.
        raw_inputs_relative = f"outputs/{state.run_id}/raw_inputs"
        payload = DealIntakeRequest(
            raw_inputs_dir_relative=raw_inputs_relative,
        )
        return BridgeRequestV1(
            deal_slug=state.deal_slug,
            run_id=state.run_id,
            deal_root=str(deal_root),
            agent_name="deal-intake",
            payload=payload.model_dump(mode="json"),
        )

    def _resolve_sibling_repo(self) -> SiblingRepo:
        """Resolve the multifamily-underwriting sibling repo location.

        Honors PLAT_MULTIFAMILY_UNDERWRITING_PATH per the SiblingRepo
        env-var convention (see dispatch/sibling.py docstring).
        """
        return SiblingRepo.from_env_or_default(
            MFU_SIBLING_NAME,
            default_relative=MFU_SIBLING_NAME,
        )

    def _derive_deal_root(self, state: LifecycleState, run_dir: Path) -> Path:
        """Deal root is the parent of outputs/<run_id>/.

        Per spec: run_dir = <deal_root>/outputs/<run_id>/. Walking up two
        parents gives <deal_root>.
        """
        return run_dir.parent.parent

    def _commit_step_artifacts(
        self,
        run_dir: Path,
        *,
        input_hash: str,
        status: str,
    ) -> None:
        """Write _provenance.json + _complete as the last action.

        The _complete marker MUST be the very last write so a mid-step crash
        leaves no marker → is_satisfied returns False → step reruns
        (spec §4.4.1).
        """
        intake_dir = run_dir / "intake"
        # Provenance carries cache-validity inputs (input_hash, contract_version)
        write_provenance(
            intake_dir,
            input_hash=input_hash,
            contract_version=CONTRACT_VERSION,
            status=status,
        )
        # The file_manifest is the set of files is_satisfied() will require
        # to be present. Derive from what's actually on disk in the intake/
        # directory so we don't lie about absent optional files.
        manifest = sorted(
            p.name for p in intake_dir.iterdir() if p.is_file() and p.name != "_complete"
        )
        write_complete_marker(
            intake_dir,
            step="intake",
            file_manifest=manifest,
        )

    def _harvest_run_property_tax_evidence(self, run_dir: Path) -> None:
        canonical_path = run_dir / "intake" / "canonical_deal.json"
        if not canonical_path.exists():
            return
        canonical = json.loads(canonical_path.read_text())
        original = json.loads(json.dumps(canonical))
        classified: dict[str, list[Path]] = {}
        for path in self._list_raw_inputs(run_dir):
            classified.setdefault(_classify_document_type(path), []).append(
                path
            )
        enriched = _harvest_property_tax_evidence(
            canonical,
            classified=classified,
            raw_inputs_dir=run_dir / "raw_inputs",
        )
        if enriched != original:
            atomic_write_json(canonical_path, enriched)


    def run(self, state: LifecycleState, run_dir: Path) -> StepResult:
        deal_root = self._derive_deal_root(state, run_dir)
        request = self._build_request(state, run_dir=run_dir, deal_root=deal_root)
        repo = self._resolve_sibling_repo()
        response = dispatch_sibling_agent(
            repo,
            request,
            payload_model=DealIntakeResponse,
        )
        result = self._handle_response(state, run_dir, response, deal_root)
        if result.status in ("ok", "blocked"):
            self._harvest_run_property_tax_evidence(run_dir)

        # Write _provenance.json + _complete only for ok / blocked outcomes.
        # An error means the step did not produce a usable output set; leaving
        # no marker forces --resume to rerun from scratch.
        if result.status in ("ok", "blocked"):
            input_hash = self._compute_input_hash(run_dir)
            self._commit_step_artifacts(
                run_dir,
                input_hash=input_hash,
                status=result.status,
            )

        return result

    def _extract_blockers_from_error(
        self, response: BridgeResponseV1
    ) -> list[BlockerItem]:
        """Pull blocker items out of BridgeError.details["blockers"] when present.

        The deal-intake agent emits structured blocker dicts under
        error.details["blockers"]; fall back to a single synthesized blocker
        when only a top-level message is provided.
        """
        if response.error is None:
            return []
        details = response.error.details or {}
        raw_blockers = details.get("blockers")
        if isinstance(raw_blockers, list) and raw_blockers:
            out: list[BlockerItem] = []
            for entry in raw_blockers:
                if not isinstance(entry, dict):
                    continue
                blocker_id = entry.get("id") or entry.get("code") or "unknown_intake_blocker"
                description = entry.get("description") or entry.get("message") or ""
                out.append(
                    BlockerItem(
                        step="intake",
                        id=str(blocker_id),
                        description=str(description),
                    )
                )
            if out:
                return out
        # Fallback: synthesize one blocker from the top-level error fields.
        return [
            BlockerItem(
                step="intake",
                id=response.error.code or "intake_blocker",
                description=response.error.message,
            )
        ]

    def _merge_blockers_into_punchlist(
        self,
        run_dir: Path,
        deal_slug: str,
        run_id: str,
        new_intake_blockers: list[BlockerItem],
    ) -> None:
        """Replace this step's blockers in punchlist.json; preserve others.

        Re-running intake with a fixed input set clears stale intake blockers
        from a prior run, while leaving blockers contributed by other steps
        (comps, judgment, etc.) untouched.
        """
        existing = read_punchlist_json(run_dir)
        # Keep everything that's NOT under "intake"
        preserved = [b for b in existing if b.step != "intake"]
        merged = preserved + new_intake_blockers
        write_punchlist_json(run_dir, merged)
        write_punchlist_markdown(run_dir, deal_slug, run_id, merged)

    def _list_raw_inputs(self, run_dir: Path) -> list[Path]:
        """Sorted recursive list of files under outputs/<run_id>/raw_inputs/.

        Sorting ensures the same set of files always produces the same hash
        regardless of OS-level directory ordering.
        """
        raw_dir = run_dir / "raw_inputs"
        if not raw_dir.is_dir():
            return []
        # Use as_posix() for portable sort keys (Windows backslash vs POSIX).
        return sorted(
            (p for p in raw_dir.rglob("*") if p.is_file()),
            key=lambda p: p.relative_to(raw_dir).as_posix(),
        )

    def _compute_input_hash(self, run_dir: Path) -> str:
        """sha256 over raw_inputs/ contents. Plan-01 helper does the hashing."""
        return compute_input_hash(self._list_raw_inputs(run_dir))

    def _parse_payload(
        self, response: BridgeResponseV1
    ) -> DealIntakeResponse | None:
        """Parse response.payload as DealIntakeResponse; return None on failure.

        The dispatcher already validates against payload_model when it can,
        but we re-parse defensively because some responses (errors) carry
        empty payloads.
        """
        if not response.payload:
            return None
        try:
            return DealIntakeResponse.model_validate(response.payload)
        except ValidationError:
            return None

    def _verify_contract_artifacts(
        self,
        deal_root: Path,
        run_dir: Path,
        payload: DealIntakeResponse | None,
    ) -> str | None:
        """Verify the artifacts the sibling claims to have written exist on disk.

        Per the deal-intake.md sibling contract the response payload carries
        the actual artifact relative paths under `deal_root`:
          - canonical_deal_json_relative (often `runs/deals/<slug>/standardized/canonical_deal.json`)
          - manifest_relative (typically deal-root `deal_manifest.md`)
          - punchlist_relative (typically deal-root `intake_punchlist.md`)

        We accept legacy responses that omit the payload by falling back to a
        run-scoped check at `outputs/<run_id>/intake/{canonical_deal.json,
        manifest.md}` (the layout used by older fixtures and tests).
        """
        if payload is not None:
            missing: list[str] = []
            for label, rel in (
                ("canonical_deal_json_relative", payload.canonical_deal_json_relative),
                ("manifest_relative", payload.manifest_relative),
                ("punchlist_relative", payload.punchlist_relative),
            ):
                if not (deal_root / rel).exists():
                    missing.append(f"{label}={rel}")
            if missing:
                return (
                    "intake step claimed status=ok but required artifacts "
                    f"missing under {deal_root}: {', '.join(missing)}"
                )
            return None

        # Legacy fallback: payload absent or unparseable. Look in the
        # lifecycle-owned `outputs/<run_id>/intake/` location.
        intake_dir = run_dir / "intake"
        missing_legacy: list[str] = []
        for fname in LIFECYCLE_INTAKE_ARTIFACTS:
            if not (intake_dir / fname).exists():
                missing_legacy.append(fname)
        if missing_legacy:
            return (
                f"intake step claimed status=ok but required artifacts missing "
                f"under {intake_dir}: {', '.join(missing_legacy)}"
            )
        return None

    def _mirror_artifacts_into_run_dir(
        self,
        deal_root: Path,
        run_dir: Path,
        payload: DealIntakeResponse | None,
    ) -> None:
        """Copy the sibling-emitted canonical + manifest into outputs/<run>/intake/.

        Downstream lifecycle code (judgment.py, memo.py, comps step) reads
        `run_dir/intake/canonical_deal.json` and `run_dir/intake/manifest.md`
        as fixed paths. The deal-intake.md contract puts those at variable
        deal-root-relative paths. We mirror them once on a successful intake
        so downstream consumers get a stable run-scoped layout.

        No-op when payload is absent (legacy path: the agent already wrote
        directly to `run_dir/intake/`).
        """
        if payload is None:
            return
        intake_dir = run_dir / "intake"
        intake_dir.mkdir(parents=True, exist_ok=True)

        for src_rel, dst_name in (
            (payload.canonical_deal_json_relative, "canonical_deal.json"),
            (payload.manifest_relative, "manifest.md"),
        ):
            src = deal_root / src_rel
            dst = intake_dir / dst_name
            if src.resolve() == dst.resolve():
                # Sibling wrote directly into the lifecycle-owned location.
                continue
            if src.exists():
                shutil.copy2(src, dst)

    def _handle_none_payload(
        self,
        state: LifecycleState,
        run_dir: Path,
        response: BridgeResponseV1,
    ) -> StepResult:
        """V1.4 graceful degradation — sibling returned payload=None.

        Willow Court regression: deal-intake legitimately can return
        ``status='ok'`` (or ``'needs_analyst_input'``) with ``payload=None``
        when the agent prompt hits an unrecoverable extraction failure but
        still wrote a punchlist directly to the deal_root. Pre-V1.4 strict
        pydantic validation crashed the step before V1.1 graceful-
        degradation could fire. Now: emit a single ``empty_payload_response``
        blocker, merge into punchlist, and return ``blocked`` so the runner
        keeps going (analyst can rerun intake in isolation).
        """
        intake_dir = run_dir / "intake"
        intake_dir.mkdir(parents=True, exist_ok=True)
        # Best-effort placeholder so downstream steps that need to call
        # `intake_dir / canonical_deal.json` get an empty-but-valid file
        # instead of a missing-file FileNotFoundError. Only write when the
        # agent didn't already drop one in place.
        canonical_local = intake_dir / "canonical_deal.json"
        if not canonical_local.exists():
            canonical_local.write_text("{}")

        agent_msg = (
            response.error.message
            if response.error is not None
            else "deal-intake agent returned no payload"
        )
        blocker = BlockerItem(
            step="intake",
            id="empty_payload_response",
            description=(
                "deal-intake agent returned status="
                f"{response.status} but with no payload — "
                f"{agent_msg}. The agent prompt likely hit an unrecoverable "
                "extraction failure (corrupt OM, missing rent roll, model "
                "rejection)."
            ),
            resolution_hint=(
                "Inspect outputs/<run_id>/raw_inputs/, fix any unparseable "
                "documents, then rerun the intake step in isolation: "
                "`plat lifecycle --rerun-from intake`."
            ),
        )
        self._merge_blockers_into_punchlist(
            run_dir, state.deal_slug, state.run_id, [blocker]
        )
        return StepResult(status="blocked", blockers=[blocker])

    def _handle_response(
        self,
        state: LifecycleState,
        run_dir: Path,
        response: BridgeResponseV1,
        deal_root: Path,
    ) -> StepResult:
        # V1.4 — short-circuit on None payload BEFORE any payload-shape work.
        # `_parse_payload` handles "missing/empty" but bridge-level payload=None
        # carries different semantics ("agent ran but produced nothing")
        # than an empty dict ({}, "agent returned shape, just no fields").
        if response.payload is None and response.status != "error":
            return self._handle_none_payload(state, run_dir, response)

        payload = self._parse_payload(response)

        if response.status == "ok":
            err = self._verify_contract_artifacts(deal_root, run_dir, payload)
            if err is not None:
                return StepResult(status="error", error_message=err)
            # Mirror sibling-emitted artifacts into the lifecycle-owned
            # `outputs/<run_id>/intake/` location so downstream steps can
            # read from a stable path.
            self._mirror_artifacts_into_run_dir(deal_root, run_dir, payload)
            self._merge_blockers_into_punchlist(
                run_dir, state.deal_slug, state.run_id, []
            )
            return StepResult(status="ok")
        if response.status == "needs_analyst_input":
            # Partial output is allowed under blocked status; don't fail-fast.
            # Best-effort mirror so downstream steps that DO want partial
            # canonical can still find it.
            self._mirror_artifacts_into_run_dir(deal_root, run_dir, payload)
            blockers = self._extract_blockers_from_error(response)
            self._merge_blockers_into_punchlist(
                run_dir, state.deal_slug, state.run_id, blockers
            )
            return StepResult(status="blocked", blockers=blockers)
        msg = response.error.message if response.error else "unknown intake error"
        return StepResult(status="error", error_message=msg)

    def is_satisfied(self, state: LifecycleState, run_dir: Path) -> bool:
        """Cache check per spec §4.4.

        Delegates to lifecycle.cache.is_satisfied with a freshly-computed
        input_hash from raw_inputs/. The cache helper handles steps_completed,
        _complete marker + file_manifest, punchlist clear, contract_version,
        and TTL (intake has no TTL).
        """
        current_hash = self._compute_input_hash(run_dir)
        return _cache_is_satisfied(
            state,
            run_dir,
            step="intake",
            current_input_hash=current_hash,
        )
