"""Tests for the market-context batch: options-implied move, going-concern
full-text flag, drop-magnitude matching, and sector-conditioned windows."""

import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import market_data
import timeframes


@pytest.fixture(autouse=True)
def _isolated_cache(monkeypatch):
    """Force the in-memory cache: with REDIS_URL set in the environment,
    popping _local would not remove entries and these tests would both flake
    and write keys into a shared Redis (CR, PR 49)."""
    monkeypatch.setattr(market_data._cache, "_redis", None)
    market_data._cache._local.clear()
    yield
    market_data._cache._local.clear()


class TestDropBandMask:
    def test_two_sided_band(self):
        # -5%, -12%, -25% days: a 4-15% band keeps the middle two only.
        closes = np.array([100.0, 95.0, 83.6, 62.7])
        mask = timeframes.day_drop_mask(closes, min_drop_pct=4.0, max_drop_pct=15.0)
        assert mask.tolist() == [False, True, True, False]

    def test_unbounded_keeps_previous_behaviour(self):
        closes = np.array([100.0, 95.0, 83.6, 62.7])
        mask = timeframes.day_drop_mask(closes, min_drop_pct=4.0)
        assert mask.tolist() == [False, True, True, True]


class TestSameDayReturnMask:
    def test_aligns_by_date_and_band(self):
        dates = ["2026-01-02", "2026-01-03", "2026-01-04", "2026-01-05"]
        ref_dates = ["2026-01-01", "2026-01-02", "2026-01-03", "2026-01-05"]
        ref_closes = [100.0, 98.0, 99.5, 97.0]  # -2%, +1.53%, then a gap day
        down = timeframes.same_day_return_mask(dates, ref_dates, ref_closes,
                                               max_ret_pct=-1.0)
        # 01-02 fell 2% (True); 01-03 rose (False); 01-04 unknown (False);
        # 01-05 return is measured from the prior known close.
        assert down[0] == True    # noqa: E712
        assert down[1] == False   # noqa: E712
        assert down[2] == False   # noqa: E712
        flat = timeframes.same_day_return_mask(dates, ref_dates, ref_closes,
                                               min_ret_pct=-0.3)
        assert flat[1] == True    # noqa: E712

    def test_unknown_dates_never_satisfy(self):
        mask = timeframes.same_day_return_mask(["2026-02-01"], [], [], max_ret_pct=0)
        assert mask.tolist() == [False]

    def test_bounded_flat_band_excludes_rallies(self):
        """CR finding: a +2% sector rally day is not 'sector flat'."""
        dates = ["2026-01-02", "2026-01-03"]
        ref_dates = ["2026-01-01", "2026-01-02", "2026-01-03"]
        ref_closes = [100.0, 100.1, 102.1]  # +0.1% then +2.0%
        mask = timeframes.same_day_return_mask(
            dates, ref_dates, ref_closes, min_ret_pct=-0.3, max_ret_pct=1.5)
        assert mask.tolist() == [True, False]


class TestImpliedMove:
    def _fake_ticker(self, monkeypatch, bid, ask, last=0.0, expiry_days=10):
        import pandas as pd
        from datetime import date, timedelta
        expiry = (date.today() + timedelta(days=expiry_days)).isoformat()

        frame = pd.DataFrame({"strike": [90.0, 100.0, 110.0],
                              "bid": [bid] * 3, "ask": [ask] * 3,
                              "lastPrice": [last] * 3})

        class Chain:
            calls = frame.copy()
            puts = frame.copy()

        class FakeTicker:
            options = (expiry,)
            def option_chain(self, e):
                return Chain()

        monkeypatch.setattr(market_data, "_ticker", lambda s: FakeTicker())
        market_data._cache.set("tech:ZZIM1", {"ok": True, "close": 100.0}, 60)
        market_data._cache._local.pop("implied:ZZIM1", None)
        return expiry

    def test_straddle_math_and_quality_ok(self, monkeypatch):
        expiry = self._fake_ticker(monkeypatch, bid=2.9, ask=3.1)
        result = market_data.implied_move("ZZIM1")
        assert result.ok
        # call mid 3.0 + put mid 3.0 over spot 100 = 6.0%
        assert result.value["implied_move_pct"] == 6.0
        assert result.value["expiry"] == expiry
        assert result.value["quality"] == "ok"
        assert result.is_derived

    def test_wide_spread_flagged(self, monkeypatch):
        self._fake_ticker(monkeypatch, bid=1.0, ask=2.5)
        result = market_data.implied_move("ZZIM1")
        assert result.ok
        assert "wide-spread" in result.value["quality"]

    def test_no_quotes_falls_to_last_with_distinct_quality(self, monkeypatch):
        self._fake_ticker(monkeypatch, bid=0.0, ask=0.0, last=2.0)
        result = market_data.implied_move("ZZIM1")
        assert result.ok
        assert result.value["implied_move_pct"] == 4.0
        assert result.value["quality"] == "last-trade fallback (no live quotes)"

    def test_no_cached_spot_is_unavailable(self, monkeypatch):
        """A strike is not an underlying price; without a real spot, refuse."""
        self._fake_ticker(monkeypatch, bid=2.9, ask=3.1)
        market_data._cache._local.pop("tech:ZZIM1", None)
        result = market_data.implied_move("ZZIM1")
        assert not result.ok
        assert "underlying price" in result.reason

    def test_shared_strike_required(self, monkeypatch):
        """Different call and put strikes are not a straddle."""
        import pandas as pd
        from datetime import date, timedelta
        expiry = (date.today() + timedelta(days=10)).isoformat()

        class Chain:
            calls = pd.DataFrame({"strike": [95.0], "bid": [2.9], "ask": [3.1],
                                  "lastPrice": [3.0]})
            puts = pd.DataFrame({"strike": [105.0], "bid": [2.9], "ask": [3.1],
                                 "lastPrice": [3.0]})

        class FakeTicker:
            options = (expiry,)
            def option_chain(self, e):
                return Chain()

        monkeypatch.setattr(market_data, "_ticker", lambda s: FakeTicker())
        market_data._cache.set("tech:ZZIM3", {"ok": True, "close": 100.0}, 60)
        market_data._cache._local.pop("implied:ZZIM3", None)
        result = market_data.implied_move("ZZIM3")
        assert not result.ok and "both sides" in result.reason

    def test_dead_chain_is_unavailable(self, monkeypatch):
        self._fake_ticker(monkeypatch, bid=0.0, ask=0.0, last=0.0)
        assert not market_data.implied_move("ZZIM1").ok

    def test_no_options_is_unavailable(self, monkeypatch):
        class Bare:
            options = ()
        monkeypatch.setattr(market_data, "_ticker", lambda s: Bare())
        market_data._cache._local.pop("implied:ZZIM2", None)
        result = market_data.implied_move("ZZIM2")
        assert not result.ok and "no listed options" in result.reason


