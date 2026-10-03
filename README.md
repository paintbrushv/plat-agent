# plat-agent

Orchestration agent for multifamily deal analysis. Plat composes two domain tools into a single deal workflow:

- **plat-costmodel** — cost estimates, ROI gating, SOW generation (MCP).
- **multifamily-underwriting** — IRR, EM, DSCR, cap rates, waterfall, Excel/PDF (MCP + direct import).

Plat is the *reasoning* layer. It does not model cost or cashflow itself — it orchestrates the tools that do, synthesizes their output, and enforces gates (ROI thresholds, feasibility, conservative-bias defaults) across the whole deal.

## What Plat does

Given structured `DealInputs` (property facts, unit mix, analyst rent assumptions, and optionally a canonical base-deal schema), `analyze_deal()`:

1. Calls `plat-costmodel` for a property-level cost estimate, exterior CapEx, and risk flags.
2. Calls `plat-costmodel` per unit type to apply the ROI gate and produce a renovation program.
3. Maps the bridge output into the canonical deal schema (`renovation_programs`).
4. If a base deal was provided and all unit types clear the ROI gate, runs the underwriting engine and returns IRR / EM / DSCR / cap rates alongside a feasibility verdict.

The output is a `DealAnalysis` containing per-unit-type results, property totals, risk flags, the underwriting-ready `renovation_programs` array, and a human-readable summary.

## Quick start

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,intake]"

# Reviewed public TEST-001 adapters (installable wheels from exact commits).
pip install "git+https://github.com/paintbrushv/plat-costmodel.git@1f82087dc7db1715f44336467c50a92b16748903"
pip install "git+https://github.com/paintbrushv/plat-multifamily-underwriting.git@96dade6530ef9032c1294e41afaeb7ac8da8f1ba"
pip install "git+https://github.com/paintbrushv/plat-harness.git@a57acc496b1bc945d22c1db8bd1ea324fb8ef697"

# Mocked unit tests (no live servers required)
pytest tests/ -q

# CLI
plat analyze --deal-file tests/fixtures/sample_hills_deal.json
plat check-inputs --deal-file deal.json
```

The installed costmodel and underwriting distributions are checked against reviewed package versions and complete packaged content digests before TEST-001 calculations, both default MCP servers, and direct underwriting/scenario/agency calls. These paths do not resolve sibling source checkouts. The current underwriting MCP adapter is `plat.underwriting.mcp/1` in producer package 0.1.1. Federated prompt dispatch still uses configured sibling or host services; see *Environment variables* below.

## Environment variables

```bash
# Optional server override; the default uses the installed, verified package
# in an isolated child interpreter with MCP 1.x.
export PLAT_COSTMODEL_CMD="/path/to/.venv/bin/python -m plat_costmodel.server"

# Optional host override; the default uses the pinned installed producer
export UNDERWRITING_MCP_CMD="/path/to/python -m engine.mcp_server"

# Optional legacy path override; direct calls use the verified installed engine by default
export UNDERWRITING_ENGINE_PATH="/path/to/multifamily-underwriting"

# Optional host-owned deal data directory (contains <deal-slug>/engine_inputs.json
# or <deal-slug>/standardized/canonical_deal.json); independent of source code
export PLAT_DEALS_ROOT="/private/deal-data"

