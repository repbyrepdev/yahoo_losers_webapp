"""Tests for the freshness batch: last-bar refresh, page prebuild policy,
Analysis prewarm inputs, and the live-audit fixes (evidence-gap disclosure,
long-horizon tracking lookback)."""

import os
import sys
import time

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import market_data
import timeframes
import tracking


def _seed_series(symbol, closes, dates):
    market_data._cache.set(f"hist:{symbol}:5y", {"ok": True, "closes": list(closes)}, 300)
    market_data._cache.set(f"ohlcv:{symbol}:1y", {
        "ok": True, "index": list(dates),
        "open": list(closes), "high": [c * 1.01 for c in closes],
        "low": [c * 0.99 for c in closes], "close": list(closes),
        "volume": [1e6] * len(closes)}, 300)


def _frame(symbols, dates, closes):
    import pandas as pd
    idx = pd.to_datetime(dates)
    if len(symbols) == 1:
        return pd.DataFrame({"Open": closes, "High": [c * 1.02 for c in closes],
                             "Low": closes, "Close": closes,
                             "Volume": [2e6] * len(closes)}, index=idx)
    frames = {}
    for s in symbols:
        frames[s] = pd.DataFrame({"Open": closes, "High": [c * 1.02 for c in closes],
                                  "Low": closes, "Close": closes,
                                  "Volume": [2e6] * len(closes)}, index=idx)
    return pd.concat(frames, axis=1)