class TestGoingConcern:
    def _mock_fts(self, monkeypatch, hits=None, error=False):
        monkeypatch.setattr(market_data, "_edgar_cik_table",
                            lambda: {"ok": True, "table": {"ZZGC1": "123456"}})

        class FakeResponse:
            def raise_for_status(self):
                if error:
                    raise RuntimeError("boom")
            def json(self):
                return {"hits": {"hits": hits or []}}

        import requests
        monkeypatch.setattr(requests, "get", lambda url, **kw: FakeResponse())
        monkeypatch.setattr(market_data, "_throttle", lambda: None)
        market_data._cache._local.pop("gc:ZZGC1", None)

    def test_recent_filing_flags(self, monkeypatch):
        from datetime import date, timedelta
        recent = (date.today() - timedelta(days=30)).isoformat()
        self._mock_fts(monkeypatch, hits=[
            {"_source": {"file_date": recent, "file_type": "10-Q", "adsh": "0001-26-1"}}])
        result = market_data.going_concern("ZZGC1")
        assert result.ok and result.value["flagged"] is True
        assert result.value["latest"] == recent
        assert "substantial doubt" in result.value["note"]

    def test_only_old_filings_do_not_flag(self, monkeypatch):
        self._mock_fts(monkeypatch, hits=[
            {"_source": {"file_date": "2019-05-01", "file_type": "10-K", "adsh": "x"}}])
        result = market_data.going_concern("ZZGC1")
        assert result.ok and result.value["flagged"] is False

    def test_zero_hits_is_a_real_clear(self, monkeypatch):
        self._mock_fts(monkeypatch, hits=[])
        result = market_data.going_concern("ZZGC1")
        assert result.ok and result.value["flagged"] is False

    def test_lookup_failure_is_unavailable_not_clear(self, monkeypatch):
        self._mock_fts(monkeypatch, error=True)
        result = market_data.going_concern("ZZGC1")
        assert not result.ok  # never render a failed check as a clean bill

    def test_render_path_never_fetches(self, monkeypatch):
        monkeypatch.setattr(market_data, "_edgar_cik_table",
                            lambda: {"ok": True, "table": {"ZZGC2": "123456"}})
        def explode(*a, **k):
            raise AssertionError("render path fetched EDGAR")
        import requests
        monkeypatch.setattr(requests, "get", explode)
        market_data._cache._local.pop("gc:ZZGC2", None)
        result = market_data.going_concern("ZZGC2", allow_fetch=False)
        assert not result.ok

    def test_render_path_never_fetches_even_with_cold_cik_map(self, monkeypatch):
        """CR finding: CIK resolution must live inside produce, or a cache-only
        render with a cold ticker map still hits the SEC."""
        def explode(*a, **k):
            raise AssertionError("render path fetched with cold CIK map")
        import requests
        monkeypatch.setattr(requests, "get", explode)
        market_data._cache._local.pop("gc:ZZGC3", None)
        market_data._cache._local.pop("edgar:ciks", None)
        result = market_data.going_concern("ZZGC3", allow_fetch=False)
        assert not result.ok

    def test_unregistered_ticker_negative_caches(self, monkeypatch):
        """Preflight failures must land in the cache so the info lane cannot
        spin on symbols that will never resolve."""
        monkeypatch.setattr(market_data, "_edgar_cik_table",
                            lambda: {"ok": True, "table": {}})
        monkeypatch.setattr(market_data, "_throttle", lambda: None)
        market_data._cache._local.pop("gc:ZZGC4", None)
        result = market_data.going_concern("ZZGC4")
        assert not result.ok and "not in SEC registry" in result.reason
        assert market_data._cache.get("gc:ZZGC4") is not None  # cached negative


