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
        def lookup(s):
            return (["2026-01-07"], [106.0])
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
        def lookup(s):
            return (["2026-01-07", "2026-01-08"], [103.0, 102.0])
        calib = tracking.compute_calibration(str(tmp_path), highs_lookup=lookup)
        # Partial high coverage decides NOTHING (audit 2026-08-19): the miss
        # resolves through the snapshot closes, so it is close-graded.
        assert calib["n_resolved"] == 1
        assert calib["n_graded_on_highs"] == 0
        bucket = next(b for b in calib["buckets"] if b["n"])
        assert bucket["realized_rate"] == 0.0

    def test_no_highs_falls_back_to_close_grading(self, tmp_path):
        _snap(tmp_path, "2026-01-05",
              [{"symbol": "NOHI", "price": 100.0, "predictions": dict(self.PRED)}])
        _snap(tmp_path, "2026-01-09", [{"symbol": "NOHI", "price": 106.0}])
        def lookup(s):
            return None
        calib = tracking.compute_calibration(str(tmp_path), highs_lookup=lookup)
        assert calib["n_resolved"] == 1
        assert calib["n_graded_on_highs"] == 0

    def test_highs_outside_window_do_not_resolve(self, tmp_path):
        _snap(tmp_path, "2026-01-05",
              [{"symbol": "LATE", "price": 100.0, "predictions": dict(self.PRED)}])
        # No later snapshot prices at all; the only high is past the window.
        _snap(tmp_path, "2026-01-20", [{"symbol": "OTHER", "price": 1.0}])
        def lookup(s):
            return (["2026-01-19"], [110.0])
        calib = tracking.compute_calibration(str(tmp_path), highs_lookup=lookup)
        assert calib["n_resolved"] == 0 and calib["n_unresolved"] == 1

    def test_default_lookup_reads_ohlcv_cache(self):
        market_data._cache.set("ohlcv:ZZTT1:1y", {
            "ok": True, "index": ["2026-01-05T00:00:00"], "high": [42.0]}, 60)
        dates, highs, closes = tracking._default_highs_lookup("ZZTT1")
        assert dates == ["2026-01-05"] and highs == [42.0]
        assert len(closes) == 1  # closes ride along for basis anchoring
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


    def test_close_hit_after_partial_highs_counts_as_close_graded(self, tmp_path):
        """CR finding: the counter must reflect which evidence DECIDED the
        grade. Partial highs that missed, then a snapshot close that hit ->
        resolved as a hit, graded on closes."""
        _snap(tmp_path, "2026-01-05",
              [{"symbol": "MIX", "price": 100.0, "predictions": {
                  "short_term:t1": {"probability": 0.5, "target_pct": 5.0,
                                    "horizon_days": 7}}}])
        _snap(tmp_path, "2026-01-09", [{"symbol": "MIX", "price": 106.0}])

        def lookup(s):
            return (["2026-01-06"], [101.0])  # partial: series stops early
        calib = tracking.compute_calibration(str(tmp_path), highs_lookup=lookup)
        assert calib["n_resolved"] == 1
        assert calib["n_graded_on_highs"] == 0

    def test_full_window_highs_miss_is_final_despite_no_snapshots(self, tmp_path):
        """A complete high series that never touched resolves the miss even
        when no later snapshot priced the symbol at all."""
        _snap(tmp_path, "2026-01-05",
              [{"symbol": "DONE", "price": 100.0, "predictions": {
                  "short_term:t1": {"probability": 0.5, "target_pct": 5.0,
                                    "horizon_days": 7}}}])
        _snap(tmp_path, "2026-01-20", [{"symbol": "OTHER", "price": 1.0}])

        def lookup(s):
            dates = [f"2026-01-{d:02d}" for d in range(6, 15)]
            return (dates, [102.0] * len(dates))
        calib = tracking.compute_calibration(str(tmp_path), highs_lookup=lookup)
        assert calib["n_resolved"] == 1
        assert calib["n_graded_on_highs"] == 1
        bucket = next(b for b in calib["buckets"] if b["n"])
        assert bucket["realized_rate"] == 0.0


