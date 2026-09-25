# plat-agent ↔ sibling-agent bridging contract

**Contract version:** `v1`
**Status:** authoritative
**Owners:** plat-agent (envelope + dispatch); sibling repos (per-domain payloads)

This doc is the source of truth for how plat-agent orchestrators dispatch to subagents living in sibling repositories (plat-costmodel, multifamily-underwriting, market-study-agent, submarket-atlas, supply-demand). Every sibling agent in the federated topology MUST conform to it. Breaking changes require a new contract version (`v2`) running in parallel until siblings migrate.

## 1. Topology

```
                    ┌──────────────────────────┐
                    │       plat-agent         │
                    │   .claude/agents/        │
                    │   ├─ intake-orch         │
                    │   ├─ market-context-orch │
                    │   ├─ cost-orch           │
                    │   ├─ underwriting-orch   │
                    │   ├─ scenario-sweeper    │
                    │   ├─ deal-input-validator│
                    │   └─ deal-memo-writer    │
                    │                          │
                    │   src/plat_agent/        │
                    │   ├─ contracts/          │← this doc
                    │   └─ dispatch/           │
                    └────────────┬─────────────┘
                                 │
              claude -p --cwd ../<sibling_repo>
                                 │
        ┌────────────────┬───────┴────────┬───────────────┬──────────────┐
        ▼                ▼                ▼               ▼              ▼
 plat-costmodel   multifamily-      market-study-   submarket-     supply-demand
                  underwriting      agent           atlas
 .claude/agents/  .claude/agents/   .claude/agents/ .claude/agents/ .claude/agents/
 ├─ cost-bridge   ├─ deal-intake    ├─ comp-finder  └─ submarket-   └─ supply-
 │  -analyst      ├─ underwriting-  └─ demographics    scorer          pipeline-
 └─ schema-       │  runner           -analyst                          analyst
    mapper-       └─ (engine + deals)
    reviewer
```

Sibling repos own their domain logic and the agents that reason about it. plat-agent owns the orchestration topology, the contract, the dispatch helper, and the analyst-facing memo synthesis.

## 2. The envelope

Every cross-repo dispatch uses a `BridgeRequestV1` → `BridgeResponseV1` round-trip. Defined in `src/plat_agent/contracts/envelope.py`.

### Request

```json
{
  "contract_version": "v1",
  "deal_slug": "demo_on_west_lane",
  "run_id": "run_001",
  "deal_root": "/abs/path/to/multifamily-underwriting/runs/deals/demo_on_west_lane",
  "agent_name": "cost-bridge-analyst",
  "payload": { /* domain-specific — see contracts/domain/<area>.py */ },
  "requested_by": "plat-agent"
}
```

### Response

```json
{
  "contract_version": "v1",
  "status": "ok",                    // "ok" | "error" | "needs_analyst_input"
  "deal_slug": "demo_on_west_lane",
  "run_id": "run_001",
  "agent_name": "cost-bridge-analyst",
  "payload": { /* domain-specific response body */ },
  "artifacts": [
    {
      "relative_path": "costmodel/property_estimate.json",
      "kind": "json",
      "description": "Property-level cost estimate (total_high basis).",
      "bytes": 4821
    }
  ],
  "provenance": [
    {
      "source": "plat-costmodel.estimate_property_from_model",
      "locator": "response.totals.total_high",
      "extracted_at": "2026-04-25T23:50:00Z",
      "note": "Conservative bias — total_high used for all gating."
    }
  ],
  "error": null,
  "sanity_flags": ["cap_rate_outside_4_7_band"]
}
```

`error` is required when `status != "ok"` and uses the `BridgeError` shape with a stable `code` for programmatic routing.

## 3. Dispatch mechanism

Headless invocation of the sibling-repo Claude Code session, with the cwd pointing at the sibling repo so its `.claude/agents/`, `AGENTS.md`, and `CLAUDE.md` are in scope.

```python
from plat_agent.contracts import BridgeRequestV1
from plat_agent.contracts.domain.cost import CostBridgeRequest
from plat_agent.dispatch import SiblingRepo, dispatch_sibling_agent

repo = SiblingRepo.from_env_or_default("plat-costmodel", "plat-costmodel")
request = BridgeRequestV1(
    deal_slug="demo_on_west_lane",
    run_id="run_001",
    deal_root="/path/to/runs/deals/demo_on_west_lane",
    agent_name="cost-bridge-analyst",
    payload=CostBridgeRequest(
        canonical_deal_json_relative="standardized/canonical_deal.json"
    ).model_dump(),
)
response = dispatch_sibling_agent(repo, request)
```

