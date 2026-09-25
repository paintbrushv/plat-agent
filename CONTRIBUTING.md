# Contributing to plat-agent

Thanks for your interest in contributing.

## Development setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,intake]"
```

## Running the tests

```bash
pytest tests/ -q
```

All default tests are mocked — no live services, sibling repositories, or
credentials are required. Live end-to-end tests (`-m e2e_live`) and
cross-repository integration tests (`-m integration`) are opt-in and skip
when their environment is absent.

**Optional dependencies:** tests that exercise XLSX intake or PDF memo
rendering are skipped with a reason when `openpyxl` / `reportlab` are
absent; install the `intake` extra for the former.

## Scope and boundaries

- **Plat is the reasoning/orchestration layer.** Cost modeling belongs in
  `plat-costmodel`; cashflow mechanics belong in the underwriting engine;
  rent comps belong in the market-study sibling. plat-agent orchestrates
  and synthesizes — it must not grow domain math.
- **Tests stay mocked by default.** A unit test that requires a live MCP
  server, a sibling checkout, or credentials is a bug.
- **Schema contracts are load-bearing.** `schema_mapper.py` is the only
  place where the bridge ↔ canonical translation lives.

## Pull requests

- Keep changes minimal and scoped; explain the reasoning layer impact.
- New features ship with tests. Bug fixes ship with a regression test.
- Your commits must be DCO-signed (`git commit -s`).
- By contributing, you agree your contributions are licensed under the
  Apache-2.0 license (see LICENSE).