class TestRefreshLastBar:
    def test_new_day_appends_and_recomputes(self, monkeypatch):
        closes = [100.0 + (i % 7) for i in range(60)]
        dates = [f"2026-{1 + i // 28:02d}-{(i % 28) + 1:02d}" for i in range(60)]
        _seed_series("ZZFB1", closes, dates)
        monkeypatch.setattr(market_data.yf, "download",
                            lambda *a, **k: _frame(["ZZFB1"], ["2026-03-05"], [123.0]))
        monkeypatch.setattr(market_data, "_throttle", lambda: None)
        assert market_data.refresh_last_bar(["ZZFB1"]) == 1
        hist = market_data._cache.get("hist:ZZFB1:5y")
        assert hist["closes"][-1] == 123.0
        assert len(hist["closes"]) == 61  # appended, prior bars intact
        ohlcv = market_data._cache.get("ohlcv:ZZFB1:1y")
        assert ohlcv["index"][-1] == "2026-03-05"
        tech = market_data._cache.get("tech:ZZFB1")
        assert tech["ok"] and tech["close"] == 123.0

    def test_same_day_replaces_never_duplicates(self, monkeypatch):
        closes = [100.0 + (i % 7) for i in range(60)]
        dates = [f"2026-{1 + i // 28:02d}-{(i % 28) + 1:02d}" for i in range(60)]
        _seed_series("ZZFB2", closes, dates)
        last_date = dates[-1]
        monkeypatch.setattr(market_data.yf, "download",
                            lambda *a, **k: _frame(["ZZFB2"], [last_date], [111.0]))
        monkeypatch.setattr(market_data, "_throttle", lambda: None)
        assert market_data.refresh_last_bar(["ZZFB2"]) == 1
        hist = market_data._cache.get("hist:ZZFB2:5y")
        assert len(hist["closes"]) == 60  # replaced in place
        assert hist["closes"][-1] == 111.0

    def test_cold_caches_are_skipped_not_crashed(self, monkeypatch):
        monkeypatch.setattr(market_data.yf, "download",
                            lambda *a, **k: _frame(["ZZFB3"], ["2026-03-05"], [50.0]))
        monkeypatch.setattr(market_data, "_throttle", lambda: None)
        assert market_data.refresh_last_bar(["ZZFB3"]) == 0

    def test_rate_limit_engages_backoff(self, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("429 Too Many Requests")
        monkeypatch.setattr(market_data.yf, "download", boom)
        monkeypatch.setattr(market_data, "_throttle", lambda: None)
        market_data._warm_backoff_until[0] = 0.0
        assert market_data.refresh_last_bar(["ZZFB4"]) == 0
        assert market_data._warm_backoff_until[0] > time.time()


class TestPagePrebuildPolicy:
    def test_absent_cache_needs_build(self):
        import app
        assert app._page_needs_prebuild(None) is True

    def test_near_expiry_needs_build(self):
        import app
        assert app._page_needs_prebuild({"expires_at": time.time() + 30}) is True

    def test_fresh_cache_does_not(self):
        import app
        assert app._page_needs_prebuild({"expires_at": time.time() + 600}) is False

    def test_legacy_entry_without_deadline_defers(self):
        import app
        assert app._page_needs_prebuild({"timestamp": "x"}) is False

    def test_redis_load_carries_deadline(self, monkeypatch):
        """The prebuilder can only act early if load_cache passes the
        deadline through from the Redis payload."""
        import app
        import json as _json
        from datetime import datetime as _dt
        payload = _json.dumps({"timestamp": _dt.now().isoformat(),
                               "data": {"x": 1}, "expires_at": time.time() + 300})

        class FakeRedis:
            def get(self, key):
                return payload
        monkeypatch.setattr(app, "redis_client", FakeRedis())
        loaded = app.load_cache()
        assert loaded and loaded.get("expires_at") is not None


class TestEvidenceGapDisclosure:
    def _early_hits_series(self):
        early = []
        level = 100.0
        for _ in range(30):
            early += [level, level * 1.06]
        return np.array(early + [100.0] * 400)

    def test_describe_names_raw_when_rates_diverge(self):
        measured = timeframes.hit_rate(self._early_hits_series(), 5.0, 7)
        raw = measured["hits"] / measured["windows"] * 100
        assert abs(measured["probability"] * 100 - raw) >= 2  # setup sanity
        text = timeframes.describe(measured)
        assert f"recency-weighted from {raw:.0f}% raw" in text

    def test_describe_quiet_when_rates_agree(self):
        # Uniformly alternating hits: the weighted rate equals the raw rate
        # by construction, so the precondition is asserted, not hoped for.
        closes = np.array([100.0, 95.0] * 60)
        measured = timeframes.hit_rate(closes, 4.0, 7)
        raw = measured["hits"] / measured["windows"] * 100
        assert abs(measured["probability"] * 100 - raw) < 2, "fixture no longer uniform"
        assert "recency-weighted" not in timeframes.describe(measured)

    def test_board_detail_names_raw_when_diverging(self):
        import app
        # Early drop-days that all bounced, then a recent staircase of
        # drop-days that never recover: the post-drop rung's weighted rate
        # must sit far below its raw fraction. Preconditions asserted, so
        # this can never pass vacuously.
        closes = [100.0, 106.0] * 30
        level = 100.0
        for _ in range(60):
            level *= 0.95
            closes.append(round(level, 6))
        market_data._cache.set("hist:ZZEG1:5y", {"ok": True, "closes": closes}, 60)
        market_data._cache.set("tech:ZZEG1", {"ok": True, "ma20": None}, 60)
        out = app._horizon_summaries("ZZEG1")
        assert out["short"]["display"] != "—", "fixture failed to measure"
        detail = out["short"]["detail"]
        shown = float(out["short"]["sort"])
        hits, windows = (int(x) for x in detail.split(" ")[0].split("/"))
        raw = hits / windows * 100
        assert abs(shown - raw) >= 2, "fixture no longer diverges"
        assert "recency-weighted from" in detail


class TestTrackingLookback:
    def test_lookback_covers_longest_horizon(self):
        import inspect
        default = inspect.signature(tracking.tracked_symbols).parameters["lookback_days"].default
        assert default >= 183 + 14  # longest prediction horizon plus slack


class TestSystemicMissingFactorBanner:
    def _row(self, score, missing):
        return {"Rebound Score": score, "Missing Factor Labels": missing}

    def test_board_wide_missing_factor_is_announced(self):
        """Live incident 2026-08-19: the options limiter removed the put/call
        factor from every row, scores dropped below the pick gate, and the
        page gave no explanation for the empty recommendations."""
        import app
        rows = [self._row(60 + i, ["Options positioning"]) for i in range(20)]
        degraded, note = app.degraded_state(rows)
        assert degraded is True
        assert "Options positioning" in note
        assert "unavailable across the board" in note

    def test_scattered_missing_factors_stay_quiet(self):
        import app
        rows = ([self._row(60, ["Options positioning"])] * 3
                + [self._row(60, [])] * 17)
        degraded, note = app.degraded_state(rows)
        assert degraded is False and note is None

    def test_fully_covered_board_stays_quiet(self):
        import app
        rows = [self._row(75, []) for _ in range(20)]
        assert app.degraded_state(rows) == (False, None)

    def test_threshold_is_against_the_full_board(self):
        """CR, PR 57: 100% of a thin scored subset must not read as a
        board-wide event when it is only 60% of the whole board."""
        import app
        rows = ([self._row(60, ["Options positioning"])] * 12
                + [{"Rebound Score": None}] * 8)  # 12/20 = 60% of the board
        degraded, note = app.degraded_state(rows)
        assert note is None or "unavailable across the board" not in (note or "")

    def test_exact_threshold_triggers(self):
        import app
        rows = ([self._row(60, ["Options positioning"])] * 16
                + [self._row(60, [])] * 4)  # exactly 80% of the board
        degraded, note = app.degraded_state(rows)
        assert degraded is True and "Options positioning" in note


class TestEarningsChip:
    def test_confirmed_date_in_window_flags(self, monkeypatch):
        import app as app_mod
        import market_data
        import sources as _src
        market_data._cache.set(_src.earnings_cache_key("ZZEC1"), {"ok": True, "date": "2026-08-25"}, 60)
        import tracking
        from datetime import date as _date
        monkeypatch.setattr(tracking, "trading_date_today", lambda: _date(2026, 8, 20))
        assert app_mod._earnings_in_window("ZZEC1", 7) == "2026-08-25"

    def test_far_date_does_not_flag(self, monkeypatch):
        import app as app_mod
        import market_data
        import sources as _src
        market_data._cache.set(_src.earnings_cache_key("ZZEC2"), {"ok": True, "date": "2026-11-03"}, 60)
        import tracking
        from datetime import date as _date
        monkeypatch.setattr(tracking, "trading_date_today", lambda: _date(2026, 8, 20))
        assert app_mod._earnings_in_window("ZZEC2", 7) is None