Under the hood: `subprocess.run(["claude", "-p", json.dumps(request)], cwd=repo.path, ...)`. The dispatch helper validates that the named agent exists at `<repo>/.claude/agents/<agent_name>.md` before invoking, and parses the last balanced JSON document from stdout.

### Sibling-repo path resolution

By default each sibling lives at `../<repo_name>` relative to plat-agent. Override per-repo with environment variables:

```bash
export PLAT_PLAT_COSTMODEL_PATH=/custom/path/to/plat-costmodel
export PLAT_MULTIFAMILY_UNDERWRITING_PATH=/custom/path/to/multifamily-underwriting
export PLAT_MARKET_STUDY_AGENT_PATH=/custom/path/to/market-study-agent
export PLAT_SUBMARKET_ATLAS_PATH=/custom/path/to/submarket-atlas
export PLAT_SUPPLY_DEMAND_PATH=/custom/path/to/supply-demand
```

## 4. Sibling-agent contract — what every sibling agent MUST do

Every sibling agent is a Markdown file under `<repo>/.claude/agents/<name>.md` with frontmatter and a system prompt. The prompt MUST instruct the agent to:

1. **Treat the user prompt as the JSON-serialized `BridgeRequestV1`.** Parse it, validate the envelope, validate `payload` against the matching `contracts.domain.*` model.
2. **Resolve all paths against `request.deal_root`** — never hard-code paths.
3. **Write artifacts to `<deal_root>/outputs/<run_id>/<domain>/`.** Create the directory if missing. Each artifact gets a `_provenance.json` sibling that mirrors the in-envelope `provenance` list (so artifacts remain self-describing if the envelope is lost).
4. **Emit exactly one `BridgeResponseV1` JSON document to stdout** as the final tool/text output. No surrounding prose, no log lines after the JSON. The dispatch helper parses the *last* balanced JSON document found in stdout, but agents should still avoid emitting intermediate JSON that could confuse a future stricter parser.
5. **Fail with a structured `BridgeError`**, never a raw exception. If the request is malformed or the data is unrecoverable, set `status="error"`, populate `error.code` with a stable identifier, and emit the response normally.
6. **Use `status="needs_analyst_input"`** when the agent reached a decision point only the analyst can resolve (e.g., conflicting unit counts between OM and rent roll). Populate `error.message` with the question; the orchestrator surfaces this to the analyst rather than retrying.

## 5. Artifact convention

Artifacts written by sibling agents land under the deal directory in the multifamily-underwriting repo:

```
multifamily-underwriting/runs/deals/<slug>/outputs/<run_id>/
├── intake/
│   ├── canonical_deal.json
│   └── _provenance.json
├── market_study/
│   ├── comps.json
│   ├── demographics.json
│   └── _provenance.json
├── submarket/
│   ├── score.json
│   └── _provenance.json
├── supply_demand/
│   ├── pipeline.json
│   └── _provenance.json
├── costmodel/
│   ├── property_estimate.json
│   ├── renovation_programs.json
│   └── _provenance.json
├── underwriting/
│   ├── deal_summary.json
│   ├── deal_workbook.xlsx          # if requested
│   └── _provenance.json
├── sweep/
│   ├── scenarios.json
│   ├── break_even_target_rent.json
│   └── _provenance.json
└── memo.md                         # composed by deal-memo-writer
```

`run_id` increments per execution: `run_001`, `run_002`, ... — multiple runs of the same deal accumulate without overwriting. The orchestrator picks the next `run_NNN` by inspecting `outputs/`.

## 6. Versioning

The contract carries an explicit `contract_version` field. Compatible additive changes (new optional fields, new sanity_flag values) stay on `v1`. Breaking changes (renamed/removed fields, semantic shifts) require:

1. Add `BridgeRequestV2` / `BridgeResponseV2` alongside V1 in `contracts/envelope.py`.
2. Dispatch helper accepts both, defaults to whichever the request specifies.
3. Sibling agents migrate one at a time. Their frontmatter or prompt declares which version they speak.
4. Once all siblings have migrated, V1 becomes deprecation-warning-only for one cycle, then is removed.

Per-domain payload models (`contracts.domain.*`) version independently using the same pattern. Most evolution should be additive and stay on V1 for years.

## 7. Error codes

Stable identifiers used in `BridgeError.code`. Sibling agents should reuse these where applicable rather than inventing new ones.

