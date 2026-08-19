"""Tests for the multi-provider failover layer and its wiring."""

import json
import os
import sys
from datetime import date, timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import market_data
import sources
import tracking


class FakeResponse:
    def __init__(self, payload, status=200):
        self._payload, self.status_code = payload, status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


def _fake_get(router):
    def fake(url, params=None, headers=None, timeout=None, **kw):
        for fragment, payload in router.items():
            if fragment in url:
                return FakeResponse(payload)
        raise AssertionError(f"unrouted url {url}")
    return fake


@pytest.fixture(autouse=True)
def _keys(monkeypatch):
    monkeypatch.setattr(sources, "get_secret",
                        lambda name, **kw: f"test-{name.lower()}")
    monkeypatch.setattr(market_data, "_throttle", lambda: None)


class TestPaperGuard:
    def test_refuses_non_paper_endpoint(self, monkeypatch):
        monkeypatch.setenv("ALPACA_PAPER_BASE", "https://api.alpaca.markets")
        with pytest.raises(RuntimeError, match="refusing non-paper"):
            sources._alpaca_trading_base()

    def test_accepts_paper_endpoint(self, monkeypatch):
        monkeypatch.setenv("ALPACA_PAPER_BASE", "https://paper-api.alpaca.markets")
        assert sources._alpaca_trading_base() == sources.ALPACA_PAPER_BASE

    def test_paper_orders_are_opg_market_notional(self, monkeypatch):
        monkeypatch.setenv("ALPACA_PAPER_BASE", "https://paper-api.alpaca.markets")
        submitted = []

        def fake_post(url, headers=None, json=None, timeout=None, **kw):
            assert url.startswith(sources.ALPACA_PAPER_BASE)
            submitted.append(json)
            return FakeResponse({"id": f"o{len(submitted)}", "status": "accepted"})
        monkeypatch.setattr(sources.requests, "post", fake_post)
        result = sources.paper_execute_picks(["AAA", "BBB", "CCC", "DDD"])
        assert result.ok
        assert len(submitted) == sources.PAPER_MAX_PICKS  # capped
        for order in submitted:
            assert order["time_in_force"] == "opg"
            assert order["type"] == "market"
            assert order["notional"] == sources.PAPER_NOTIONAL_PER_PICK
        assert "simulated money" in result.value["basis"]


class TestFmpBudget:
    def test_hard_stop_at_budget(self, monkeypatch):
        day_key = f"fmpbudget:{date.today().isoformat()}"
        market_data._cache.set(day_key, sources.FMP_DAILY_BUDGET, 3600)
        payload, err = sources._fmp_get("quote", {"symbol": "AAPL"})
        assert payload is None and "budget" in err

    def test_counter_increments(self, monkeypatch):
        monkeypatch.setattr(sources.requests, "get",
                            _fake_get({"/stable/quote": [{"price": 1.0}]}))
        day_key = f"fmpbudget:{date.today().isoformat()}"
        market_data._cache.set(day_key, 0, 3600)
        sources._fmp_get("quote", {"symbol": "AAPL"})
        assert market_data._cache.get(day_key) == 1


class TestLosersFailover:
    def test_fmp_losers_shape(self, monkeypatch):
        monkeypatch.setattr(sources.requests, "get", _fake_get({
            "/stable/biggest-losers": [
                {"symbol": "ZZL1", "name": "Test", "change": -1.2,
                 "changesPercentage": -25.5}]}))
        result = sources.fmp_losers()
        assert result.ok
        row = result.value[0]
        assert row["Symbol"] == "ZZL1" and "%" in row["Percent Change"]

    def test_universe_uses_fmp_when_scrape_fails(self, monkeypatch):
        import app
        monkeypatch.setattr(app, "scrape_yahoo_losers",
                            lambda: ([], {"success": False, "message": "boom"}))
        monkeypatch.setattr(sources, "fmp_losers",
                            lambda: market_data.Sourced.live(
                                [{"Symbol": "ZZF1", "Name": "F", "Change": "-1",
                                  "Percent Change": "-10.00%", "Volume": "n/a",
                                  "Market Cap": "n/a"}], "fmp:biggest-losers"))
        losers, status = app.stable_universe()
        assert status["success"] and status["data_source"] == "fmp-failover"
        assert losers[0]["Symbol"] == "ZZF1"


