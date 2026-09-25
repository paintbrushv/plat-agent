"""Tests for DealInputs and DealAnalysis models."""

import pytest
from pydantic import ValidationError

from plat_agent.models import DealAnalysis, DealInputs, UnitMixEntry, UnitTypeResult


class TestUnitMixEntry:
    def test_basic(self):
        u = UnitMixEntry(sqft=850, bedrooms=2, bathrooms=1,
                         current_monthly_rent=900, target_monthly_rent=1050)
        assert u.count == 1
        assert u.scope_level == "standard_value_add"
        assert u.finish_tier == "basic"

    def test_count_gt_1(self):
        u = UnitMixEntry(sqft=850, bedrooms=2, bathrooms=1, count=30,
                         current_monthly_rent=900, target_monthly_rent=1050)
        assert u.count == 30

    def test_zero_sqft_rejected(self):
        with pytest.raises(ValidationError):
            UnitMixEntry(sqft=0, bedrooms=2, bathrooms=1,
                         current_monthly_rent=900, target_monthly_rent=1050)

    def test_zero_bathrooms_rejected(self):
        with pytest.raises(ValidationError):
            UnitMixEntry(sqft=850, bedrooms=2, bathrooms=0,
                         current_monthly_rent=900, target_monthly_rent=1050)

    def test_studio_allowed(self):
        u = UnitMixEntry(sqft=500, bedrooms=0, bathrooms=1,
                         current_monthly_rent=750, target_monthly_rent=850)
        assert u.bedrooms == 0

    def test_zero_count_rejected(self):
        with pytest.raises(ValidationError):
            UnitMixEntry(sqft=850, bedrooms=2, bathrooms=1, count=0,
                         current_monthly_rent=900, target_monthly_rent=1050)

    def test_zero_rent_rejected(self):
        with pytest.raises(ValidationError):
            UnitMixEntry(sqft=850, bedrooms=2, bathrooms=1,
                         current_monthly_rent=0, target_monthly_rent=1050)


class TestDealInputs:
    def _make_mix(self):
        return [UnitMixEntry(sqft=850, bedrooms=2, bathrooms=1,
                             current_monthly_rent=900, target_monthly_rent=1050)]

    def test_minimal(self):
        d = DealInputs(
            property_id="PROP001",
            total_units=10,
            unit_mix=self._make_mix(),
            start_month="2026-06",
            monthly_pace=5,
        )
        assert d.downtime_days == 21
        assert d.roi_threshold_pct == 15.0

    def test_full(self):
        d = DealInputs(
            property_id="PROP001",
            total_units=100,
            unit_mix=self._make_mix(),
            year_built=1985,
            property_class="C",
            market="dallas",
            building_type="garden",
            start_month="2026-06",
            monthly_pace=10,
            downtime_days=14,
            roi_threshold_pct=20.0,
        )
        assert d.year_built == 1985
        assert d.property_class == "C"
        assert d.roi_threshold_pct == 20.0

    def test_missing_property_id_rejected(self):
        with pytest.raises(ValidationError):
            DealInputs(total_units=10, unit_mix=self._make_mix(),
                       start_month="2026-06", monthly_pace=5)

    def test_zero_monthly_pace_rejected(self):
        with pytest.raises(ValidationError):
            DealInputs(property_id="P1", total_units=10, unit_mix=self._make_mix(),
                       start_month="2026-06", monthly_pace=0)


class TestDealAnalysis:
    def test_construction(self):
        analysis = DealAnalysis(
            property_id="PROP001",
            total_units=10,
            ready_to_underwrite=True,
            unit_type_results=[
                UnitTypeResult(
                    sqft=850, bedrooms=2, bathrooms=1, count=10,
                    cost_estimate_low=12000, cost_estimate_high=15000,
                    roi_pct=18.5, roi_passes=True,
                    renovation_program={"renovation_cost_per_unit": 15000},
                    roi_result={"roi_pct": 18.5},
                )
            ],
            total_renovation_cost_low=120000,
            total_renovation_cost_high=150000,
            exterior_capex_low=50000,
            exterior_capex_high=80000,
            risk_flags=[],
            renovation_programs=[{"renovation_cost_per_unit": 15000}],
            summary="Deal: PROP001...",
        )
        assert analysis.ready_to_underwrite is True
        assert len(analysis.unit_type_results) == 1
        assert len(analysis.renovation_programs) == 1
