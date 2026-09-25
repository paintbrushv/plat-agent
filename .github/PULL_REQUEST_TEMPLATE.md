## Summary

<!-- One or two sentences: what changed and why. -->

## Scope check

plat-agent is the orchestration/reasoning layer. Confirm where this change
belongs:

- [ ] This change is orchestration, dispatch, synthesis, or CLI — not domain math (cost → plat-costmodel, cashflow → underwriting engine, comps → market-study).

## Testing

- [ ] New/changed behavior is covered by mocked unit tests (no live MCP/sibling/credential requirements).
- [ ] `pytest tests/ -q` passes locally.

## Commit discipline

- [ ] Commits are DCO-signed (`git commit -s`).
- [ ] No real deal data, personal identity, or private paths are included (the
      public-tree sanitize gate runs in CI and will fail the build otherwise).