# Azure-backed underwriting runs. When ENDPOINT is set, sweep's make_backend("auto")
# picks AzureRunBackend; otherwise local. Use --backend local to force local.
export UNDERWRITING_AZURE_ENDPOINT="https://your-endpoint.example.net"
export UNDERWRITING_AZURE_KEY="..."
```

## Deal input shape

```json
{
  "property_id": "PROP_DALLAS_001",
  "total_units": 100,
  "year_built": 1985,
  "property_class": "C",
  "market": "dallas",
  "building_type": "garden",
  "start_month": "2026-06",
  "monthly_pace": 10,
  "downtime_days": 21,
  "renovation_strategy": "on_turnover",
  "unit_mix": [
    {
      "sqft": 850, "bedrooms": 2, "bathrooms": 1, "count": 60,
      "current_monthly_rent": 900, "target_monthly_rent": 1100
    }
  ],
  "base_deal_inputs": { "...canonical schema v0.1...": "" }
}
```

When `base_deal_inputs` is present, the generated `renovation_programs` are merged into it and the underwriting engine is invoked for full metrics.

## Design boundaries

- **Rent is never derived by Plat.** Both `current_monthly_rent` and `target_monthly_rent` come from the analyst. Rent comp evidence comes from a separate market-study sibling; that handoff still produces an analyst decision — not a Plat-internal estimate.
- **Conservative bias.** All ROI / cost gates use plat-costmodel's `total_high` values.
- **All unit types must clear the ROI threshold** for `ready_to_underwrite = True`.
- **Deal data never lives here.** This repository is code-only; deal materials and generated artifacts live in the underwriting sibling's `runs/deals/` tree.

## Honest limitations

- **Federated siblings are not bundled.** The federated workflow dispatches to separate cost, underwriting, and market-study repos. The reviewed cost and underwriting engines are public installable packages for TEST-001; some agent-prompt dispatch flows still need a configured sibling repo. The default mocked tests run offline.
- **Cross-repo integration tests are opt-in.** Tests marked `integration` require explicitly configured sibling repositories and skip otherwise; tests marked `e2e_live` hit live external services and are excluded from CI.
- **Live smoke is manual.** `tests/smoke_test_live.py` spawns real MCP subprocesses and is run manually, not by the suite.
- **Versioned TEST-001 adapter.** The reviewed installed packages are required for the synthetic interior/roof/underwriting calculation. An absent package or different build refuses calculations. The package version alone is insufficient because these producers currently label multiple commits `0.1.0`; adapter v1 also checks all packaged source and data files against the reviewed commits.
- **Validation is synthetic-only.** The lifecycle fixtures are hand-built synthetic deals; no real deal data ships with this repository.

## Sanitize gate

`tests/test_sanitize.py` fails the suite if private identity literals (real deal/property names, personal identity, private host paths) appear anywhere in the tree. It runs with the default suite.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). By contributing you agree your contributions are licensed under Apache-2.0 (see [LICENSE](LICENSE)).

## Backsolve assumptions (v0.1 candidate)

House base-case synthesis now uses the content-pinned public underwriting API
through its CLI. A zero/missing purchase price requires both
`metadata.property_summary.backsolve_policy` (version `plat.backsolve-policy/1`
and strategy) and `backsolve_benchmark` (`rate`, `as_of`, `source`). A dated,
sourced `debt_guidance.hold_matched_recommendation` is also accepted. Missing
metadata refuses; the agent no longer inserts a Treasury rate or house strategy.
The child has a 120-second timeout and an exhausted search does not pass as a
solved price. See the producer's `docs/BACKSOLVE_API.md` for the complete contract.
Historical V1/V2/V3 package pins and public thesis fixtures v3/v4 remain unchanged;
new synthetic evidence is in fixture v5.

## MCP 2 packaging candidate

Candidate 0.1.1 uses MCP 2.3–2.x and a refreshed `uv.lock`. Use
`uv sync --locked --extra dev --extra intake` for development, then install the
reviewed producer commits in the setup instructions above. CI uses those same
immutable pins and verifies their package content digests before stdio calls.
Current adapters are `COSTMODEL_V2`, `UNDERWRITING_V5`, and
`UNDERWRITING_MCP_V4`; prior identities and thesis fixtures remain unchanged.
Fixture v6 records the new source identities with unchanged financial expectations.

CI also builds the wheel/source archive, installs the agent wheel, runs
`scripts/verify_installed.py` outside the checkout with source overrides removed,
and checks both actual MCP servers using only synthetic data. Tagged publication
requires full tests, matching runtime/package/tag versions, and an unused PyPI
version. No package publication is part of this candidate change.
