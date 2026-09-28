"""Write the minimum verified underwriting artifacts for lifecycle test doubles."""

import json
from pathlib import Path

from plat_agent.lifecycle.atomic import atomic_write_json
from plat_agent.lifecycle.cache import write_provenance
from plat_agent.lifecycle.steps.underwriting import _compute_engine_inputs_hash


def write_verified_underwriting(run_dir: Path) -> None:
    step_dir = run_dir / "underwriting"
    step_dir.mkdir(exist_ok=True)
    engine_inputs = json.loads((run_dir / "judgment" / "engine_inputs.json").read_text())
    atomic_write_json(step_dir / "inputs.json", engine_inputs)
    atomic_write_json(step_dir / "_response_envelope.json", {})
    write_provenance(step_dir, input_hash="x", status="ok", extra={
        "engine_version": "0.1.0",
        "schema_version": "0.1",
        "inputs_hash_sha256": _compute_engine_inputs_hash(engine_inputs),
        "generated_at_utc": "2026-07-20T00:00:00Z",
        "validator_status": "PASS",
        "validator_issues": [],
        "feasibility_verdict": "marginal",
        "feasibility_sanity_flags": [],
        "feasibility_reasons": [],
        "cap_rate_derivation": None,
    })