class TestEvidenceBasesLadder:
    def _seed_5y(self, symbol, closes):
        market_data._cache.set(f"hist:{symbol}:5y", {"ok": True, "closes": closes}, 60)
        market_data._cache.set(f"tech:{symbol}", {"ok": True, "ma20": None}, 60)

    def test_magnitude_rung_appears_for_big_drops(self):
        import app
        # History rich in ~10% drops that bounce; today's close is a -10% day.
        pattern = [100.0]
        for _ in range(80):
            pattern.append(pattern[-1] * 0.90)
            pattern.append(pattern[-1] * 1.12)
        pattern.append(pattern[-1] * 0.90)   # today: -10%
        closes = np.array(pattern)
        bases = app._evidence_bases("ZZEB1", closes)
        labels = [b["label"] for b in bases]
        assert any("like today's -10" in lab for lab in labels)
        assert labels[-1] == "all windows"   # the floor survives

    def test_no_magnitude_rung_for_small_drops(self):
        import app
        closes = np.array([100.0, 99.0, 98.0, 97.5, 96.0] * 30)
        bases = app._evidence_bases("ZZEB2", closes)
        assert not any("like today" in b["label"] for b in bases)

    def test_sector_rung_when_context_cached(self):
        import app
        closes = np.linspace(120, 80, 300)
        # Seed sector context inputs: profile sector + ETF histories.
        market_data._cache.set("info:ZZEB3", {"ok": True, "sector": "Energy"}, 60)
        market_data._cache.set("hist:XLE:5y",
                               {"ok": True, "closes": [80.0, 78.0]}, 60)  # -2.5% day
        dates = [f"2026-0{1 + i // 28}-{(i % 28) + 1:02d}" for i in range(90)]
        market_data._cache.set("ohlcv:ZZEB3:1y", {
            "ok": True, "index": dates,
            "close": list(np.linspace(100, 70, 90)),
            "high": list(np.linspace(101, 71, 90))}, 60)
        market_data._cache.set("ohlcv:XLE:1y", {
            "ok": True, "index": dates,
            "close": list(np.linspace(80, 60, 90))}, 60)
        bases = app._evidence_bases("ZZEB3", closes)
        assert any("sector also down" in b["label"] for b in bases)


class TestSnapshotContextFields:
    def test_snapshot_route_rows_carry_new_context(self, monkeypatch):
        """Exercise the real /api/snapshot assembly (CR, PR 49): a field
        removed from the row would previously still pass the accessor-only
        version of this test."""
        import app
        import tracking
        monkeypatch.setattr(app, "scrape_yahoo_losers", lambda: (
            [{"Symbol": "ZZSN1", "Name": "Test Co", "Change": "-1",
              "Percent Change": "-10%", "Volume": "1M"}],
            {"success": True, "message": "mocked"}))
        monkeypatch.setattr(market_data, "batch_history", lambda *a, **k: 0)
        monkeypatch.setattr(app, "score_stock", lambda *a, **k: {
            "scored": True, "score": 75.0, "confidence": "High", "coverage": 1.0,
            "factors": [], "recommendation": "x", "recommendation_color": "g",
            "factors_used": 6, "factors_total": 6, "missing": []})
        monkeypatch.setattr(market_data, "technicals",
                            lambda s, **kw: market_data.Sourced.live({"close": 10.0}, "t"))
        monkeypatch.setattr(market_data, "analyst_target", lambda s, **kw: {
            "mean": market_data.Sourced.unavailable("t", "none"),
            "analysts": market_data.Sourced.unavailable("t", "none")})
        monkeypatch.setattr(market_data, "sector_context",
                            lambda s: market_data.Sourced.derived({"sector": "Energy"}, "t"))
        monkeypatch.setattr(market_data, "implied_move",
                            lambda s, **kw: market_data.Sourced.derived(
                                {"implied_move_pct": 7.5}, "t"))
        monkeypatch.setattr(market_data, "going_concern",
                            lambda s, **kw: market_data.Sourced.live({"flagged": True}, "t"))
        monkeypatch.setattr(app, "_sophisticated_cached", lambda s: {})
        monkeypatch.setattr(tracking, "tracked_symbols", lambda **kw: [])
        monkeypatch.setattr(market_data, "price_history",
                            lambda s, **kw: market_data.Sourced.unavailable("t", "none"))
        client = app.app.test_client()
        response = client.get("/api/snapshot")
        assert response.status_code == 200
        row = response.get_json()["universe"][0]
        assert row["symbol"] == "ZZSN1"
        assert row["implied_move_pct"] == 7.5
        assert row["going_concern"] is True
        assert row["sector"] == "Energy"
