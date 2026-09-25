# tests/lifecycle/test_crm_read_helpers.py
from datetime import datetime, timezone
import json
from pathlib import Path

from plat_agent.lifecycle.crm import (
    CRMRow,
    atomic_append,
    crm_path,
    get_latest,
    list_deals,
)


def _row(slug: str, run: str, ts: datetime, **kw) -> CRMRow:
    base = dict(
        deal_slug=slug, run_id=run, ts=ts,
        status="memo_ready",
        recommendation="PROCEED",
        memo_path="/m", address="x", units=1, asking_price=1,
    )
    base.update(kw)
    return CRMRow(**base)


def test_list_deals_returns_all_rows(tmp_path: Path) -> None:
    p = crm_path(tmp_path)
    atomic_append(p, _row("a", "r1", datetime(2026, 5, 1, tzinfo=timezone.utc)))
    atomic_append(p, _row("b", "r1", datetime(2026, 5, 2, tzinfo=timezone.utc)))
    atomic_append(p, _row("a", "r2", datetime(2026, 5, 3, tzinfo=timezone.utc)))

    rows = list_deals(p)
    assert len(rows) == 3
    assert {r.deal_slug for r in rows} == {"a", "b"}


def test_list_deals_returns_empty_list_when_file_missing(tmp_path: Path) -> None:
    p = crm_path(tmp_path)  # not yet created
    assert list_deals(p) == []


def test_list_deals_filter_by_deal_slug(tmp_path: Path) -> None:
    p = crm_path(tmp_path)
    atomic_append(p, _row("a", "r1", datetime(2026, 5, 1, tzinfo=timezone.utc)))
    atomic_append(p, _row("b", "r1", datetime(2026, 5, 2, tzinfo=timezone.utc)))
    atomic_append(p, _row("a", "r2", datetime(2026, 5, 3, tzinfo=timezone.utc)))

    rows = list_deals(p, filter={"deal_slug": "a"})
    assert len(rows) == 2
    assert all(r.deal_slug == "a" for r in rows)


def test_list_deals_filter_by_recommendation(tmp_path: Path) -> None:
    p = crm_path(tmp_path)
    atomic_append(p, _row("a", "r1", datetime(2026, 5, 1, tzinfo=timezone.utc),
                          recommendation="PROCEED"))
    atomic_append(p, _row("b", "r1", datetime(2026, 5, 2, tzinfo=timezone.utc),
                          recommendation="DECLINE"))
    atomic_append(p, _row("c", "r1", datetime(2026, 5, 3, tzinfo=timezone.utc),
                          recommendation="NEEDS_DATA"))
    proceed = list_deals(p, filter={"recommendation": "PROCEED"})
    assert len(proceed) == 1 and proceed[0].deal_slug == "a"


def test_list_deals_filter_combined(tmp_path: Path) -> None:
    p = crm_path(tmp_path)
    atomic_append(p, _row("a", "r1", datetime(2026, 5, 1, tzinfo=timezone.utc),
                          recommendation="PROCEED"))
    atomic_append(p, _row("a", "r2", datetime(2026, 5, 2, tzinfo=timezone.utc),
                          recommendation="DECLINE"))
    rows = list_deals(p, filter={"deal_slug": "a", "recommendation": "DECLINE"})
    assert len(rows) == 1
    assert rows[0].run_id == "r2"


def test_list_deals_tolerates_null_optional_fields(tmp_path: Path) -> None:
    """Per §2.6: read helpers must tolerate null in any Optional field."""
    p = crm_path(tmp_path)
    # Row with maximal nulls in optional fields
    atomic_append(p, _row("a", "r1", datetime(2026, 5, 1, tzinfo=timezone.utc),
                          levered_irr=None, equity_multiple=None,
                          going_in_cap=None, ppu=None, vintage=None,
                          recommendation_confidence=None,
                          workbook_path=None, thesis_path=None))
    rows = list_deals(p)
    assert len(rows) == 1
    assert rows[0].levered_irr is None


def test_get_latest_returns_most_recent_by_ts(tmp_path: Path) -> None:
    """Per §2.6: latest row per deal_slug is crm.get_latest()."""
    p = crm_path(tmp_path)
    atomic_append(p, _row("a", "r1", datetime(2026, 5, 1, tzinfo=timezone.utc)))
    atomic_append(p, _row("a", "r2", datetime(2026, 5, 3, tzinfo=timezone.utc)))
    atomic_append(p, _row("a", "r3", datetime(2026, 5, 2, tzinfo=timezone.utc)))

    latest = get_latest(p, "a")
    assert latest is not None
    assert latest.run_id == "r2"  # ts=2026-05-03 is most recent


def test_get_latest_returns_none_when_no_rows_for_slug(tmp_path: Path) -> None:
    p = crm_path(tmp_path)
    atomic_append(p, _row("a", "r1", datetime(2026, 5, 1, tzinfo=timezone.utc)))
    assert get_latest(p, "b") is None


def test_get_latest_ignores_unrelated_legacy_recommendation_rows(tmp_path: Path) -> None:
    p = crm_path(tmp_path)
    legacy = _row(
        "legacy",
        "r1",
        datetime(2026, 5, 1, tzinfo=timezone.utc),
    ).model_dump(mode="json")
    legacy["recommendation"] = "NEEDS_REVIEW"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(legacy) + "\n", encoding="utf-8")
    atomic_append(p, _row("target", "r1", datetime(2026, 5, 2, tzinfo=timezone.utc)))

    latest = get_latest(p, "target")

    assert latest is not None
    assert latest.deal_slug == "target"


def test_get_latest_returns_none_when_file_missing(tmp_path: Path) -> None:
    assert get_latest(crm_path(tmp_path), "anything") is None


def test_get_latest_handles_rerun_same_slug_run_id(tmp_path: Path) -> None:
    """Per §2.6: re-runs of (deal_slug, run_id) APPEND. Latest by ts wins."""
    p = crm_path(tmp_path)
    atomic_append(p, _row("a", "r1", datetime(2026, 5, 1, 10, tzinfo=timezone.utc),
                          recommendation="NEEDS_DATA"))
    atomic_append(p, _row("a", "r1", datetime(2026, 5, 1, 14, tzinfo=timezone.utc),
                          recommendation="PROCEED"))
    latest = get_latest(p, "a")
    assert latest.recommendation == "PROCEED"


def test_list_deals_skips_blank_lines(tmp_path: Path) -> None:
    """Defensively tolerate trailing empty lines (analyst could `cat >> file`)."""
    p = crm_path(tmp_path)
    atomic_append(p, _row("a", "r1", datetime(2026, 5, 1, tzinfo=timezone.utc)))
    # Append blank lines manually
    with open(p, "a", encoding="utf-8") as f:
        f.write("\n\n   \n")
    rows = list_deals(p)
    assert len(rows) == 1