| code | meaning | recoverable |
|---|---|---|
| `missing_input` | A required input file or field was absent | analyst |
| `data_quality` | Input was present but failed sanity checks | analyst |
| `roi_gate_failed` | One or more cohorts did not clear ROI threshold | analyst |
| `not_bracketed` | Bisection bounds did not contain the threshold | retry with wider bounds |
| `sibling_repo_unavailable` | Sibling repo path or agent file missing | environment |
| `external_api_error` | Sibling depends on external service that failed | retry |
| `schema_violation` | Sibling could not produce a valid response payload | bug — file an issue |

## 8. What a sibling agent MUST NOT do

- Write outside `<deal_root>/outputs/<run_id>/<domain>/` (except when the agent's defined purpose is to update deal-level files like `deal_manifest.md` or `assumptions.md`).
- Emit prose, status updates, or partial JSON to stdout AFTER the final `BridgeResponseV1`. Stderr is fine for diagnostics.
- Estimate `target_monthly_rent` for any deal, anywhere, ever. Rent comes from the analyst (informed by `comp-finder` evidence). This is the single hardest rule and the most easily violated.
  - **Note**: `RentValidationRequest`/`RentValidationResponse` (in `contracts/domain/market_study.py`) is a *validation* surface, not an estimation one. It takes the analyst's proposed target rents and a comp set as input and emits per-cohort verdicts (within/above/below the comp IQR). It never sets or alters `target_monthly_rent`. The reference implementation lives in plat-agent at `plat_agent.rent_validation.validate_target_rents` so the surface is usable today; market-study-agent may host a richer implementation later without breaking callers.
- Mutate input artifacts from upstream agents. Treat upstream artifacts as immutable; produce new files in your own domain directory.
- Invoke `claude` recursively to call other sibling agents. Cross-repo orchestration belongs in plat-agent's orchestrators, not in sibling agents — siblings stay focused and composable.

## 9. Cohort namespace uniqueness

Federation runs splice new `renovation_programs` into a canonical deal-inputs dict that already has `unit_cohorts` and may already have analyst-defined `renovation_programs`. The splice must preserve a single, flat namespace of cohort identifiers across both lists.

Invariants enforced by `plat_agent.schema_mapper.splice_renovation_programs_into_canonical`:

1. Every `renovation_program.output_cohort` MUST NOT collide with any existing `unit_cohort.cohort_id`. The Legacy Park root cause was rent-roll-derived `*_renovated` subtotals (e.g. `beal_renovated`, `bradford_renovated`) being silently overwritten by federation output.
2. `output_cohort` is unique across `renovation_programs` UNLESS two programs share the same `target_cohort` (the supported "two strategies into one outcome" pattern).
3. `program_id` is globally unique across the merged `renovation_programs` list.

Naming convention for federation-emitted output_cohorts:

  * Canonical form: `<target>_postreno`
  * Disambiguated form on collision: `<target>_postreno_<short_id>` where `<short_id>` is the trailing path segment of the program_id.

Federation uses the `_postreno` suffix specifically to disambiguate from rent-roll-derived `*_renovated` subtotals that already appear in many canonical deal inputs. **NEVER** use `_renovated` for federation-emitted output_cohorts.

## 10. Payload-model validation

Orchestrators MUST pass `payload_model=<DomainModel>` to `dispatch_sibling_agent` so the inner `payload` is validated against its Pydantic model post-envelope. Without this, malformed sibling responses surface as opaque `KeyError`/`AttributeError` deep in orchestrator code instead of as a structured `BridgeError(code="schema_violation")` at the dispatch boundary.

```python
from plat_agent.contracts.domain.cost import CostBridgeResponse
response = dispatch_sibling_agent(repo, request, payload_model=CostBridgeResponse)
# response.payload is now a CostBridgeResponse instance, not a raw dict.
```

The dispatch helper validates the envelope unconditionally; `payload_model` is what makes the per-domain payload also enforced.

## 11. Per-dispatch on-disk artifacts

Every successful dispatch leaves TWO machine-readable sidecars under `<deal_root>/outputs/<run_id>/<domain>/`:

| File | Writer | Schema | Purpose |
|---|---|---|---|
| `_response_envelope.json` | **plat-agent dispatcher** (`_persist_response_envelope` in `dispatch/sibling.py`) | `BridgeResponseV1` (envelope.py) | Full envelope as returned by the sibling — payload, artifacts, provenance, sanity_flags, error. Stable typed feed for the memo composer and audit. |
| `_provenance.json` | **sibling agent** (per its convention, written via the orchestrator's atomic helper or the sibling's own atomic write) | `UnderwritingProvenance` (for underwriting) or per-sibling extension | Engine-/sibling-specific provenance: `engine_version`, `schema_version`, `inputs_hash_sha256`, `generated_at_utc`, `validator_status`, plus domain-specific extensions. |

### `_response_envelope.json` (dispatcher-written)

The dispatcher writes this automatically after envelope + payload validation succeed. **Orchestrator agents take no action** — calling `dispatch_sibling_agent(...)` is sufficient.

Domain resolution order (used to choose the `<domain>` path segment):
1. The first artifact's `relative_path` (per §5 convention `outputs/<run_id>/<domain>/...`).
2. Fallback table mapping `agent_name` → domain (matches the artifact tree in §5).
3. If neither resolves, persistence is skipped with a warning — there is no safe default subdirectory.

Failure to persist is **non-fatal**: the dispatcher logs a warning to stderr but still returns the parsed `BridgeResponseV1` to the caller. The in-memory response is the source of truth; the on-disk envelope is a best-effort artifact for downstream consumers.

### `_provenance.json` (sibling-/orchestrator-written)

Siblings (and orchestrators that emit provenance themselves) MUST use the atomic temp-then-rename pattern. The dispatcher exposes a public helper:

```python
from plat_agent.dispatch import atomic_write_json

atomic_write_json(target_dir / "_provenance.json", {
    "engine_version": "0.4.2",
    "schema_version": "v1.3",
    "inputs_hash_sha256": "<sha256-of-spliced-canonical>",
    "generated_at_utc": "2026-04-26T14:32:11Z",
    "validator_status": "PASS",
    "feasibility_verdict": "pass",
    # ... domain-specific extensions
})
```

Required core keys (every domain):
- `engine_version` — sibling/engine package version producing this output
- `schema_version` — input schema version validated against
- `inputs_hash_sha256` — hash of the canonical input the sibling ran on
- `generated_at_utc` — ISO-8601 UTC timestamp
- `validator_status` — one of `PASS` / `WARN` / `FAIL` / `SKIPPED`

Recommended extensions for engine-backed siblings (see `UnderwritingProvenance` in `plat_agent.contracts.domain.underwriting`):
- `validator_issues: list[dict]`
- `feasibility_verdict: 'pass'|'marginal'|'fail'`
- `feasibility_sanity_flags: list[str]`
- `feasibility_reasons: list[str]`
- `cap_rate_derivation: dict`
- `dispatch_request_id: str`
- `elapsed_seconds: float`

Non-engine siblings (e.g. `comp-finder`, `submarket-scorer`) MAY omit feasibility/validator fields if not meaningful, but MUST still emit the five core keys plus a domain-specific extension block describing their inputs/outputs. The convention is: required core keys + domain-specific extensions; never silently drop a key just because it isn't applicable.

### Atomicity

Both writers (dispatcher and orchestrator/sibling helper) use a temp-and-rename pattern:

1. Write to `<target>.tmp.<pid>` in the same directory.
2. `Path.replace(<target>)` — atomic on POSIX same-filesystem; on Windows since Python 3.3.
3. On exception, best-effort cleanup of the temp file.

Any reader sees either the prior version of the file or the new one — never a half-written document. Stage 3 HIGH-4 (audit 2026-04-26) traced a partial-`_provenance.json` failure mode to non-atomic sibling writes; this convention closes that gap.

## 12. Validator gate

Federation runs use `engine.engine.run_underwriting(..., federation_mode=True)`. The engine will raise (not silently skip) when:

  * The validator returns FAIL on the spliced canonical.
  * `skip_validation=True` is combined with `federation_mode=True` — this combination is explicitly disallowed, since federation runs cannot trust their inputs without validator coverage.

Sibling agents that produce inputs feeding into the underwriting engine (notably `cost-bridge-analyst` after splice) MUST run the validator on the post-splice canonical and return `BridgeError(code="validation_failed")` rather than emit a `status="ok"` response that would later fail at the engine boundary.

## 13. Comp evidence + reconciliation (Wave 6)

The Legacy Park post-mortem (CONSOLIDATED_FIX_PLAN.md, Family I) showed a
silent killer: `market-study-agent` had never been dispatched for the
deal, yet downstream stages happily consumed broker-derived rent
premiums. The comp output, even when it existed for other deals, never
plumbed back into the canonical's `market_rent_curve`. Wave 6 adds two
federation steps that close this gap.

### Step 1 — `comp_evidence_missing` HARD gate (in `market-context-orchestrator`)

Before the chain can advance past market context, the orchestrator MUST
verify that comp evidence exists for any cohort with a non-zero rent
premium. The premium check covers BOTH encoding paths:

- `revenue_programs[].reno_premium_*` (analyst-side)
- `renovation_programs[].rent_premium_monthly` (federation-side, post-splice)

If `premium_cohorts` is non-empty AND
`outputs/<run_id>/market_study/comps.json` does NOT exist, the
orchestrator raises `BridgeError(code="comp_evidence_missing", recoverable=True)`
with `details.cohort_ids_lacking_evidence`. The federation halts and
the analyst is notified.

When `comps.json` IS present, the orchestrator instead emits a
`comp_reconciliation_required` entry in the dispatch result's
`sanity_flags`. The next federation step consumes that flag.

### Step 2 — `comp-reconciler` advisory step

A new sibling agent `plat-agent/.claude/agents/comp-reconciler.md`
runs AFTER `comp-finder` produces `comps.json` and BEFORE
`underwriting-runner` consumes the canonical. Its job:

1. Read canonical + comps.json.
2. For each cohort with a non-zero rent premium, compute
   `comp_p50_rent` (median asking rent across the comp set for that
   cohort) and compare against the canonical's `market_rent_curve` at
   month 1.
3. If `abs(divergence_pct) > 10%`, emit a HARD WARN
   `comp_disagreement_<cohort>` entry in `sanity_flags`. **Advisory
   only** — the response's `status` MUST remain `"ok"`. The chain
   does NOT block on disagreement.
4. Persist `outputs/<run_id>/market_study/comp_reconciliation.json`
   per §11 atomic-write semantics.

The payload conforms to `CompReconciliationResult` in
`plat_agent.contracts.domain.market_study`:

```json
{
  "market_rent_calibration": [
    {
      "cohort_id": "essex",
      "canonical_market_rent": 1200.0,
      "comp_p50_rent": 1400.0,
      "comp_count": 5,
      "divergence_pct": 16.67,
      "recommendation": "review and consider upward adjustment"
    }
  ],
  "divergence_threshold_pct": 10.0,
  "advisory_only": true,
  "cohorts_lacking_comp_evidence": []
}
```

### Federation dispatch chain (current Wave 6 ordering)

```
intake-orchestrator
  → market-context-orchestrator   [HARD-gate: comp_evidence_missing]
      → comp-finder                (in market-study-agent)
      → demographics-analyst       (in market-study-agent)
      → submarket-scorer           (in submarket-atlas)
      → supply-pipeline-analyst    (in supply-demand)
      → [emit comp_reconciliation_required if premium_cohorts non-empty]
  → cost-orchestrator
      → cost-bridge-analyst        (in plat-costmodel)
  → comp-reconciler                [NEW, Wave 6 — advisory only]
  → underwriting-orchestrator
      → underwriting-runner        (in multifamily-underwriting)
  → scenario-sweeper
  → deal-memo-writer
```

The `comp-reconciler` step is plat-agent-internal: it lives in
`plat-agent/.claude/agents/comp-reconciler.md` and dispatches against
plat-agent's own repo (no sibling-repo subprocess). The
`underwriting-orchestrator` is responsible for invoking it before
dispatching `underwriting-runner` whenever the upstream
`comp_reconciliation_required` flag was emitted.

### Advisory-disagreement convention

The default federation policy (per Wave 6 Q2) is:

- **Authoritative rent source**: analyst (via `target_monthly_rent` /
  `market_rent_curve` in canonical).
- **Comps**: evidence, surfaced as advisory `comp_disagreement_<cohort>`
  flags when median diverges by more than `divergence_threshold_pct`
  (default 10%). Surfaced; not enforced.
- **HARD block** is reserved for `comp_evidence_missing` — the absence
  of evidence is enforceable; the disagreement is advisory because the
  analyst has access to context (renovation tier match, vintage band,
  submarket position) that the median cannot capture.

The memo composer surfaces both numbers + the recommendation per
calibration entry; the verdict / sanity_flags table notes the flag
verbatim. Analysts override by adjusting canonical and re-running, or
accepting the calibration as-is.

## 14. Testing the contract

Round-trip tests live in `tests/test_bridging_contract.py`. When adding a new domain payload, add a test that round-trips it through `BridgeRequestV1.payload` JSON encoding to catch breaking changes early.
