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

# Mocked unit tests (no live servers required)
pytest tests/ -q

# CLI
plat analyze --deal-file tests/fixtures/sample_hills_deal.json
plat check-inputs --deal-file deal.json
```

Configure MCP endpoints via environment variables (see *Environment variables* below) if the sibling repos live outside the default `../<repo>` paths.

## Environment variables

```bash
# plat-costmodel MCP server command (default: python -m plat_costmodel.server)
export PLAT_COSTMODEL_CMD="/path/to/.venv/bin/python -m plat_costmodel.server"

# Underwriting engine MCP server command
export UNDERWRITING_MCP_CMD="/path/to/python /path/to/multifamily-underwriting/server.py"

# Underwriting engine path for direct import (default: ../multifamily-underwriting)
export UNDERWRITING_ENGINE_PATH="/path/to/multifamily-underwriting"

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

- **Sibling repositories are not bundled.** The federated workflow dispatches to sibling repos (`plat-costmodel`, `multifamily-underwriting`, and market-study siblings) that are separate private projects; without them, the single-process `analyze_deal()` path needs at least `plat-costmodel` via MCP. The default test suite is fully mocked and runs offline.
- **Cross-repo integration tests are opt-in.** Tests marked `integration` require explicitly configured sibling repositories and skip otherwise; tests marked `e2e_live` hit live external services and are excluded from CI.
- **Live smoke is manual.** `tests/smoke_test_live.py` spawns real MCP subprocesses and is run manually, not by the suite.
- **Pre-existing test failures.** At the release import baseline, 61 tests in the suite fail for environment reasons (missing sibling engine import, fake-subprocess drift in tests, lifecycle state drift) — the same set of failures is present in the upstream tree and none are introduced by the public packaging. The default suite runs green apart from these pre-existing failures; failure-set parity is the release gate, tracked in the release evidence.
- **Validation is synthetic-only.** The lifecycle fixtures are hand-built synthetic deals; no real deal data ships with this repository.

## Sanitize gate

`tests/test_sanitize.py` fails the suite if private identity literals (real deal/property names, personal identity, private host paths) appear anywhere in the tree. It runs with the default suite.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). By contributing you agree your contributions are licensed under Apache-2.0 (see [LICENSE](LICENSE)).