class TestGrades:
    def test_fmp_events_counted(self, monkeypatch):
        recent = (date.today() - timedelta(days=3)).isoformat()
        monkeypatch.setattr(sources.requests, "get", _fake_get({
            "/stable/grades": [
                {"date": recent, "gradingCompany": "TestCo",
                 "action": "downgrade", "previousGrade": "Buy", "newGrade": "Hold"},
                {"date": "2020-01-01", "gradingCompany": "Old",
                 "action": "upgrade", "previousGrade": "x", "newGrade": "y"}]}))
        result = sources.analyst_grades("ZZG1")
        assert result.ok
        assert result.value["downgrades"] == 1 and result.value["upgrades"] == 0
        assert len(result.value["events"]) == 1  # the 2020 event is outside the window

    def test_finnhub_fallback_trend(self, monkeypatch):
        def router(url, params=None, headers=None, timeout=None, **kw):
            if "/stable/grades" in url:
                return FakeResponse({}, status=403)
            if "stock/recommendation" in url:
                return FakeResponse([
                    {"period": "2026-08-01", "strongBuy": 5, "buy": 10,
                     "hold": 4, "sell": 1, "strongSell": 0},
                    {"period": "2026-07-01", "strongBuy": 8, "buy": 12,
                     "hold": 2, "sell": 0, "strongSell": 0}])
            raise AssertionError(url)
        monkeypatch.setattr(sources.requests, "get", router)
        result = sources.analyst_grades("ZZG2")
        assert result.ok
        assert result.value["monthly_trend"] == (5 + 10) - (8 + 12)
        assert result.source.startswith("finnhub")


class TestEarnings:
    def test_fmp_first(self, monkeypatch):
        soon = (date.today() + timedelta(days=5)).isoformat()
        monkeypatch.setattr(sources.requests, "get", _fake_get({
            "/stable/earnings-calendar": [{"symbol": "ZZE1", "date": soon}]}))
        result = sources.earnings_confirmed("ZZE1")
        assert result.ok and result.value["date"] == soon
        assert result.source.startswith("fmp")


    def test_finnhub_earnings_fallback(self, monkeypatch):
        soon = (date.today() + timedelta(days=8)).isoformat()

        def router(url, params=None, headers=None, timeout=None, **kw):
            if "/stable/earnings-calendar" in url:
                return FakeResponse({}, status=403)
            if "calendar/earnings" in url:
                return FakeResponse({"earningsCalendar": [
                    {"symbol": "ZZE2", "date": soon}]})
            raise AssertionError(url)
        monkeypatch.setattr(sources.requests, "get", router)
        result = sources.earnings_confirmed("ZZE2")
        assert result.ok and result.value["date"] == soon
        assert result.source.startswith("finnhub")


class TestSplits:
    def test_alpaca_ratio(self, monkeypatch):
        monkeypatch.setenv("ALPACA_PAPER_BASE", "https://paper-api.alpaca.markets")
        monkeypatch.setattr(sources.requests, "get", _fake_get({
            "corporate_actions": [
                {"initiating_symbol": "ZZS1", "ex_date": "2026-06-01",
                 "old_rate": 10, "new_rate": 1}]}))
        result = sources.splits_for("ZZS1")
        assert result.ok
        assert result.value[0]["ratio"] == pytest.approx(0.1)  # 1-for-10 reverse

    def test_split_factor_corrects_track_record(self, tmp_path, monkeypatch):
        """The +900% reverse-split phantom becomes a real, corrected return."""
        with open(os.path.join(tmp_path, "2026-01-05.json"), "w") as fh:
            json.dump({"date": "2026-01-05", "universe": [
                {"symbol": "ZZS2", "price": 1.0, "score": 80.0}],
                "tracked_prices": {}}, fh)
        with open(os.path.join(tmp_path, "2026-01-12.json"), "w") as fh:
            json.dump({"date": "2026-01-12", "universe": [
                {"symbol": "ZZS2", "price": 9.0}], "tracked_prices": {}}, fh)
        monkeypatch.setattr(sources, "splits_for",
                            lambda sym, **kw: market_data.Sourced.live(
                                [{"date": "2026-01-08", "ratio": 0.1}], "test"))
        record = tracking.compute_track_record(str(tmp_path))
        assert record.get("split_corrected") == 1
        ret = record["picks"][0]["returns"]["7"]["pct"]
        assert ret == pytest.approx(-10.0, abs=0.1)  # 9.0*0.1 vs 1.0 entry


