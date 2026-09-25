# SECURITY.md — plat-agent

## Reporting a vulnerability

Please report suspected vulnerabilities privately. Do **not** open a public
issue with exploit details. Contact the maintainers through the private
channel listed in the README (or GitHub's "Report a vulnerability" flow on
this repository) with a description, reproduction steps, and impact
assessment.

## Security posture

- **No network calls in the default test suite.** All unit tests run
  against mocks; live end-to-end tests are opt-in (`-m e2e_live`) and
  require explicit credentials in the environment.
- **Subprocess dispatch is explicit.** `dispatch_sibling_agent()` invokes
  a named sibling agent's prompt inside a sibling repository. Sibling
  repositories and agent names are resolved from environment variables
  (`PLAT_<NAME>_PATH`) or a `../<repo>` default; no implicit downloads,
  and prompts are read from the sibling repo's `.claude/agents/` directory.
- **Credentials are never stored in this repository.** Cloud/tenant
  credentials belong in environment variables or your secret manager.
- **Deal data is out of scope by design.** This repository is code-only;
  deal materials and generated artifacts live in sibling repositories and
  are never committed here.

## Honest limitations

- The lifecycle orchestrator shells out to sibling agent processes
  (`claude -p`-style headless dispatch). If you point it at an untrusted
  sibling repository, the sibling's prompt is executed by that tool —
  only dispatch into repositories you control.
- There is no memory/CPU cap on in-process parsing of analyst-provided
  spreadsheets; input size bounds are applied where documented, but
  do not feed untrusted documents to this pipeline in a shared process.