"""Tests for plat_agent.sanity.compute_sanity_flags."""

import pytest

from plat_agent.sanity import compute_sanity_flags


def _ok_metrics(**overrides) -> dict:
    """Baseline metrics that should produce zero flags."""
    base = {
        "status": "success",
        "irr": {"levered_irr": 0.18, "unlevered_irr": 0.10},
        "equity_multiple": {"levered_em": 2.1, "unlevered_em": 1.6},
        "dscr": {"minimum": 1.35, "average": 1.55},
        "yields": {"going_in_cap_rate": 0.055},
    }
    for k, v in overrides.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            base[k] = {**base[k], **v}
        else:
            base[k] = v
    return base


class TestNoFlags:
    def test_clean_metrics_produce_no_flags(self):
        assert compute_sanity_flags(_ok_metrics()) == []

    def test_none_metrics_produce_no_flags(self):
        assert compute_sanity_flags(None) == []

    def test_non_success_status_produces_no_flags(self):
        assert compute_sanity_flags({"status": "error"}) == []

    def test_missing_subkeys_handled_gracefully(self):
        assert compute_sanity_flags({"status": "success"}) == []


class TestCapRate:
    def test_low_cap_flagged_warning(self):
        flags = compute_sanity_flags(_ok_metrics(yields={"going_in_cap_rate": 0.035}))
        cap = [f for f in flags if f["metric"] == "going_in_cap_rate"]
        assert len(cap) == 1
        assert cap[0]["severity"] == "warning"

    def test_very_low_cap_flagged_error(self):
        flags = compute_sanity_flags(_ok_metrics(yields={"going_in_cap_rate": 0.025}))
        cap = [f for f in flags if f["metric"] == "going_in_cap_rate"]
        assert cap[0]["severity"] == "error"

    def test_high_cap_flagged_warning(self):
        flags = compute_sanity_flags(_ok_metrics(yields={"going_in_cap_rate": 0.085}))
        cap = [f for f in flags if f["metric"] == "going_in_cap_rate"]
        assert cap[0]["severity"] == "warning"

    def test_very_high_cap_flagged_error(self):
        flags = compute_sanity_flags(_ok_metrics(yields={"going_in_cap_rate": 0.12}))
        cap = [f for f in flags if f["metric"] == "going_in_cap_rate"]
        assert cap[0]["severity"] == "error"

    @pytest.mark.parametrize("cap", [0.04, 0.055, 0.07])
    def test_in_band_cap_not_flagged(self, cap):
        flags = compute_sanity_flags(_ok_metrics(yields={"going_in_cap_rate": cap}))
        assert not [f for f in flags if f["metric"] == "going_in_cap_rate"]


class TestDscr:
    def test_low_dscr_flagged_warning(self):
        flags = compute_sanity_flags(_ok_metrics(dscr={"minimum": 1.10, "average": 1.30}))
        d = [f for f in flags if f["metric"] == "min_dscr"]
        assert len(d) == 1
        assert d[0]["severity"] == "warning"

    def test_subunit_dscr_flagged_error(self):
        flags = compute_sanity_flags(_ok_metrics(dscr={"minimum": 0.58, "average": 0.95}))
        d = [f for f in flags if f["metric"] == "min_dscr"]
        assert d[0]["severity"] == "error"

    def test_at_threshold_dscr_not_flagged(self):
        flags = compute_sanity_flags(_ok_metrics(dscr={"minimum": 1.20, "average": 1.45}))
        assert not [f for f in flags if f["metric"] == "min_dscr"]


class TestIrrAndEm:
    def test_negative_irr_flagged_error(self):
        flags = compute_sanity_flags(_ok_metrics(irr={"levered_irr": -0.05, "unlevered_irr": -0.02}))
        i = [f for f in flags if f["metric"] == "levered_irr"]
        assert i[0]["severity"] == "error"

    def test_implausibly_high_irr_flagged_warning(self):
        flags = compute_sanity_flags(_ok_metrics(irr={"levered_irr": 0.55, "unlevered_irr": 0.20}))
        i = [f for f in flags if f["metric"] == "levered_irr"]
        assert i[0]["severity"] == "warning"

    def test_em_below_one_flagged_error(self):
        flags = compute_sanity_flags(_ok_metrics(equity_multiple={"levered_em": 0.85, "unlevered_em": 1.10}))
        e = [f for f in flags if f["metric"] == "levered_em"]
        assert e[0]["severity"] == "error"

    def test_em_outlier_high_flagged_warning(self):
        flags = compute_sanity_flags(_ok_metrics(equity_multiple={"levered_em": 6.5, "unlevered_em": 2.0}))
        e = [f for f in flags if f["metric"] == "levered_em"]
        assert e[0]["severity"] == "warning"


class TestSampleHillsScenario:
    """The motivating case: cap 3.0% + DSCR 0.58x both flagged, both error."""

    def test_sample_hills(self):
        flags = compute_sanity_flags(_ok_metrics(
            yields={"going_in_cap_rate": 0.030},
            dscr={"minimum": 0.58, "average": 0.85},
        ))
        metrics_flagged = {f["metric"] for f in flags}
        assert "going_in_cap_rate" in metrics_flagged
        assert "min_dscr" in metrics_flagged
        # Both should be error severity
        for f in flags:
            assert f["severity"] == "error"


class TestFlagShape:
    def test_flag_has_required_keys(self):
        flags = compute_sanity_flags(_ok_metrics(dscr={"minimum": 0.5, "average": 0.7}))
        f = flags[0]
        assert {"metric", "value", "expected_range", "severity", "message"} <= set(f)
        assert f["severity"] in {"warning", "error"}