class TestAuditRegressions:
    """Regression tests for the CodeRabbit findings recovered by the audit
    of review bodies (outside-diff and nitpick sections, PRs 43-50)."""

    def test_info_success_halves_interval(self, monkeypatch):
        """PR 43 nitpick: one success halves the adaptive interval exactly."""
        monkeypatch.setattr(market_data, "_info_interval",
                            [market_data.INFO_CALL_INTERVAL_SECONDS * 8])
        market_data._info_lane_succeeded()
        assert market_data._info_interval[0] == market_data.INFO_CALL_INTERVAL_SECONDS * 4

    def test_info_refusals_cap_at_max(self, monkeypatch):
        monkeypatch.setattr(market_data, "_info_interval",
                            [market_data.INFO_CALL_INTERVAL_SECONDS])
        monkeypatch.setattr(market_data, "_info_cooldown_until", [0.0])
        for _ in range(20):
            market_data._info_lane_refused()
        assert market_data._info_interval[0] == market_data.INFO_INTERVAL_MAX_SECONDS

    def test_walkforward_excludes_unresolved_train_rows(self, tmp_path):
        """PR 45 outside-diff Major: a training row is usable only once the
        snapshot that RESOLVED its forward return predates the test day --
        otherwise future prices leak into the fit."""
        import walkforward
        from datetime import date, timedelta
        start = date(2026, 1, 5)
        for i in range(30):
            day = (start + timedelta(days=i)).isoformat()
            _snap(tmp_path, day, [{"symbol": "AAA", "price": 100.0 + i,
                                   "factors": {"a": {"score": 50 + i}}}])
        rows = walkforward._training_rows(str(tmp_path))
        assert rows and all("resolved_on" in r for r in rows)
        for r in rows:
            assert r["resolved_on"] > r["day"]

    def test_walkforward_trains_on_full_minimum(self, tmp_path):
        """PR 45 outside-diff Major: the first fitted test day must have
        MIN_FIT_DAYS distinct training days, not half of them."""
        import walkforward
        from datetime import date, timedelta
        start = date(2026, 1, 5)
        # Enough days that at least one test day qualifies under the strict rule.
        for i in range(walkforward.MIN_FIT_DAYS + 18):
            day = (start + timedelta(days=i)).isoformat()
            rows = []
            for j, sym in enumerate(("AAA", "BBB", "CCC", "DDD")):
                score = (j * 25 + i * 3) % 100
                rows.append({"symbol": sym, "price": 100.0 * (1 + score / 1000.0) ** i,
                             "factors": {"a": {"score": score}}})
            _snap(tmp_path, day, rows)
        wf = walkforward.walk_forward(str(tmp_path))
        assert wf["ready"]
        assert "imputed_factor_share" in wf

    def test_ci_moves_with_shrinkage(self):
        """PR 50 outside-diff Major: the displayed interval must belong to the
        displayed (shrunk) probability, not the raw one."""
        import numpy as np
        import timeframes
        closes = np.full(80, 100.0)
        highs = closes.copy()
        highs[5] = 106.0  # thin evidence: raw rate near zero
        targets = {"t1": {"upside_percent": 5.0, "target_price": 105.0}}
        bases = [{"closes": closes, "highs": highs, "label": "all windows"}]
        raw = timeframes.annotate_targets(closes, dict(targets), "short", bases=bases)["t1"]
        shrunk = timeframes.annotate_targets(closes, dict(targets), "short", bases=bases,
                                             cohort_prior=0.50)["t1"]
        assert shrunk["probability"] > raw["probability"]
        assert shrunk["ci_low"] > raw["ci_low"]   # interval followed the estimate

    def test_going_concern_sends_date_range(self, monkeypatch):
        """PR 49 outside-diff Major: EFTS caps at 100 relevance-ranked hits, so
        the date range must ride in the request."""
        monkeypatch.setattr(market_data, "_edgar_cik_table",
                            lambda: {"ok": True, "table": {"ZZDR1": "123456"}})
        captured = {}

        class FakeResponse:
            def raise_for_status(self):
                pass
            def json(self):
                return {"hits": {"hits": []}}

        def fake_get(url, params=None, **kw):
            captured.update(params or {})
            return FakeResponse()

        import requests
        monkeypatch.setattr(requests, "get", fake_get)
        monkeypatch.setattr(market_data, "_throttle", lambda: None)
        market_data._cache._local.pop("gc:ZZDR1", None)
        market_data.going_concern("ZZDR1")
        assert captured.get("dateRange") == "custom"
        assert captured.get("startdt") and captured.get("enddt")

    def test_stable_universe_missing_at_field(self):
        """PR 44 nitpick: a cached universe written before the 'at' field
        existed must serve as cached with a sane reuse message, not raise."""
        import app
        market_data._cache.set("universe:v1", {
            "losers": [{"Symbol": "ZZUA1"}],
            "status": {"success": True, "message": "scraped"}}, 60)
        losers, status = app.stable_universe()
        assert losers == [{"Symbol": "ZZUA1"}]
        assert status["data_source"] == "cached"
        assert "reused" in status["message"]
