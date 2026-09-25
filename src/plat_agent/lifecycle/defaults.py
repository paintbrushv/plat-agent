"""V1 default constants and the recommendation derivation rule.

Every constant here is a deliberate V1 commitment per spec §6.A.
Changes require a contract version bump and coordinated update of
fixture goldens. See spec §2.3 (recommendation derivation).
"""

from __future__ import annotations

from typing import Final

# Contract version stamped into every artifact's _provenance.json.
# Bump when any §2 schema changes in a way consumers must care about.
CONTRACT_VERSION: Final[str] = "1.0.0"

# V1 judgment engine identifier (per §2.3 + §5.4 passthrough guardrails).
JUDGMENT_ENGINE_V1: Final[str] = "deterministic_v1"

LEVERAGE_SOURCE_V1_HARDCODED: Final[str] = "v1_hardcoded"
# V1.5: when judgment can size leverage from mfu's compute_agency_loan_terms()
# (vintage available), the leverage block is stamped with this source.
LEVERAGE_SOURCE_AGENCY_DSCR: Final[str] = "agency_dscr_constrained"

DEFAULT_LEVERAGE_V1: Final[dict] = {
    "ltv": 0.65,
    "rate": 0.0575,
    "amort_years": 30,
    "io_months": 12,
    "source": LEVERAGE_SOURCE_V1_HARDCODED,
}

# V1.5 Cash-on-Cash recommendation rule
COC_TARGET_PCT: Final[float] = 0.07   # 7% Year-1 post-debt CoC bar for PROCEED
MIN_DSCR_FOR_PROCEED: Final[float] = 1.20  # spec §2.3 retains 1.20 floor

# Per spec §1 V1 Defaults — broker-claim delta thresholds.
VALIDATION_THRESHOLDS: Final[dict] = {
    "yellow_pct": 0.15,  # ±15% from data-derived value triggers "yellow" delta_flag
    "red_pct": 0.25,     # ±25% triggers "red"
}

# Per-step TTL for is_satisfied() cache check (spec §4.4 #6).
# None means no default TTL (always considered fresh until manually invalidated).
DEFAULT_TTL_DAYS: Final[dict[str, int | None]] = {
    "intake": None,
    "comps": 7,           # comp data ages out fast; force refresh after 7 days
    "judgment": None,
    "underwriting": None,
    "memo": None,
    "crm": None,
}


def derive_recommendation(
    *,
    levered_irr: float | None,
    min_dscr: float | None,
    blocker_count: int,
    comps_unavailable: bool,
    judgment_confidence: float,
    leverage_source: str,
    cash_on_cash_year_1: float | None = None,
) -> tuple[str, float]:
    """V1.5 recommendation derivation rule.

    Returns (recommendation, recommendation_confidence) where:
      - recommendation ∈ {"PROCEED", "DECLINE", "NEEDS_DATA"}
      - recommendation_confidence ∈ [0.0, 1.0]

    Rules (V1.5 — CoC-driven):
      - PROCEED       if cash_on_cash_year_1 >= 7%  AND min_dscr >= 1.20
                      AND blocker_count == 0
      - NEEDS_DATA    if blocker_count > 0
                      OR comps_unavailable
                      OR cash_on_cash_year_1 is None (engine didn't surface CoC)
                      OR min_dscr is None
      - DECLINE       otherwise

    Confidence:
      - When metrics absent: confidence = judgment_confidence
      - Otherwise: min(judgment_confidence, metrics-headroom factor)
        where headroom uses CoC headroom above 7% (clamped 0..1).
      - When leverage_source == LEVERAGE_SOURCE_V1_HARDCODED: cap at 0.6
      - When leverage_source == LEVERAGE_SOURCE_AGENCY_DSCR: cap at 0.85
        (high-quality but still data-derived; full 1.0 reserved for V2 with
        live broker debt matrix).

    Backward-compat note: ``levered_irr`` is preserved on the signature
    because some callers still pass it; it is no longer used in the
    decision but contributes to the headroom factor when CoC is also
    present (more data → tighter confidence).
    """
    # Confidence cap by leverage source
    def _apply_leverage_cap(c: float) -> float:
        if leverage_source == LEVERAGE_SOURCE_V1_HARDCODED:
            return min(c, 0.6)
        if leverage_source == LEVERAGE_SOURCE_AGENCY_DSCR:
            return min(c, 0.85)
        return c

    # NEEDS_DATA when any signal missing
    if min_dscr is None or cash_on_cash_year_1 is None:
        return "NEEDS_DATA", _apply_leverage_cap(judgment_confidence)

    if blocker_count > 0 or comps_unavailable:
        return "NEEDS_DATA", _apply_leverage_cap(judgment_confidence)

    if (
        cash_on_cash_year_1 >= COC_TARGET_PCT
        and min_dscr >= MIN_DSCR_FOR_PROCEED
    ):
        rec = "PROCEED"
    else:
        rec = "DECLINE"

    # CoC headroom: 5% spans roughly +/-2.5pts around the 7% target
    coc_headroom = max(
        0.0, min(1.0, (cash_on_cash_year_1 - COC_TARGET_PCT) / 0.05 + 0.5)
    )
    conf = min(judgment_confidence, coc_headroom)
    return rec, _apply_leverage_cap(conf)
