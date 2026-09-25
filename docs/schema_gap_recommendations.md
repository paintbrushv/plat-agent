# Federation schema-gap recommendations

**Source:** Three parallel `feature-dev:code-explorer` investigations dispatched 2026-04-25, after the first end-to-end smoke test of the federated underwriting workflow against `demo_on_first_street` surfaced contract impedance issues.

**Status:** Recommendations only — no code changes proposed here. Implement in a follow-up.

---

## Gap 1 — Per-cohort `bedrooms` / `bathrooms` are silently dropped during ingest

### Symptom

`comp-finder` (and any future sibling that filters by bed/bath) requires `bedrooms` and `bathrooms` per cohort, but the canonical schema v0.1 `unit_cohorts` entries store only `{cohort_id, unit_type, unit_count, sqft, initial_inplace_rent}`. The orchestrator currently parses bed/bath from the `unit_type` letter convention (E=0BR, A=1BR, B=2BR, C=3BR) — a heuristic that breaks on property-native unit codes like West Lane's `ab1` / `b4` / `c`.

### Where the data actually lives

The integers ARE computed during ingest. `multifamily-underwriting/engine/ingest/rent_roll_parser.py:91-103` builds `_cohort_key()` using the per-row `beds` and `baths` integers (lines 163-164), then synthesizes a `cohort_id` like `"1BR/1BA"` and writes only `{cohort_id, unit_type, unit_count, sqft, initial_inplace_rent}` at lines 70-76. **Beds and baths are computed and immediately discarded.**

### Recommendation: Option 1 — Extend canonical schema with optional `bedrooms` / `bathrooms`

Add two optional integer fields to the canonical UnitCohorts spec. Since the parser already has them in scope, the change is:

| File | Change | Lines |
|---|---|---|
| `multifamily-underwriting/engine/ingest/rent_roll_parser.py:70-76` | Add `bedrooms` and `bathrooms` to the cohort dict literal | +2 |
| `multifamily-underwriting/docs/canonical_deal_schema_v0_1.md` | UnitCohorts table — add two optional rows | +2 |
| `plat-agent/src/plat_agent/contracts/domain/cost.py` | Mirror the optional fields if needed for cost-bridge requests | +0 (consumer-side already has them) |

**Risk:** Near-zero. Optional fields don't break run_001 / West Lane canonical_inputs.json. The orchestrator's regex fallback in `plat-agent/src/plat_agent/schema_mapper.py:144-153` stays as a backstop for legacy files. No engine module references `bedrooms` (`engine/modules/revenue.py` confirmed clean).

### Adjacent finding to act on

West Lane's cohort IDs use property-native naming (`ab1`, `b4`, `c`) — lowercased and stripped of separators by `rent_roll_parser.py:68`. The regex shim `re.findall(r"(\d+)B", cid)` silently misclassifies `b4` as `beds=4` rather than `beds=2`. **The system has two cohort-key conventions and no canonical mapping between them.** Adopting Option 1 fixes new runs at the source; legacy runs need either a one-shot enrichment pass or to be tolerated as imprecise.

---

## Gap 2 — `scenario-sweeper` consumes legacy `DealInputs`, not canonical schema v0.1

### Symptom

`plat_agent.sweep.run_scenarios` and `find_break_even` require a `DealInputs` object with `base_deal_inputs` populated. They cannot accept a canonical `canonical_inputs.json` directly. The sweeper is therefore unrunnable against any federated deal without a wrapper.

### What the data flow actually looks like

The "legacy DealInputs" wrapper is essentially vestigial. From the explorer's trace (`plat-agent/src/plat_agent/sweep/scenarios.py:79` to `engine.engine.run_underwriting`):

1. `run_scenarios(inputs: DealInputs)` checks `base_deal_inputs is not None` at `scenarios.py:98`
2. `merged = copy.deepcopy(inputs.base_deal_inputs)` at `scenarios.py:120` — extracts the canonical dict
3. From this point on, **everything operates on the canonical dict.** Renovation programs are merged in via `schema_mapper.merge_renovation_programs`. Perturbation in `perturb.py` mutates canonical paths (`exit_assumptions.exit_cap_rate`, `unit_cohorts[*].initial_inplace_rent`). The backend submits the canonical dict to the engine MCP unchanged.

The DealInputs object is used only for `property_id`, `base_deal_inputs`, and the ROI-gate cache.

### Recommendation: Option 1 — Add thin `_from_canonical` entry points

Add two new functions:

```python
def run_scenarios_from_canonical(canonical: dict, preset: str, *, ...) -> ScenarioSweepResult: ...
def find_break_even_from_canonical(canonical: dict, axis: str, bounds, *, ...) -> BreakEvenResult: ...
```

Each synthesizes a minimal `DealInputs` stub populated with `base_deal_inputs=canonical`, `property_id=canonical['metadata']['deal_id']`, and a no-op unit_mix, then delegates to the existing entry points unchanged.

