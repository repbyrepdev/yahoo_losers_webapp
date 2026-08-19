"""Tests for the polish batch: recency weighting, true-touch calibration
grading, the liquidity chip, and the methodology disclosures."""

import json
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import market_data
import timeframes
import tracking


class TestRecencyWeighting:
    def _old_hits_recent_misses(self):
        """First 60 days: every window touches +5%. Last 200 days: flat, none do."""
        early = []
        level = 100.0
        for _ in range(30):
            early += [level, level * 1.06]
        flat = [100.0] * 200
        return np.array(early + flat)

    def test_weighted_probability_tracks_recent_behaviour(self):
        closes = self._old_hits_recent_misses()
        weighted = timeframes.hit_rate(closes, 5.0, 7)
        unweighted = timeframes.hit_rate(closes, 5.0, 7, half_life_bars=None)
        assert weighted["probability"] < unweighted["probability"]
        assert weighted["hits"] == unweighted["hits"]          # raw counts identical
        assert weighted["windows"] == unweighted["windows"]

    def test_effective_sample_below_raw_count(self):
        closes = self._old_hits_recent_misses()
        measured = timeframes.hit_rate(closes, 5.0, 7)
        assert measured["n_eff"] < measured["windows"]
        assert measured["recency_half_life_bars"] == timeframes.RECENCY_HALF_LIFE_BARS

    def test_half_life_none_reproduces_unweighted(self):
        closes = np.array([100.0, 95.0] * 60)
        measured = timeframes.hit_rate(closes, 4.0, 7, half_life_bars=None)
        # probability is rounded to 4 decimals in the payload
        assert measured["probability"] == pytest.approx(
            measured["hits"] / measured["windows"], abs=5e-5)
        assert measured["n_eff"] == measured["windows"]

    def test_describe_mentions_recency_when_it_bites(self):
        # A five-year-scale history: old evidence far past the half-life, so
        # the effective sample visibly shrinks and the label must say so.
        early = []
        level = 100.0
        for _ in range(30):
            early += [level, level * 1.06]
        closes = np.array(early + [100.0] * 1200)
        measured = timeframes.hit_rate(closes, 5.0, 7)
        assert measured["n_eff"] < measured["windows"] * 0.95
        assert "recency-weighted" in timeframes.describe(measured)
        assert "n_eff" in timeframes.describe(measured)

    def test_shrink_toward_rate_math(self):
        # weighted p 0.10 over n_eff 30, prior 0.30 with m=20: (3+6)/50 = 0.18
        assert timeframes.shrink_toward_rate(0.10, 30, 0.30) == pytest.approx(0.18)


def _snap(directory, day, rows):
    with open(os.path.join(directory, f"{day}.json"), "w") as handle:
        json.dump({"date": day, "universe": rows, "tracked_prices": {}}, handle)


