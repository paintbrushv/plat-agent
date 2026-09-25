"""Wave 2 Task 2.2 — UnderwritingProvenance Pydantic model.

Audit Stage 5 HIGH-1/HIGH-2: the underwriting-runner emits a `_provenance.json`
sidecar but the schema is currently free-form. Standardizing it lets the
orchestrator pass `payload_model=UnderwritingProvenance` to
`dispatch_sibling_agent` and reject malformed sidecars at the boundary.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from plat_agent.contracts.domain.underwriting import UnderwritingProvenance


def _minimal_valid_kwargs() -> dict:
    return {
        "engine_version": "0.4.2",
        "schema_version": "v1.3",
        "inputs_hash_sha256": "a" * 64,
        "generated_at_utc": datetime(2026, 4, 26, 12, 0, 0, tzinfo=timezone.utc),
        "validator_status": "PASS",
        "validator_issues": [],
        "feasibility_verdict": "pass",
        "feasibility_sanity_flags": [],
        "feasibility_reasons": [],
    }


def test_underwriting_provenance_required_fields() -> None:
    """All eight non-optional fields must be present; omitting any raises."""
    full = _minimal_valid_kwargs()
    # Sanity: full kwargs must construct.
    prov = UnderwritingProvenance(**full)
    assert prov.engine_version == "0.4.2"
    assert prov.cap_rate_derivation is None
    assert prov.dispatch_request_id is None
    assert prov.elapsed_seconds is None

    required = [
        "engine_version",
        "schema_version",
        "inputs_hash_sha256",
        "generated_at_utc",
        "validator_status",
        "feasibility_verdict",
    ]
    for field in required:
        bad = dict(full)
        del bad[field]
        with pytest.raises(ValidationError) as excinfo:
            UnderwritingProvenance(**bad)
        assert field in str(excinfo.value), (
            f"missing-field error for {field!r} did not mention the field name: {excinfo.value}"
        )


def test_underwriting_provenance_serializable_to_json() -> None:
    """Pydantic JSON serialization round-trips — necessary because the
    runner persists this to outputs/<run_id>/underwriting/_provenance.json
    and the orchestrator (or memo writer) re-loads it later."""
    prov = UnderwritingProvenance(
        **_minimal_valid_kwargs(),
        cap_rate_derivation={"going_in": 0.055, "exit": 0.0625, "noi_source": "engine"},
        dispatch_request_id="req-001",
        elapsed_seconds=12.5,
    )

    encoded = prov.model_dump_json()
    parsed = json.loads(encoded)
    assert parsed["engine_version"] == "0.4.2"
    assert parsed["validator_status"] == "PASS"
    assert parsed["feasibility_verdict"] == "pass"
    assert parsed["cap_rate_derivation"]["going_in"] == 0.055
    assert parsed["dispatch_request_id"] == "req-001"
    assert parsed["elapsed_seconds"] == 12.5

    # And the round-trip back through the model preserves everything.
    rebuilt = UnderwritingProvenance.model_validate_json(encoded)
    assert rebuilt == prov


def test_validator_status_enum_strict() -> None:
    """validator_status is constrained to PASS/WARN/FAIL/SKIPPED. Anything
    else (including lowercase variants) must fail validation — the runner's
    classification logic relies on the literal."""
    for ok_status in ("PASS", "WARN", "FAIL", "SKIPPED"):
        prov = UnderwritingProvenance(
            **{**_minimal_valid_kwargs(), "validator_status": ok_status}
        )
        assert prov.validator_status == ok_status

    for bad_status in ("pass", "ok", "FATAL", "", "unknown"):
        with pytest.raises(ValidationError):
            UnderwritingProvenance(
                **{**_minimal_valid_kwargs(), "validator_status": bad_status}
            )


def test_feasibility_verdict_enum_strict() -> None:
    """feasibility_verdict is constrained to pass/marginal/fail (lowercase)."""
    for ok_verdict in ("pass", "marginal", "fail"):
        prov = UnderwritingProvenance(
            **{**_minimal_valid_kwargs(), "feasibility_verdict": ok_verdict}
        )
        assert prov.feasibility_verdict == ok_verdict

    for bad_verdict in ("PASS", "Pass", "ok", "warn", ""):
        with pytest.raises(ValidationError):
            UnderwritingProvenance(
                **{**_minimal_valid_kwargs(), "feasibility_verdict": bad_verdict}
            )


def test_real_legacy_park_provenance_loads() -> None:
    """Mock a Legacy Park-shaped provenance sidecar (no real on-disk JSON exists
    in plat-agent's tree; the actual file lives under
    multifamily-underwriting/runs/deals/demo_at_legacy_park/outputs/...).
    This test asserts the model accepts a realistic payload shape."""
    legacy_park_like = {
        "engine_version": "0.4.2",
        "schema_version": "v1.3",
        "inputs_hash_sha256": "f" * 64,
        "generated_at_utc": "2026-04-26T14:32:11Z",
        "validator_status": "WARN",
        "validator_issues": [
            {
                "field": "renovation_programs[2].output_cohort",
                "severity": "WARN",
                "message": "potential collision with existing cohort_id",
            }
        ],
        "feasibility_verdict": "marginal",
        "feasibility_sanity_flags": [
            "cap_rate_outside_4_7_band",
            "irr_above_30_pct",
        ],
        "feasibility_reasons": [
            "going_in_cap=8.1% > 7%",
            "levered_irr=46.98% > 30%",
        ],
        "cap_rate_derivation": {
            "going_in": 0.081,
            "exit": 0.0625,
            "noi_source": "engine",
        },
        "dispatch_request_id": "legacy_park-run_001",
        "elapsed_seconds": 18.4,
    }

    prov = UnderwritingProvenance.model_validate(legacy_park_like)
    assert prov.validator_status == "WARN"
    assert prov.feasibility_verdict == "marginal"
    assert len(prov.feasibility_sanity_flags) == 2
    assert prov.cap_rate_derivation["going_in"] == 0.081
    # generated_at_utc parsed into a real datetime.
    assert isinstance(prov.generated_at_utc, datetime)