| File | Change | Lines |
|---|---|---|
| `plat-agent/src/plat_agent/sweep/scenarios.py` | New `run_scenarios_from_canonical()` wrapper | +12 |
| `plat-agent/src/plat_agent/sweep/break_even.py` | New `find_break_even_from_canonical()` wrapper | +12 |
| `plat-agent/src/plat_agent/sweep/__init__.py` | Export both | +2 |
| New tests in `tests/test_sweep_canonical.py` | Round-trip assertions | +40 |

**Risk:** Near-zero. No existing test touched. No wire format change. The new entry points skip the ROI gate (canonical deals are assumed pre-validated).

### Hidden gotcha

The `unit_mix` in the synthesized DealInputs is a no-op stub, BUT `perturb.apply_target_rent` (`perturb.py:81`) uses `target_cohort` IDs from renovation_programs to look up cohorts. **If we synthesize unit_mix entries with new cohort IDs, the lookup fails.** Mitigation: pass the canonical dict through with its native cohort IDs intact (Options 1 and 2 do this; Option 3 — a true canonical→DealInputs adapter — would have to preserve them faithfully, which is the dependency that makes Option 3 less attractive).

---

## Gap 3 — No formal `intake_punchlist.md` resolution protocol

### Symptom

When intake-orchestrator returns `status="needs_analyst_input"` with a punchlist (target rents per cohort, missing source documents, OM/rent-roll mismatches), there is no programmatic mechanism for the analyst to signal "I've resolved this — federation can proceed." The downstream orchestrators have no way to verify resolution before dispatching cost-bridge or underwriting.

### Today's reality

There IS no formal step. Both West Lane and First Street manifests show `status: "Raw materials loaded, standardized, and base-case underwriting complete"` — prose narrative, not machine-readable state. Analysts resolve by directly editing `canonical_inputs.json` (visible in the populated `market_rent_curve` per cohort in run_001 outputs), then re-running underwriting manually. `assumptions.md` is a TBD-placeholder template that nothing reads or writes programmatically. **No sentinel files exist.** Deal-input-validator currently has prose instructions to "FAIL if punchlist still has unresolved blocking items" — judgment-based, not deterministic.

### Recommendation: Option A — Punchlist resolution sidecar with content hash binding

Resolution state lives at `<deal_root>/outputs/<run_id>/intake/punchlist_resolution.json`:

```json
{
  "resolved_at": "2026-04-23T14:00:00Z",
  "resolved_by": "analyst_initials",
  "items": [
    {"item": "target_monthly_rent:a1", "resolution": "confirmed_1400", "source": "comp-finder run 2026-04-23"},
    {"item": "target_monthly_rent:b1", "resolution": "confirmed_1700", "source": "same"}
  ],
  "unresolved": [],
  "canonical_deal_json_sha256": "abc123..."
}
```

| File | Change |
|---|---|
| `plat-agent/src/plat_agent/contracts/domain/intake.py` | Add `punchlist_resolution_relative: Optional[str]` to `DealIntakeResponse` |
| `plat-agent/.claude/agents/deal-input-validator.md` | Read sidecar at step 0 — if absent or `unresolved` non-empty, return `status="needs_analyst_input"` |
| `multifamily-underwriting/runs/resolve_punchlist.py` | New thin CLI (~50 lines): prompts analyst inline or opens editor, writes the sidecar with current canonical JSON sha256 |
| `plat-agent/docs/bridging_contract.md` | Document the resolution protocol in the `status="needs_analyst_input"` semantics section |

**Risk:** Low. Sidecar pattern already exists in the contract (`_provenance.json`). Existing deals untouched.

### Edge case worth flagging

The SHA-256 binding catches the most dangerous scenario: analyst resolves the punchlist against a 2026-03-17 rent roll, broker sends a corrected rent roll, ingest re-runs and overwrites canonical_deal.json with a different cohort structure, `punchlist_resolution.json` references a stale hash. **Validator should treat hash mismatch as a hard fail with `code="data_quality"` and message "canonical_deal.json has changed since punchlist was resolved — re-confirm target rents."** Forces re-attestation rather than proceeding on a resolution that covers different data.

For partial resolution (target rents filled but renovation scope still TBD), tier punchlist items as `blocking: true/false`. Non-blocking unresolved items produce `PASS_WITH_WARNINGS`, not `FAIL`.

---

## Implementation order (suggested)

1. **Gap 1 first.** Smallest diff (~4 lines), unblocks comp-finder reliability for all future deals, no contract version bump needed.
2. **Gap 2 second.** Lets the federation actually run scenario sweeps on real deals — currently the sweep step is impossible.
3. **Gap 3 third.** Bigger surface (new contract field, new CLI script, new validator behavior) but no other gap depends on it. Defer until intake is being routinely run on new deals.

Total estimated effort: ~150 lines across 4 repos, plus tests and docs. Each gap is an isolated PR. None require breaking the v1 contract.

---

## What this doc is NOT

- Not a plan: no acceptance criteria, no test specifications, no rollout strategy.
- Not authoritative: each gap should be re-verified against the code at implementation time (federation evolves; these findings are dated 2026-04-25).
- Not exhaustive: only covers the 3 gaps surfaced by the first_street smoke test. Others (intake-orchestrator → market-context handoff convention, sibling agent prompt-conformance tightening, FRED/BLS-backed supply-pipeline-analyst for non-CoStar markets) remain open.