class TestTrueTouchGrading:
    PRED = {"short_term:t1": {"probability": 0.5, "target_pct": 5.0, "horizon_days": 7}}

    def test_intraday_high_between_snapshots_counts_as_hit(self, tmp_path):
        _snap(tmp_path, "2026-01-05",
              [{"symbol": "SPIKE", "price": 100.0, "predictions": dict(self.PRED)}])
        # Snapshot closes never reach 105 -- but the intraday high did.
        _snap(tmp_path, "2026-01-09", [{"symbol": "SPIKE", "price": 101.0}])
        _snap(tmp_path, "2026-01-20", [{"symbol": "SPIKE", "price": 99.0}])
        lookup = lambda s: (["2026-01-07"], [106.0])
        calib = tracking.compute_calibration(str(tmp_path), highs_lookup=lookup)
        assert calib["n_resolved"] == 1
        assert calib["n_graded_on_highs"] == 1
        bucket = next(b for b in calib["buckets"] if b["n"])
        assert bucket["realized_rate"] == 100.0

    def test_highs_confirm_a_miss_without_inflating(self, tmp_path):
        _snap(tmp_path, "2026-01-05",
              [{"symbol": "FLAT", "price": 100.0, "predictions": dict(self.PRED)}])
        _snap(tmp_path, "2026-01-09", [{"symbol": "FLAT", "price": 101.0}])
        _snap(tmp_path, "2026-01-20", [{"symbol": "FLAT", "price": 99.0}])
        lookup = lambda s: (["2026-01-07", "2026-01-08"], [103.0, 102.0])
        calib = tracking.compute_calibration(str(tmp_path), highs_lookup=lookup)
        assert calib["n_resolved"] == 1
        assert calib["n_graded_on_highs"] == 1
        bucket = next(b for b in calib["buckets"] if b["n"])
        assert bucket["realized_rate"] == 0.0

    def test_no_highs_falls_back_to_close_grading(self, tmp_path):
        _snap(tmp_path, "2026-01-05",
              [{"symbol": "NOHI", "price": 100.0, "predictions": dict(self.PRED)}])
        _snap(tmp_path, "2026-01-09", [{"symbol": "NOHI", "price": 106.0}])
        calib = tracking.compute_calibration(str(tmp_path), highs_lookup=lambda s: None)
        assert calib["n_resolved"] == 1
        assert calib["n_graded_on_highs"] == 0

    def test_highs_outside_window_do_not_resolve(self, tmp_path):
        _snap(tmp_path, "2026-01-05",
              [{"symbol": "LATE", "price": 100.0, "predictions": dict(self.PRED)}])
        # No later snapshot prices at all; the only high is past the window.
        _snap(tmp_path, "2026-01-20", [{"symbol": "OTHER", "price": 1.0}])
        lookup = lambda s: (["2026-01-19"], [110.0])
        calib = tracking.compute_calibration(str(tmp_path), highs_lookup=lookup)
        assert calib["n_resolved"] == 0 and calib["n_unresolved"] == 1

    def test_default_lookup_reads_ohlcv_cache(self):
        market_data._cache.set("ohlcv:ZZTT1:1y", {
            "ok": True, "index": ["2026-01-05T00:00:00"], "high": [42.0]}, 60)
        dates, highs = tracking._default_highs_lookup("ZZTT1")
        assert dates == ["2026-01-05"] and highs == [42.0]
        assert tracking._default_highs_lookup("ZZNONE") is None


class TestLiquidity:
    def _seed(self, symbol, price, volume, days=30):
        market_data._cache.set(f"ohlcv:{symbol}:1y", {
            "ok": True, "close": [price] * days, "volume": [volume] * days}, 60)

    def test_thin_tape_flagged_with_k_display(self):
        import app
        self._seed("ZZLQ1", price=4.0, volume=100_000)   # $400K/day
        liquidity = app._liquidity("ZZLQ1")
        assert liquidity["thin"] is True
        assert liquidity["display"] == "$400K/day"
        assert liquidity["dollar_volume_20d"] == 400_000

    def test_deep_tape_not_flagged_with_m_display(self):
        import app
        self._seed("ZZLQ2", price=25.0, volume=1_000_000)  # $25M/day
        liquidity = app._liquidity("ZZLQ2")
        assert liquidity["thin"] is False
        assert liquidity["display"] == "$25.0M/day"

    def test_cold_cache_is_none(self):
        import app
        assert app._liquidity("ZZLQ3") is None

    def test_too_few_days_is_none(self):
        import app
        self._seed("ZZLQ4", price=10.0, volume=1000, days=5)
        assert app._liquidity("ZZLQ4") is None

    def test_chip_templates_present(self):
        source = open(os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "app.py"), encoding="utf-8").read()
        assert source.count("thin: {{ stock['Liquidity'].display }}") == 2  # both tables
        assert "🫙 {{ stock['Liquidity'].display }}" in source              # card


class TestMethodologyDisclosures:
    def test_readme_states_the_limits(self):
        readme = open(os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "README.md"), encoding="utf-8").read()
        assert "Survivorship" in readme
        assert "n_eff" in readme
        assert "intraday highs" in readme
