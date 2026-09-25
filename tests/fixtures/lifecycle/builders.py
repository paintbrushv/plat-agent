"""Deterministic fixture builders for lifecycle integration tests.

Every function here MUST be pure: same inputs -> byte-identical outputs.
No timestamps, no randomness, no network, no environment dependence.
Used by tests AND by the `regenerate-fixtures` CLI helper.
"""

from __future__ import annotations

import csv
import io
from pathlib import Path


CLEAN_DEAL_ADDRESS = "1234 Main St, Dallas, TX 75201"
CLEAN_DEAL_UNITS = 240
CLEAN_DEAL_YEAR_BUILT = 2005
CLEAN_DEAL_PURCHASE_PRICE = 28_000_000


def write_clean_rent_roll(path: Path) -> None:
    """240 units across 1BR/2BR/3BR. In-place rents stable, no nulls."""
    rows: list[list] = []
    rows.append(["unit_id", "unit_type", "sqft", "in_place_rent", "occupancy_status"])
    for i in range(120):  # 1BR
        rows.append([f"A{i+101:03d}", "1BR", 720, 1450, "occupied"])
    for i in range(80):   # 2BR
        rows.append([f"B{i+201:03d}", "2BR", 1050, 1875, "occupied"])
    for i in range(40):   # 3BR
        rows.append([f"C{i+301:03d}", "3BR", 1340, 2300, "occupied"])
    _write_csv(path, rows)


def write_clean_t12(path: Path) -> None:
    """12 months of stable income/expense lines. Sums round-trip cleanly."""
    rows = [
        ["category", "amount"],
        ["gross_potential_rent", 5_400_000],
        ["vacancy_loss", -270_000],
        ["other_income", 180_000],
        ["effective_gross_income", 5_310_000],
        ["payroll", 540_000],
        ["repairs_maintenance", 320_000],
        ["utilities", 410_000],
        ["insurance", 145_000],
        ["real_estate_tax", 525_000],
        ["management_fee", 159_300],
        ["total_opex", 2_099_300],
        ["noi", 3_210_700],
    ]
    _write_csv(path, rows)


def write_clean_om(path: Path) -> None:
    """Plain-text OM with broker claims roughly aligned with T12 data."""
    text = f"""PROJECT ESSEX -- OFFERING MEMORANDUM (SYNTHETIC FIXTURE)

Address: {CLEAN_DEAL_ADDRESS}
Year Built: {CLEAN_DEAL_YEAR_BUILT}
Total Units: {CLEAN_DEAL_UNITS}
Asking Price: ${CLEAN_DEAL_PURCHASE_PRICE:,}

Investment Highlights:
- Stabilized in-place NOI of $3.2M
- Going-in cap rate of 5.2%
- Projected rent growth of 3.0% per year
- Light value-add opportunity: $15,000/unit interior renovation
- Exit cap of 5.8%

Submarket: Dallas / Uptown
Asset Class: Garden-style B+
"""
    # Explicit UTF-8 + newline="\n" so byte-output is stable across machines
    # (Python defaults to locale on Windows; CI runners may or may not be
    # POSIX). The determinism test hashes raw bytes -- any platform-dependent
    # newline / encoding drift would break it.
    path.write_text(text, encoding="utf-8", newline="\n")


def write_clean_deal_room(root: Path) -> None:
    """Top-level builder for synthetic_clean_deal/."""
    root.mkdir(parents=True, exist_ok=True)
    write_clean_om(root / "OM.txt")
    write_clean_rent_roll(root / "rent_roll.csv")
    write_clean_t12(root / "T12.csv")
    (root / "README.md").write_text(
        "# Synthetic clean deal\n\n"
        "Hand-built deterministic fixture. See builders.py.\n",
        encoding="utf-8",
        newline="\n",
    )


def write_missing_t12_deal_room(root: Path) -> None:
    """Same as clean deal but T12.csv intentionally absent.

    Intake should produce canonical_deal.json without `opex_table`
    and emit a `missing_t12` blocker.
    """
    root.mkdir(parents=True, exist_ok=True)
    write_clean_om(root / "OM.txt")
    write_clean_rent_roll(root / "rent_roll.csv")
    # Deliberately: no T12.csv
    (root / "README.md").write_text(
        "# Synthetic missing-T12\n\n"
        "Same as clean deal but no T12. Drives intake_blocker tests.\n",
        encoding="utf-8",
        newline="\n",
    )


def write_om_optimistic_deal_room(root: Path) -> None:
    """OM claims aggressive 5% growth + $10k/unit capex; data implies 3% + $18k.

    Drives delta_flag = "broker_optimistic" assertions in judgment.
    """
    root.mkdir(parents=True, exist_ok=True)
    write_clean_rent_roll(root / "rent_roll.csv")
    write_clean_t12(root / "T12.csv")
    optimistic_text = f"""PROJECT ESSEX -- OFFERING MEMORANDUM (OPTIMISTIC FIXTURE)

Address: {CLEAN_DEAL_ADDRESS}
Year Built: {CLEAN_DEAL_YEAR_BUILT}
Total Units: {CLEAN_DEAL_UNITS}
Asking Price: ${CLEAN_DEAL_PURCHASE_PRICE:,}

Investment Highlights:
- Submarket rent growth of 5.0% per year (broker claim)
- Light $10,000/unit interior renovation
- Exit cap of 5.0% (compressing market)
"""
    (root / "OM.txt").write_text(optimistic_text, encoding="utf-8", newline="\n")
    (root / "README.md").write_text(
        "# Synthetic OM-optimistic\n\n"
        "Broker claims diverge from data; drives delta_flag tests.\n",
        encoding="utf-8",
        newline="\n",
    )


def _write_csv(path: Path, rows: list[list]) -> None:
    """Write rows as CSV with LF line endings + UTF-8 encoding.

    csv.writer is fed an in-memory StringIO with `lineterminator="\\n"` so
    the *content* uses LF unconditionally; we then write the buffer with
    explicit `encoding="utf-8"` and `newline="\\n"` so Path.write_text
    doesn't translate LF to platform-native line endings (Windows would
    otherwise emit CRLF and break the byte-determinism test).
    """
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerows(rows)
    path.write_text(buf.getvalue(), encoding="utf-8", newline="\n")
