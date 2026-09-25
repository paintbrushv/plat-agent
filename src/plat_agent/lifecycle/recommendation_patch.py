"""Step 4.5 — orchestrator recommendation post-step (spec §2.3 + §3).

After underwriting completes, derive recommendation + recommendation_confidence
and atomically patch them into judgment/positioning.json. The judgment subsystem
itself does NOT produce these fields; only the orchestrator does, after seeing
both judgment.confidence and underwriting metrics.

Also enforces §5.4 passthrough guardrail: when judgment_mode != 'deterministic_v1'
the recommendation is forced to NEEDS_DATA regardless of metrics.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from plat_agent.lifecycle.atomic import atomic_write_json
from plat_agent.lifecycle.defaults import (
    JUDGMENT_ENGINE_V1,
    derive_recommendation,
)


def _read_metrics(
    run_dir: Path,
) -> tuple[float | None, float | None, float | None]:
    """Return (levered_irr, min_dscr, cash_on_cash_year_1) from underwriting.

    All three default to None when underwriting hasn't run or the field is
    missing. CoC Y1 was added by V1.5 mfu — older fixtures may not have it.
    """
    summary_path = run_dir / "underwriting" / "deal_summary.json"
    if not summary_path.exists():
        return (None, None, None)
    try:
        payload = json.loads(summary_path.read_text())
        metrics = payload.get("metrics", {}) or {}
        irr = metrics.get("irr", {}).get("levered_irr")
        dscr = metrics.get("dscr", {}).get("minimum_dscr")
        # V1.5: prefer the federation-friendly nested coc.cash_on_cash_year_1;
        # fall back to cash_on_cash.cash_on_cash_year_1 (legacy block).
        coc = (
            metrics.get("coc", {}).get("cash_on_cash_year_1")
            if isinstance(metrics.get("coc"), dict)
            else None
        )
        if coc is None and isinstance(metrics.get("cash_on_cash"), dict):
            coc = metrics["cash_on_cash"].get("cash_on_cash_year_1")
        return (irr, dscr, coc)
    except (json.JSONDecodeError, KeyError):
        return (None, None, None)


def patch_recommendation(
    run_dir: Path,
    *,
    comps_unavailable: bool,
    judgment_mode: str,
    blocker_count: int = 0,
) -> str:
    """Atomically patch recommendation + recommendation_confidence into positioning.json.

    Returns the ISO8601 patched-at timestamp (so orchestrator can set
    state.recommendation_patched_at).
    """
    positioning_path = run_dir / "judgment" / "positioning.json"
    payload = json.loads(positioning_path.read_text())

    levered_irr, min_dscr, coc_y1 = _read_metrics(run_dir)
    judgment_confidence = float(payload.get("positioning", {}).get("confidence", 0.0))
    leverage_source = payload.get("leverage", {}).get("source", "v1_hardcoded")

    # §5.4 passthrough guardrail
    if judgment_mode != JUDGMENT_ENGINE_V1:
        rec = "NEEDS_DATA"
        conf = judgment_confidence
    else:
        rec, conf = derive_recommendation(
            levered_irr=levered_irr,
            min_dscr=min_dscr,
            blocker_count=blocker_count,
            comps_unavailable=comps_unavailable,
            judgment_confidence=judgment_confidence,
            leverage_source=leverage_source,
            cash_on_cash_year_1=coc_y1,
        )

    payload["recommendation"] = rec
    payload["recommendation_confidence"] = conf

    atomic_write_json(positioning_path, payload)
    return datetime.now(timezone.utc).isoformat()
