# tests/contracts/test_comps_artifact.py
import pytest
from pydantic import ValidationError

from plat_agent.contracts.domain.market_study import (
    CompsArtifact,
    CompsArtifactComp,
    CompsArtifactSubject,
    CompsArtifactUnitType,
)


def _minimal_payload() -> dict:
    return {
        "subject": {"address": "123 Main St", "metro_slug": "dallas_tx"},
        "as_of": "2026-05-06",
        "comps": [
            {
                "comp_id": "comp_001",
                "name": "Foo Apartments",
                "address": "456 Side St",
                "units": 240,
            }
        ],
    }


def test_minimal_payload_validates() -> None:
    artifact = CompsArtifact.model_validate(_minimal_payload())
    assert artifact.subject.metro_slug == "dallas_tx"
    assert len(artifact.comps) == 1
    assert artifact.comps[0].comp_id == "comp_001"


def test_missing_required_subject_address_fails() -> None:
    payload = _minimal_payload()
    del payload["subject"]["address"]
    with pytest.raises(ValidationError):
        CompsArtifact.model_validate(payload)


def test_missing_required_metro_slug_fails() -> None:
    payload = _minimal_payload()
    del payload["subject"]["metro_slug"]
    with pytest.raises(ValidationError):
        CompsArtifact.model_validate(payload)


def test_missing_required_as_of_fails() -> None:
    payload = _minimal_payload()
    del payload["as_of"]
    with pytest.raises(ValidationError):
        CompsArtifact.model_validate(payload)


def test_empty_comps_array_fails() -> None:
    # §2.2: comps[] requires ≥1 entry
    payload = _minimal_payload()
    payload["comps"] = []
    with pytest.raises(ValidationError):
        CompsArtifact.model_validate(payload)


def test_comp_missing_required_id_fails() -> None:
    payload = _minimal_payload()
    del payload["comps"][0]["comp_id"]
    with pytest.raises(ValidationError):
        CompsArtifact.model_validate(payload)


def test_optional_unit_types_omitted_validates() -> None:
    # unit_types[] is optional per §2.2
    artifact = CompsArtifact.model_validate(_minimal_payload())
    assert artifact.comps[0].unit_types == []


def test_full_payload_with_optional_fields_validates() -> None:
    payload = _minimal_payload()
    payload["comps"][0].update(
        {
            "distance_miles": 1.2,
            "year_built": 2005,
            "tier": "mid_market",
            "unit_types": [
                {
                    "unit_type": "1BR",
                    "sqft": 720,
                    "face_rent": 1500.0,
                    "effective_rent": 1450.0,
                    "rent_psf": 2.01,
                    "units_available": 8,
                    "concession": "1 month free",
                }
            ],
        }
    )
    payload["submarket_aggregates"] = {
        "rent_growth_trailing_12mo": 0.038,
        "submarket_vacancy": 0.06,
        "comp_count": 6,
    }
    artifact = CompsArtifact.model_validate(payload)
    assert artifact.submarket_aggregates is not None
    assert artifact.submarket_aggregates.comp_count == 6
    assert artifact.comps[0].unit_types[0].concession == "1 month free"