class TestCalendar:
    def test_add_trading_days_uses_injected_calendar(self, monkeypatch):
        days = {"2026-09-01", "2026-09-02", "2026-09-04"}  # 09-03 holiday
        monkeypatch.setattr(sources, "trading_days_set",
                            lambda **kw: days)
        end = sources.add_trading_days(date(2026, 8, 31), 3)
        assert end == date(2026, 9, 4)  # skipped the holiday

    def test_market_phase_reads_holiday_as_closed(self, monkeypatch):
        recorded = market_data._trading_days_source[0]
        try:
            market_data.set_trading_days_source(lambda: set())  # nothing trades today
            phase = market_data.market_phase()
            assert phase["phase"] == "closed"
        finally:
            market_data._trading_days_source[0] = recorded

    def test_phase_hook_is_cache_only(self, monkeypatch):
        """The hot path must read the warmed cache, never fetch."""
        market_data._cache.set("src:trading-days", {"days": ["2026-01-02"]}, 60)
        result = sources.trading_days_set(cache_only=True)
        assert result == {"2026-01-02"}
        import inspect
        params = inspect.signature(sources.trading_days_set).parameters
        assert params["cache_only"].default is False  # hot path must opt in


class TestPriceFailover:
    def test_patch_path_updates_same_day_close(self, monkeypatch):
        # The last cached bar must be TODAY's: the failover only patches a
        # same-session bar and skips stale ones (it must never overwrite
        # yesterday's close with a live price).
        today = market_data._eastern_now().date().isoformat()
        market_data._cache.set("hist:ZZPF1:5y",
                               {"ok": True, "closes": [10.0, 9.0],
                                "full_fetched_at": 123.0}, 60)
        market_data._cache.set("ohlcv:ZZPF1:1y", {
            "ok": True, "index": ["2026-01-02", today],
            "close": [10.0, 9.0], "high": [10.1, 9.1]}, 60)
        market_data.set_price_failover(lambda symbols: {"ZZPF1": 9.5})
        monkeypatch.setattr(market_data.yf, "download", lambda *a, **k: None)
        try:
            patched = market_data.refresh_last_bar(["ZZPF1"])
        finally:
            market_data._price_failover_source[0] = None
        assert patched == 1
        hist = market_data._cache.get("hist:ZZPF1:5y")
        assert hist["closes"][-1] == 9.5
        assert hist["full_fetched_at"] == 123.0  # stamp preserved
        ohlcv = market_data._cache.get("ohlcv:ZZPF1:1y")
        assert ohlcv["high"][-1] == 9.5 or ohlcv["high"][-1] == 9.1

    def test_stale_last_bar_is_never_overwritten(self, monkeypatch):
        """CR Critical, PR 55: an empty Yahoo frame on a new session must not
        let the failover destroy yesterday's close."""
        market_data._cache.set("hist:ZZPF2:5y",
                               {"ok": True, "closes": [10.0, 9.0]}, 60)
        market_data._cache.set("ohlcv:ZZPF2:1y", {
            "ok": True, "index": ["2026-01-02", "2026-01-03"],
            "close": [10.0, 9.0], "high": [10.1, 9.1]}, 60)
        market_data.set_price_failover(lambda symbols: {"ZZPF2": 5.0})
        monkeypatch.setattr(market_data.yf, "download", lambda *a, **k: None)
        try:
            patched = market_data.refresh_last_bar(["ZZPF2"])
        finally:
            market_data._price_failover_source[0] = None
        assert patched == 0
        assert market_data._cache.get("hist:ZZPF2:5y")["closes"][-1] == 9.0


class TestRevisionChipTemplate:
    def test_chips_present_in_tables_and_card(self):
        source = open(os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "app.py"), encoding="utf-8").read()
        assert source.count("stock['Analyst Revisions'].label") == 3  # 2 tables + card
        assert source.count("Analyst rating changes") == 1  # card tooltip


class TestOptionsCooldown:
    def test_refusal_pauses_and_short_circuits(self, monkeypatch):
        """Live incident 2026-08-19: the prewarmer walked the universe and
        tripped the per-symbol options endpoint. One refusal must pause ALL
        options calls, and paused calls must not touch the provider."""
        import time as _time
        market_data._options_cooldown_until[0] = 0.0

        class Refused:
            @property
            def options(self):
                raise RuntimeError("Too Many Requests. Rate limited.")
        monkeypatch.setattr(market_data, "_ticker", lambda s: Refused())
        result = market_data.implied_move("ZZOC1")
        assert not result.ok
        assert market_data._options_cooldown_until[0] > _time.time()

        calls = []
        monkeypatch.setattr(market_data, "_ticker",
                            lambda s: calls.append(s) or None)
        cooled = market_data.implied_move("ZZOC2")
        assert not cooled.ok and "cooling down" in cooled.reason
        assert calls == []  # provider untouched during the cooldown
        market_data._options_cooldown_until[0] = 0.0
