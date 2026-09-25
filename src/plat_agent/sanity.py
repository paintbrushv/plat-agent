"""Sanity checks for underwriting output metrics.

Thin validation layer that flags metric values outside accepted multifamily
ranges before an analyst acts on them. Bands are deliberately conservative:
"warning" means worth a second look; "error" means physically/financially
implausible.

The Sample Hills deal that motivated this layer cleared the engine's gates
yet returned DSCR 0.58x and a 3.0% going-in cap — both well outside normal
multifamily ranges (4-7% cap, 1.2x+ DSCR). compute_sanity_flags() is the
backstop the analyst sees before trusting the numbers.

Bands are intentionally hardcoded — these are industry-standard multifamily
ranges, not project-specific tunables.
"""

# (lo, hi) — values strictly outside this range get flagged.
GOING_IN_CAP_BAND = (0.04, 0.07)
LEVERED_IRR_PLAUSIBLE_MAX = 0.40
LEVERED_EM_OUTLIER_MAX = 5.0
MIN_DSCR_THRESHOLD = 1.20


def compute_sanity_flags(metrics: dict | None) -> list[dict]:
    """Return a list of sanity-flag dicts for the given normalized metrics.

    Empty list = nothing notable. Each flag has shape:
        {
            "metric": str,            # e.g. "going_in_cap_rate"
            "value": float | None,
            "expected_range": str,    # human-readable band
            "severity": "warning" | "error",
            "message": str,
        }

    Missing or non-success metrics return [] — sanity flags are layered on
    top of, not a replacement for, the engine's own status checks.
    """
    if not metrics or metrics.get("status") != "success":
        return []

    flags: list[dict] = []

    yields = metrics.get("yields") or {}
    cap = yields.get("going_in_cap_rate")
    if cap is not None:
        lo, hi = GOING_IN_CAP_BAND
        if cap < lo:
            flags.append({
                "metric": "going_in_cap_rate",
                "value": cap,
                "expected_range": f"{lo:.0%}-{hi:.0%}",
                "severity": "warning" if cap > 0.03 else "error",
                "message": f"Going-in cap {cap:.2%} is below the typical "
                           f"multifamily range ({lo:.0%}-{hi:.0%}). "
                           "Verify NOI and purchase price.",
            })
        elif cap > hi:
            flags.append({
                "metric": "going_in_cap_rate",
                "value": cap,
                "expected_range": f"{lo:.0%}-{hi:.0%}",
                "severity": "warning" if cap <= 0.10 else "error",
                "message": f"Going-in cap {cap:.2%} is above the typical "
                           f"multifamily range ({lo:.0%}-{hi:.0%}). "
                           "Verify NOI assumptions or distressed pricing.",
            })

    dscr = metrics.get("dscr") or {}
    min_dscr = dscr.get("minimum")
    if min_dscr is not None and min_dscr < MIN_DSCR_THRESHOLD:
        flags.append({
            "metric": "min_dscr",
            "value": min_dscr,
            "expected_range": f">={MIN_DSCR_THRESHOLD:.2f}x",
            "severity": "error" if min_dscr < 1.0 else "warning",
            "message": f"Minimum DSCR {min_dscr:.2f}x is below the "
                       f"{MIN_DSCR_THRESHOLD:.2f}x lender threshold. "
                       "Debt service may not be coverable in worst months.",
        })

    irr = metrics.get("irr") or {}
    levered_irr = irr.get("levered_irr")
    if levered_irr is not None:
        if levered_irr < 0:
            flags.append({
                "metric": "levered_irr",
                "value": levered_irr,
                "expected_range": ">=0",
                "severity": "error",
                "message": f"Levered IRR {levered_irr:.1%} is negative. "
                           "Equity is losing money over the hold.",
            })
        elif levered_irr > LEVERED_IRR_PLAUSIBLE_MAX:
            flags.append({
                "metric": "levered_irr",
                "value": levered_irr,
                "expected_range": f"<={LEVERED_IRR_PLAUSIBLE_MAX:.0%}",
                "severity": "warning",
                "message": f"Levered IRR {levered_irr:.1%} is implausibly high "
                           f"(>{LEVERED_IRR_PLAUSIBLE_MAX:.0%}). "
                           "Verify rent growth, exit cap, and leverage assumptions.",
            })

    em = metrics.get("equity_multiple") or {}
    levered_em = em.get("levered_em")
    if levered_em is not None:
        if levered_em < 1.0:
            flags.append({
                "metric": "levered_em",
                "value": levered_em,
                "expected_range": ">=1.0x",
                "severity": "error",
                "message": f"Levered equity multiple {levered_em:.2f}x is "
                           "below 1.0x. Equity does not return original capital.",
            })
        elif levered_em > LEVERED_EM_OUTLIER_MAX:
            flags.append({
                "metric": "levered_em",
                "value": levered_em,
                "expected_range": f"<={LEVERED_EM_OUTLIER_MAX:.1f}x",
                "severity": "warning",
                "message": f"Levered equity multiple {levered_em:.2f}x is an "
                           f"outlier (>{LEVERED_EM_OUTLIER_MAX:.1f}x). Sanity-check inputs.",
            })

    return flags
