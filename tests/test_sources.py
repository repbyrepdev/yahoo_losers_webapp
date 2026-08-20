"""Tests for the multi-provider failover layer and its wiring."""

import json
import os
import sys
from datetime import date, timedelta

import time

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
    # Paper-entry rules depend on the market phase; the suite must not
    # change behaviour with the wall clock.
    monkeypatch.setattr(market_data, "market_phase", lambda: {"phase": "closed"})


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
        result = sources.paper_execute_picks(
            [{"symbol": sym, "price": 151.30} for sym in ("AAA", "BBB", "CCC", "DDD")])
        assert result.ok
        assert len(submitted) == sources.PAPER_MAX_PICKS  # capped
        for order in submitted:
            assert order["time_in_force"] == "day"
            assert order["type"] == "limit"
            assert order["extended_hours"] is True
            # ref 151.30 +2% band
            assert order["limit_price"] == "154.33"
            assert order["qty"] == "6"
            assert "notional" not in order
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
        market_data._cache._local.pop("universe:v1", None)
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
    @pytest.fixture(autouse=True)
    def _restore_cooldown(self):
        prior = market_data._options_cooldown_until[0]
        market_data._options_cooldown_until[0] = 0.0
        yield
        market_data._options_cooldown_until[0] = prior

    def test_refusal_pauses_and_short_circuits(self, monkeypatch):
        """Live incident 2026-08-19: the prewarmer walked the universe and
        tripped the per-symbol options endpoint. One refusal must pause ALL
        options calls, and paused calls must not touch the provider."""
        import time as _time

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

    def test_chain_refusal_also_engages_cooldown(self, monkeypatch):
        """CR, PR 56: .options can succeed while .option_chain() is the call
        the limiter refuses -- that path must engage the cooldown too."""
        import time as _time
        from datetime import date, timedelta
        expiry = (date.today() + timedelta(days=10)).isoformat()

        class ChainRefused:
            options = (expiry,)
            def option_chain(self, e):
                raise RuntimeError("429 Too Many Requests")
        monkeypatch.setattr(market_data, "_ticker", lambda s: ChainRefused())
        market_data._cache.set("tech:ZZOC3", {"ok": True, "close": 100.0}, 60)
        result = market_data.implied_move("ZZOC3")
        assert not result.ok
        assert market_data._options_cooldown_until[0] > _time.time()

    def test_options_flow_respects_cooldown(self, monkeypatch):
        """The other options producer must short-circuit identically."""
        import time as _time
        market_data._options_cooldown_until[0] = _time.time() + 60
        calls = []
        monkeypatch.setattr(market_data, "_ticker",
                            lambda s: calls.append(s) or None)
        result = market_data.options_flow("ZZOC4")
        assert not result.ok and "cooling down" in result.reason
        assert calls == []


class TestFactorBackups:
    @pytest.fixture(autouse=True)
    def _isolated_options_cooldown(self):
        """_options_refused() inside these tests arms the REAL shared
        cooldown, which would relabel later tests' refusals as cooling-down."""
        prior = market_data._options_cooldown_until[0]
        market_data._options_cooldown_until[0] = 0
        yield
        market_data._options_cooldown_until[0] = prior

    def test_putcall_parses_contract_sides(self, monkeypatch):
        monkeypatch.setattr(sources.requests, "get", _fake_get({
            "/v1beta1/options/snapshots/ZZPC1": {"snapshots": {
                "ZZPC1260918C00010000": {"dailyBar": {"v": 300}},
                "ZZPC1260918C00012000": {"dailyBar": {"v": 100}},
                "ZZPC1260918P00010000": {"dailyBar": {"v": 200}},
            }}}))
        result = sources.options_putcall("ZZPC1")
        assert result.ok
        assert result.value["call_volume"] == 400
        assert result.value["put_volume"] == 200
        assert result.value["put_call_ratio"] == 0.5

    def test_ratings_spread_shape_matches_yahoo(self, monkeypatch):
        monkeypatch.setattr(sources.requests, "get", _fake_get({
            "stock/recommendation": [
                {"period": "2026-08-01", "strongBuy": 3, "buy": 7,
                 "hold": 5, "sell": 1, "strongSell": 0}]}))
        result = sources.ratings_spread("ZZRS1")
        assert result.ok
        assert result.value == {"strongBuy": 3, "buy": 7, "hold": 5,
                                "sell": 1, "strongSell": 0, "total": 16}

    def test_options_flow_falls_back_during_cooldown(self, monkeypatch):
        """The 2026-08-19 incident scenario: Yahoo options in cooldown must
        no longer blank the factor -- the indicative feed answers instead."""
        import time as _time
        prior = market_data._options_cooldown_until[0]
        market_data._options_cooldown_until[0] = _time.time() + 600
        try:
            monkeypatch.setattr(sources, "options_putcall",
                                lambda s: market_data.Sourced.live(
                                    {"call_volume": 400, "put_volume": 200,
                                     "put_call_ratio": 0.5, "contracts": 3,
                                     "window": "expiries to x"}, "alpaca:options-indicative"))
            market_data._cache._local.pop("options:v2:ZZFB9", None)
            result = market_data.options_flow("ZZFB9")
            assert result.ok
            assert result.value["put_call_ratio"] == 0.5
            assert result.source == "alpaca:options-indicative"
        finally:
            market_data._options_cooldown_until[0] = prior

    def test_ratings_falls_back_when_yahoo_empty(self, monkeypatch):
        class NoRecs:
            recommendations = None
        monkeypatch.setattr(market_data, "_ticker", lambda s: NoRecs())
        monkeypatch.setattr(sources, "ratings_spread",
                            lambda s: market_data.Sourced.live(
                                {"strongBuy": 2, "buy": 4, "hold": 3,
                                 "sell": 0, "strongSell": 0, "total": 9},
                                "finnhub:recommendation-trends"))
        market_data._cache._local.pop("recs:v2:ZZFB8", None)
        result = market_data.analyst_recommendations("ZZFB8")
        assert result.ok
        assert result.value["total"] == 9
        assert result.source == "finnhub:recommendation-trends"

    def test_putcall_merges_all_pages(self, monkeypatch):
        """CR PR58: a truncated chain overweights calls (C sorts before P),
        so every page must merge before the ratio computes."""
        pages = [
            {"snapshots": {"ZZPG1260918C00010000": {"dailyBar": {"v": 300}}},
             "next_page_token": "page-2"},
            {"snapshots": {"ZZPG1260918P00010000": {"dailyBar": {"v": 600}}}},
        ]
        seen_tokens = []

        def fake(url, params=None, headers=None, timeout=None, **kw):
            assert "/v1beta1/options/snapshots/ZZPG1" in url
            seen_tokens.append((params or {}).get("page_token"))
            return FakeResponse(pages[len(seen_tokens) - 1])

        monkeypatch.setattr(sources.requests, "get", fake)
        result = sources.options_putcall("ZZPG1")
        assert result.ok
        assert seen_tokens == [None, "page-2"]
        assert result.value["call_volume"] == 300
        assert result.value["put_volume"] == 600
        assert result.value["put_call_ratio"] == 2.0
        assert result.value["contracts"] == 2

    def test_putcall_refuses_past_page_budget(self, monkeypatch):
        monkeypatch.setattr(sources.requests, "get", _fake_get({
            "/v1beta1/options/snapshots/ZZPG2": {
                "snapshots": {"ZZPG2260918C00010000": {"dailyBar": {"v": 1}}},
                "next_page_token": "never-ends"}}))
        result = sources.options_putcall("ZZPG2")
        assert not result.ok
        assert "page budget" in result.reason


    def test_options_falls_back_on_chain_stage_failure(self, monkeypatch):
        """Live 2026-08-19: Yahoo failed at option_chain(), not expiries, and
        the refusal cached with no fallback attempt. Every exit must try."""
        class ChainDies:
            options = ("2026-09-18",)
            def option_chain(self, expiry):
                raise RuntimeError("429 Too Many Requests")
        monkeypatch.setattr(market_data, "_ticker", lambda s: ChainDies())
        monkeypatch.setattr(sources, "options_putcall",
                            lambda s: market_data.Sourced.live(
                                {"call_volume": 100, "put_volume": 150,
                                 "put_call_ratio": 1.5, "contracts": 2,
                                 "window": "expiries to x"}, "alpaca:options-indicative"))
        result = market_data.options_flow("ZZFB7")
        assert result.ok
        assert result.value["put_call_ratio"] == 1.5
        assert result.source == "alpaca:options-indicative"

    def test_options_falls_back_when_yahoo_reports_none_listed(self, monkeypatch):
        """Yahoo degrades into empty 200s wearing structural prose; the
        independent feed gets to disagree before the refusal caches."""
        class NoChain:
            options = ()
        monkeypatch.setattr(market_data, "_ticker", lambda s: NoChain())
        monkeypatch.setattr(sources, "options_putcall",
                            lambda s: market_data.Sourced.live(
                                {"call_volume": 40, "put_volume": 10,
                                 "put_call_ratio": 0.25, "contracts": 1,
                                 "window": "expiries to x"}, "alpaca:options-indicative"))
        result = market_data.options_flow("ZZFB6")
        assert result.ok
        assert result.value["put_call_ratio"] == 0.25

    def test_options_refusal_stands_when_both_providers_empty(self, monkeypatch):
        class NoChain:
            options = ()
        monkeypatch.setattr(market_data, "_ticker", lambda s: NoChain())
        monkeypatch.setattr(sources, "options_putcall",
                            lambda s: market_data.Sourced.unavailable(
                                "alpaca:options-indicative", "no listed options"))
        result = market_data.options_flow("ZZFB5")
        assert not result.ok
        assert result.reason == "no listed options"

    def test_unavailable_fallback_called_once_when_rate_limited(self, monkeypatch):
        """CR PR59: re-raising after a failed fallback let _cached's quick
        retry re-enter under the armed cooldown -- a second Alpaca request
        per symbol per cycle. The refusal must return, classified, instead."""
        calls = []

        class ExpiryDies:
            @property
            def options(self):
                raise RuntimeError("429 Too Many Requests")
        monkeypatch.setattr(market_data, "_ticker", lambda s: ExpiryDies())

        def counting_unavailable(sym):
            calls.append(sym)
            return market_data.Sourced.unavailable(
                "alpaca:options-indicative", "budget exhausted")
        monkeypatch.setattr(sources, "options_putcall", counting_unavailable)
        result = market_data.options_flow("ZZFB4")
        assert not result.ok
        assert len(calls) == 1
        assert "429" in result.reason or "429" in (result.value or {}).get("detail", "") \
            or result.reason == "RuntimeError"


class TestFactorLaneDrain:
    def test_missing_factor_keys_reports_cold_symbols(self):
        market_data._cache.set(market_data._recs_key("ZZLD1"), {"ok": True}, 60)
        market_data._cache.set(market_data._options_key("ZZLD2"), {"ok": True}, 60)
        recs, options = market_data._symbols_missing_factor_keys(
            ["ZZLD1", "ZZLD2", "ZZLD3", "^GSPC"])
        assert recs == ["ZZLD2", "ZZLD3"]
        assert options == ["ZZLD1", "ZZLD3"]

    def test_producers_write_the_keys_the_lane_scans(self, monkeypatch):
        """The lane and the producers must agree on key names, or the drain
        refetches forever (the v2 bump would have recreated 2026-08-19)."""
        class NoRecs:
            recommendations = None
        monkeypatch.setattr(market_data, "_ticker", lambda s: NoRecs())
        monkeypatch.setattr(sources, "ratings_spread",
                            lambda s: market_data.Sourced.live(
                                {"strongBuy": 1, "buy": 1, "hold": 1,
                                 "sell": 0, "strongSell": 0, "total": 3},
                                "finnhub:recommendation-trends"))
        market_data.analyst_recommendations("ZZLD4")
        recs, _ = market_data._symbols_missing_factor_keys(["ZZLD4"])
        assert recs == []

    def test_dual_provider_failure_is_not_structural_absence(self, monkeypatch):
        """CR PR60: rate-limited Yahoo + unavailable Finnhub must keep the
        error identity -- 'no ratings published' would two-strike into a 6h
        structural negative during a transient outage."""
        class YahooDies:
            @property
            def recommendations(self):
                raise RuntimeError("429 Too Many Requests")
        monkeypatch.setattr(market_data, "_ticker", lambda s: YahooDies())
        monkeypatch.setattr(sources, "ratings_spread",
                            lambda s: market_data.Sourced.unavailable(
                                "finnhub:recommendation-trends", "quota"))
        result = market_data.analyst_recommendations("ZZLD5")
        assert not result.ok
        assert result.reason != "no ratings published"
        assert "RuntimeError" in result.reason

    def test_clean_empty_yahoo_and_empty_fallback_stays_structural(self, monkeypatch):
        class NoCoverage:
            recommendations = None
        monkeypatch.setattr(market_data, "_ticker", lambda s: NoCoverage())
        monkeypatch.setattr(sources, "ratings_spread",
                            lambda s: market_data.Sourced.unavailable(
                                "finnhub:recommendation-trends", "no ratings published"))
        result = market_data.analyst_recommendations("ZZLD6")
        assert not result.ok
        assert result.reason == "no ratings published"


class TestTargetFallback:
    def test_fmp_summary_prefers_quarter_window(self, monkeypatch):
        monkeypatch.setattr(sources.requests, "get", _fake_get({
            "price-target-summary": [
                {"lastQuarterCount": 5, "lastQuarterAvgPriceTarget": 12.5,
                 "lastYearCount": 20, "lastYearAvgPriceTarget": 15.0}]}))
        result = sources.price_targets("ZZTF1")
        assert result.ok
        assert result.value == {"mean": 12.5, "count": 5, "window": "3mo"}

    def test_fmp_summary_falls_back_to_year_window(self, monkeypatch):
        monkeypatch.setattr(sources.requests, "get", _fake_get({
            "price-target-summary": [
                {"lastQuarterCount": 0, "lastQuarterAvgPriceTarget": 0,
                 "lastYearCount": 8, "lastYearAvgPriceTarget": 30.0}]}))
        result = sources.price_targets("ZZTF2")
        assert result.ok
        assert result.value == {"mean": 30.0, "count": 8, "window": "12mo"}

    def test_producer_fills_from_fmp_when_profile_blocked(self, monkeypatch):
        monkeypatch.setattr(market_data, "_info",
                            lambda sym, allow_fetch=True: {"ok": False, "reason": "401"})
        monkeypatch.setattr(sources, "price_targets",
                            lambda sym, allow_fetch=True: market_data.Sourced.live(
                                {"mean": 205.25, "count": 12, "window": "3mo"},
                                "fmp:price-target-summary"))
        result = market_data.analyst_target("ZZTF3")
        assert result["mean"].ok and result["mean"].value == 205.25
        assert result["analysts"].value == 12
        assert result["mean"].source == "fmp:price-target-summary"
        assert not result["high"].ok  # fallback feed has no extremes; never invent

    def test_thin_fallback_coverage_still_gated(self, monkeypatch):
        monkeypatch.setattr(market_data, "_info",
                            lambda sym, allow_fetch=True: {"ok": False, "reason": "401"})
        monkeypatch.setattr(sources, "price_targets",
                            lambda sym, allow_fetch=True: market_data.Sourced.live(
                                {"mean": 9.0, "count": 1, "window": "3mo"},
                                "fmp:price-target-summary"))
        result = market_data.analyst_target("ZZTF4")
        assert not result["mean"].ok
        assert "1 analyst" in result["mean"].reason

    def test_healthy_yahoo_never_touches_fmp(self, monkeypatch):
        calls = []
        monkeypatch.setattr(market_data, "_info",
                            lambda sym, allow_fetch=True: {
                                "ok": True, "target_mean": 50.0, "analysts": 9,
                                "target_high": 60.0, "target_low": 40.0})
        monkeypatch.setattr(sources, "price_targets",
                            lambda sym, allow_fetch=True: calls.append(sym))
        result = market_data.analyst_target("ZZTF5")
        assert result["mean"].ok and result["mean"].source == "yfinance:targetMeanPrice"
        assert result["high"].ok
        assert calls == []

    def test_lane_scan_targets_only_failed_profiles(self):
        market_data._cache.set("info:ZZTF6", {"ok": False, "reason": "401"}, 60)
        market_data._cache.set("info:ZZTF7", {"ok": True, "target_mean": 5.0}, 60)
        market_data._cache.set("info:ZZTF8", {"ok": False, "reason": "401"}, 60)
        market_data._cache.set("src:targets:ZZTF8", {"ok": True}, 60)
        need = market_data._symbols_needing_target_fallback(
            ["ZZTF6", "ZZTF7", "ZZTF8", "ZZTF9", "^GSPC"])
        assert need == ["ZZTF6"]

    def test_one_fmp_request_per_symbol_per_day(self, monkeypatch):
        """CR PR62: response-cache TTLs (5-min structural first strike,
        off-market shortening) must not translate into repeat FMP spends.
        The day stamp caps HTTP at one, whatever the response cache does."""
        hits = []

        def counting(url, params=None, headers=None, timeout=None, **kw):
            hits.append(url)
            return FakeResponse([])  # structural: no coverage published

        monkeypatch.setattr(sources.requests, "get", counting)
        first = sources.price_targets("ZZTF10")
        assert not first.ok and "no analyst coverage" in first.reason
        # simulate the 5-minute first-strike expiry: response cache gone
        market_data._cache._local.pop("src:targets:ZZTF10", None)
        second = sources.price_targets("ZZTF10")
        # the day's answer replays -- same refusal, no second HTTP spend
        assert not second.ok
        assert "no analyst coverage" in second.reason
        assert len(hits) == 1

    def test_day_stamp_set_even_on_transport_failure(self, monkeypatch):
        def dying(url, params=None, headers=None, timeout=None, **kw):
            raise RuntimeError("connection reset")
        monkeypatch.setattr(sources.requests, "get", dying)
        first = sources.price_targets("ZZTF11")
        assert not first.ok
        market_data._cache._local.pop("src:targets:ZZTF11", None)
        monkeypatch.setattr(sources.requests, "get",
                            lambda *a, **k: (_ for _ in ()).throw(AssertionError("second HTTP")))
        second = sources.price_targets("ZZTF11")
        assert not second.ok and "fmp targets failed" in second.reason


class TestPaperWindowFix:
    def test_failure_reason_carries_provider_body(self, monkeypatch):
        """The 2026-08-19 incident report said only 'HTTPError'; Alpaca's body
        named the opg window rule. Refusals must keep the provider's words."""
        class Resp:
            status_code = 403
            text = '{"code":40310000,"message":"opg orders must be submitted after 7:00pm"}'
            def raise_for_status(self):
                import requests as rq
                err = rq.HTTPError("403")
                err.response = self
                raise err
            def json(self):
                return {}
        monkeypatch.setattr(sources.requests, "post", lambda *a, **k: Resp())
        result = sources.paper_execute_picks([{"symbol": "ZZPW1", "price": 10.0}])
        assert not result.ok
        assert "40310000" in result.reason or "opg" in result.reason

    def test_order_id_uses_eastern_trading_day(self, monkeypatch):
        from datetime import date as _date
        monkeypatch.setattr(sources, "_eastern_today", lambda: _date(2026, 8, 19))
        captured = {}

        class Resp:
            status_code = 200
            text = "{}"
            def raise_for_status(self): pass
            def json(self): return {"id": "x", "status": "accepted"}

        def post(url, headers=None, timeout=None, json=None, **kw):
            captured.update(json or {})
            return Resp()
        monkeypatch.setattr(sources.requests, "post", post)
        result = sources.paper_execute_picks([{"symbol": "ZZPW2", "price": 10.0}])
        assert result.ok
        assert captured["client_order_id"] == "snap-2026-08-19-ZZPW2"

    def test_expensive_stock_still_buys_one_share(self, monkeypatch):
        captured = {}

        class Resp:
            status_code = 200
            text = "{}"
            def raise_for_status(self): pass
            def json(self): return {"id": "x", "status": "accepted"}

        def post(url, headers=None, timeout=None, json=None, **kw):
            captured.update(json or {})
            return Resp()
        monkeypatch.setattr(sources.requests, "post", post)
        result = sources.paper_execute_picks(
            [{"symbol": "ZZPW3", "price": sources.PAPER_NOTIONAL_PER_PICK * 3}])
        assert result.ok
        assert captured["qty"] == "1"

    def test_missing_price_refuses_that_pick_only(self, monkeypatch):
        class Resp:
            status_code = 200
            text = "{}"
            def raise_for_status(self): pass
            def json(self): return {"id": "x", "status": "accepted"}
        monkeypatch.setattr(sources.requests, "post", lambda *a, **k: Resp())
        result = sources.paper_execute_picks(
            [{"symbol": "ZZPW4", "price": None},
             {"symbol": "ZZPW5", "price": 20.0}])
        assert result.ok
        assert [o["symbol"] for o in result.value["submitted"]] == ["ZZPW5"]
        assert result.value["failed"][0]["symbol"] == "ZZPW4"
        assert "no price" in result.value["failed"][0]["reason"]

class TestShortFloatBackup:
    def test_composes_finra_short_over_fmp_float(self, monkeypatch):
        def fake(url, params=None, headers=None, timeout=None, json=None, **kw):
            if "partitions" in url:
                return FakeResponse({"availablePartitions": [
                    {"partitions": ["2026-07-31"]}, {"partitions": ["2026-07-15"]}]})
            if "consolidatedShortInterest" in url:
                assert json["compareFilters"][1]["fieldValue"] == "2026-07-31"
                return FakeResponse([{"currentShortPositionQuantity": 141606163,
                                      "settlementDate": "2026-07-31"}])
            if "shares-float" in url:
                return FakeResponse([{"floatShares": 14669554809,
                                      "date": "2026-08-19 04:02:30"}])
            raise AssertionError(f"unrouted {url}")
        monkeypatch.setattr(sources.requests, "get", fake)
        monkeypatch.setattr(sources.requests, "post", fake)
        result = sources.short_percent_float("AAPL")
        assert result.ok
        assert result.value == 0.0097
        assert result.source.startswith("derived:")
        assert "settlement 2026-07-31" in result.source

    def test_refuses_implausible_ratio(self, monkeypatch):
        def fake(url, params=None, headers=None, timeout=None, json=None, **kw):
            if "partitions" in url:
                return FakeResponse({"availablePartitions": [{"partitions": ["2026-07-31"]}]})
            if "consolidatedShortInterest" in url:
                return FakeResponse([{"currentShortPositionQuantity": 900}])
            if "shares-float" in url:
                return FakeResponse([{"floatShares": 100, "date": "2026-08-19"}])
            raise AssertionError(f"unrouted {url}")
        monkeypatch.setattr(sources.requests, "get", fake)
        monkeypatch.setattr(sources.requests, "post", fake)
        result = sources.short_percent_float("ZZSF2")
        assert not result.ok
        assert "implausible" in result.reason

    def test_profile_fills_short_float_when_yahoo_blocked(self, monkeypatch):
        monkeypatch.setattr(market_data, "_info",
                            lambda sym, allow_fetch=True: {"ok": False, "reason": "401"})
        monkeypatch.setattr(sources, "short_percent_float",
                            lambda sym, allow_fetch=True: market_data.Sourced.live(
                                0.0913, "finra:consolidated-short-interest (settlement 2026-07-31) / fmp:shares-float"))
        prof = market_data.profile("ZZSF3")
        assert prof["short_pct_float"].ok
        assert prof["short_pct_float"].value == 0.0913
        assert not prof["sector"].ok  # only the backed-up field recovers

    def test_profile_healthy_yahoo_never_calls_finra(self, monkeypatch):
        calls = []
        monkeypatch.setattr(market_data, "_info",
                            lambda sym, allow_fetch=True: {
                                "ok": True, "name": "X", "sector": "Tech",
                                "industry": "Chips", "short_pct_float": 0.05,
                                "held_pct_institutions": 0.6, "avg_volume": 1e6})
        monkeypatch.setattr(sources, "short_percent_float",
                            lambda sym, allow_fetch=True: calls.append(sym))
        prof = market_data.profile("ZZSF4")
        assert prof["short_pct_float"].value == 0.05
        assert prof["short_pct_float"].source == "yfinance:info"
        assert calls == []

    def test_lane_scan_short_only_failed_profiles(self):
        market_data._cache.set("info:ZZSF5", {"ok": False, "reason": "401"}, 60)
        market_data._cache.set("info:ZZSF6", {"ok": True, "short_pct_float": 0.02}, 60)
        market_data._cache.set("info:ZZSF7", {"ok": True, "short_pct_float": None}, 60)
        market_data._cache.set("info:ZZSF8", {"ok": False, "reason": "401"}, 60)
        market_data._cache.set("src:shortfloat:ZZSF8", {"ok": True}, 60)
        need = market_data._symbols_needing_short_fallback(
            ["ZZSF5", "ZZSF6", "ZZSF7", "ZZSF8", "ZZSF9", "^VIX"])
        assert need == ["ZZSF5", "ZZSF7"]

    def test_one_finra_spend_per_symbol_per_day(self, monkeypatch):
        hits = []
        def fake(url, params=None, headers=None, timeout=None, json=None, **kw):
            hits.append(url)
            if "partitions" in url:
                return FakeResponse({"availablePartitions": [{"partitions": ["2026-07-31"]}]})
            return FakeResponse([])
        monkeypatch.setattr(sources.requests, "get", fake)
        monkeypatch.setattr(sources.requests, "post", fake)
        first = sources.short_percent_float("ZZSF10")
        assert not first.ok and "no short interest" in first.reason
        market_data._cache._local.pop("src:shortfloat:ZZSF10", None)
        second = sources.short_percent_float("ZZSF10")
        assert not second.ok and "no short interest" in second.reason
        assert len([h for h in hits if "data/group" in h]) == 1


    def test_exact_150_percent_is_accepted(self, monkeypatch):
        def fake(url, params=None, headers=None, timeout=None, json=None, **kw):
            if "partitions" in url:
                return FakeResponse({"availablePartitions": [{"partitions": ["2026-07-31"]}]})
            if "consolidatedShortInterest" in url:
                return FakeResponse([{"currentShortPositionQuantity": 150}])
            if "shares-float" in url:
                return FakeResponse([{"floatShares": 100, "date": "2026-08-19"}])
            raise AssertionError(f"unrouted {url}")
        monkeypatch.setattr(sources.requests, "get", fake)
        monkeypatch.setattr(sources.requests, "post", fake)
        result = sources.short_percent_float("ZZSF11")
        assert result.ok
        assert result.value == 1.5


class TestCacheContracts:
    def test_effective_ttl_never_shrinks_long_entries(self, monkeypatch):
        """CR PR66: the 12h stretch ceiling was shortening every daily+ cache
        off-market, silently re-spending provider budget."""
        monkeypatch.setattr(market_data, "market_phase", lambda: {"phase": "closed"})
        day = 24 * 60 * 60
        assert market_data._effective_ttl(day, spread=0) == day
        assert market_data._effective_ttl(7 * day, spread=0) == 7 * day
        # short TTLs still stretch, bounded by the 12h ceiling
        assert market_data._effective_ttl(15 * 60, spread=0) == 8 * 15 * 60

    def test_claim_once_is_single_winner(self):
        key = "test:claim:zz1"
        results = [market_data._cache.claim_once(key, 60) for _ in range(3)]
        assert results == [True, False, False]


    def test_effective_ttl_jitter_never_dips_below_base(self, monkeypatch):
        monkeypatch.setattr(market_data, "market_phase", lambda: {"phase": "closed"})
        day = 24 * 60 * 60
        assert all(market_data._effective_ttl(day) >= day for _ in range(60))

    def test_nonfinite_float_shares_refused(self, monkeypatch):
        def fake(url, params=None, headers=None, timeout=None, json=None, **kw):
            if "shares-float" in url:
                return FakeResponse([{"floatShares": float("nan"), "date": "2026-08-19"}])
            raise AssertionError(f"unrouted {url}")
        monkeypatch.setattr(sources.requests, "get", fake)
        result = sources.shares_float("ZZNF1")
        assert not result.ok
        assert result.reason == "float not reported"

    def test_claim_loser_waits_for_winner_answer(self, monkeypatch):
        """CR PR66 follow-up: a concurrent-miss loser must return the
        winner's answer, not cache a day-scoped failure."""
        import threading

        def fake(url, params=None, headers=None, timeout=None, json=None, **kw):
            if "price-target-summary" in url:
                time.sleep(1.0)  # winner in flight while the loser races
                return FakeResponse([{"lastQuarterCount": 6,
                                      "lastQuarterAvgPriceTarget": 42.0}])
            raise AssertionError(f"unrouted {url}")
        monkeypatch.setattr(sources.requests, "get", fake)
        results = {}

        def call(tag):
            results[tag] = sources.price_targets("ZZIF1")
        first = threading.Thread(target=call, args=("winner",))
        first.start()
        time.sleep(0.2)  # let the winner claim
        market_data._cache._local.pop("src:targets:ZZIF1", None)
        call("loser")
        first.join()
        assert results["winner"].ok and results["winner"].value["mean"] == 42.0
        assert results["loser"].ok and results["loser"].value["mean"] == 42.0

    def test_unpriceable_pick_does_not_burn_a_slot(self, monkeypatch):
        """CR PR67: validation must precede the pick cap, or a bad top pick
        excludes a valid lower-ranked one."""
        submitted = []

        class Resp:
            status_code = 200
            text = "{}"
            def raise_for_status(self): pass
            def json(self): return {"id": "x", "status": "accepted"}

        def post(url, headers=None, timeout=None, json=None, **kw):
            submitted.append(json["symbol"])
            return Resp()
        monkeypatch.setattr(sources.requests, "post", post)
        result = sources.paper_execute_picks(
            [{"symbol": "BAD1", "price": None},
             {"symbol": "OK1", "price": 10.0},
             {"symbol": "OK2", "price": 10.0},
             {"symbol": "OK3", "price": 10.0},
             {"symbol": "OK4", "price": 10.0}])
        assert result.ok
        assert submitted == ["OK1", "OK2", "OK3"]
        assert result.value["failed"][0]["symbol"] == "BAD1"

    def test_duplicate_retry_keeps_qty_and_ref_price(self, monkeypatch):
        class Dup:
            status_code = 422
            text = '{"message":"client_order_id must be unique"}'
            def raise_for_status(self): pass
            def json(self): return {}
        monkeypatch.setattr(sources.requests, "post", lambda *a, **k: Dup())
        result = sources.paper_execute_picks([{"symbol": "ZZDR1", "price": 25.0}])
        assert result.ok
        row = result.value["submitted"][0]
        assert row["status"] == "already-submitted"
        assert row["qty"] == 40 and row["ref_price"] == 25.0


class TestLastTwoBackups:
    @pytest.fixture(autouse=True)
    def _isolated_options_cooldown(self):
        prior = market_data._options_cooldown_until[0]
        market_data._options_cooldown_until[0] = 0
        yield
        market_data._options_cooldown_until[0] = prior

    def test_company_news_maps_finnhub_shape(self, monkeypatch):
        monkeypatch.setattr(sources.requests, "get", _fake_get({
            "company-news": [
                {"headline": "Title A", "source": "SeekingAlpha",
                 "datetime": 1787182806, "url": "https://x/a"},
                {"headline": "", "source": "skip-me"},
                {"headline": "Title B", "source": "Reuters",
                 "datetime": 1787182800, "url": "https://x/b"}]}))
        result = sources.company_news("ZZNW1", limit=5)
        assert result.ok
        assert [i["title"] for i in result.value] == ["Title A", "Title B"]
        assert result.value[0]["publisher"] == "SeekingAlpha"
        assert result.value[0]["published"].endswith("Z")

    def test_headlines_fall_back_to_finnhub(self, monkeypatch):
        class NoNews:
            news = []
        monkeypatch.setattr(market_data, "_ticker", lambda s: NoNews())
        monkeypatch.setattr(sources, "company_news",
                            lambda sym, limit=5: market_data.Sourced.live(
                                [{"title": "T", "publisher": "P",
                                  "published": None, "url": None}],
                                "finnhub:company-news"))
        result = market_data.headlines("ZZNW2")
        assert result.ok
        assert result.source == "finnhub:company-news"
        assert result.value[0]["title"] == "T"

    def test_straddle_from_alpaca_quotes(self, monkeypatch):
        exp = (date.today() + timedelta(days=30)).strftime("%y%m%d")

        def fake(url, params=None, headers=None, timeout=None, **kw):
            assert "/v1beta1/options/snapshots/ZZIM1" in url
            return FakeResponse({"snapshots": {
                # expiry 30d out, strikes 10 and 12 both-sided
                f"ZZIM1{exp}C00010000": {"latestQuote": {"bp": 1.0, "ap": 1.2}},
                f"ZZIM1{exp}P00010000": {"latestQuote": {"bp": 0.8, "ap": 1.0}},
                f"ZZIM1{exp}C00012000": {"latestQuote": {"bp": 0.4, "ap": 0.6}},
                f"ZZIM1{exp}P00012000": {"latestQuote": {"bp": 1.6, "ap": 1.8}},
            }})
        monkeypatch.setattr(sources.requests, "get", fake)
        result = sources.implied_straddle_move("ZZIM1", spot=10.4)
        assert result.ok
        v = result.value
        assert v["strike"] == 10.0  # nearest to spot
        # mids: call 1.1, put 0.9 -> 2.0 / 10.4 = 19.2%
        assert v["implied_move_pct"] == 19.2
        assert v["quality"] == "ok"

    def test_straddle_last_trade_quality_flagged(self, monkeypatch):
        exp = (date.today() + timedelta(days=30)).strftime("%y%m%d")

        def fake(url, params=None, headers=None, timeout=None, **kw):
            return FakeResponse({"snapshots": {
                f"ZZIM2{exp}C00010000": {"dailyBar": {"c": 1.1}},
                f"ZZIM2{exp}P00010000": {"latestQuote": {"bp": 0.8, "ap": 1.0}},
            }})
        monkeypatch.setattr(sources.requests, "get", fake)
        result = sources.implied_straddle_move("ZZIM2", spot=10.0)
        assert result.ok
        assert "last-trade" in result.value["quality"]

    def test_implied_move_falls_back_during_cooldown(self, monkeypatch):
        import time as _time
        market_data._options_cooldown_until[0] = _time.time() + 600
        monkeypatch.setattr(market_data, "technicals",
                            lambda sym, allow_fetch=True: market_data.Sourced.live(
                                {"close": 20.0}, "test"))
        monkeypatch.setattr(sources, "implied_straddle_move",
                            lambda sym, spot: market_data.Sourced.live(
                                {"expiry": "2026-09-18", "days_to_expiry": 29,
                                 "implied_move_pct": 12.5, "spot": spot,
                                 "strike": 20.0, "quality": "ok",
                                 "estimate_basis": "x"},
                                "alpaca:options-indicative-straddle"))
        market_data._cache._local.pop("implied:ZZIM3", None)
        result = market_data.implied_move("ZZIM3")
        assert result.ok
        assert result.value["implied_move_pct"] == 12.5
        assert "alpaca" in result.source

    def test_news_dual_failure_keeps_error_identity(self, monkeypatch):
        class YahooDies:
            @property
            def news(self):
                raise RuntimeError("429 Too Many Requests")
        monkeypatch.setattr(market_data, "_ticker", lambda s: YahooDies())
        monkeypatch.setattr(sources, "company_news",
                            lambda sym, limit=5: market_data.Sourced.unavailable(
                                "finnhub:company-news", "quota"))
        result = market_data.headlines("ZZNW3")
        assert not result.ok
        assert result.reason != "no recent headlines"
        assert "RuntimeError" in result.reason

    def test_implied_move_rate_limit_single_fallback_attempt(self, monkeypatch):
        calls = []

        class ExpiryDies:
            @property
            def options(self):
                raise RuntimeError("429 Too Many Requests")
        monkeypatch.setattr(market_data, "_ticker", lambda s: ExpiryDies())
        monkeypatch.setattr(market_data, "technicals",
                            lambda sym, allow_fetch=True: market_data.Sourced.live(
                                {"close": 20.0}, "test"))

        def counting_unavailable(sym, spot):
            calls.append(sym)
            return market_data.Sourced.unavailable(
                "alpaca:options-indicative-straddle", "budget exhausted")
        monkeypatch.setattr(sources, "implied_straddle_move", counting_unavailable)
        result = market_data.implied_move("ZZIM4")
        assert not result.ok
        assert len(calls) == 1


class TestEarningsIntegrity:
    def test_finnhub_rows_filtered_by_symbol(self, monkeypatch):
        """Live 2026-08-20: the free-tier calendar ignored the symbol param
        and STLD/MTSI wore SMTC's Aug 25. Foreign rows must never count."""
        def fake(url, params=None, headers=None, timeout=None, **kw):
            if "earnings-calendar" in url:   # FMP branch: force the fallback
                return FakeResponse(None, status=402)
            if "calendar/earnings" in url:
                return FakeResponse({"earningsCalendar": [
                    {"symbol": "SMTC", "date": "2026-08-25"},
                    {"symbol": "ZZEI1", "date": "2026-10-19"}]})
            raise AssertionError(f"unrouted {url}")
        monkeypatch.setattr(sources.requests, "get", fake)
        result = sources.earnings_confirmed("ZZEI1")
        assert result.ok
        assert result.value["date"] == "2026-10-19"

    def test_no_own_rows_refuses_rather_than_borrow(self, monkeypatch):
        def fake(url, params=None, headers=None, timeout=None, **kw):
            if "earnings-calendar" in url:
                return FakeResponse(None, status=402)
            if "calendar/earnings" in url:
                return FakeResponse({"earningsCalendar": [
                    {"symbol": "SMTC", "date": "2026-08-25"}]})
            raise AssertionError(f"unrouted {url}")
        monkeypatch.setattr(sources.requests, "get", fake)
        result = sources.earnings_confirmed("ZZEI2")
        assert not result.ok
        assert "no confirmed earnings" in result.reason


class TestProviderPrinciples:
    def test_alpaca_losers_filters_junk_and_shapes_rows(self, monkeypatch):
        monkeypatch.setattr(sources.requests, "get", _fake_get({
            "/v1beta1/screener/stocks/movers": {"losers": [
                {"symbol": "REAL1", "percent_change": -12.5, "change": -3.1, "price": 21.7},
                {"symbol": "GDEVW", "percent_change": -50.0, "change": -0.01, "price": 0.02},
                {"symbol": "FTRA.WS", "percent_change": -40.0, "change": -0.2, "price": 0.3},
                {"symbol": "PENNY", "percent_change": -30.0, "change": -0.2, "price": 0.55},
                {"symbol": "REAL2", "percent_change": -9.9, "change": -1.0, "price": 9.1}]}}))
        result = sources.alpaca_losers()
        assert result.ok
        assert [r["Symbol"] for r in result.value] == ["REAL1", "REAL2"]
        assert result.value[0]["Percent Change"] == "-12.50%"

    def test_universe_prefers_alpaca_over_fmp(self, monkeypatch):
        import app
        monkeypatch.setattr(app, "scrape_yahoo_losers",
                            lambda: ([], {"success": False, "message": "boom"}))
        monkeypatch.setattr(sources, "alpaca_losers",
                            lambda: market_data.Sourced.live(
                                [{"Symbol": "ZZAL1", "Name": "A", "Change": "-1",
                                  "Percent Change": "-8.00%", "Volume": "n/a",
                                  "Market Cap": "n/a"}], "alpaca:movers-losers"))
        monkeypatch.setattr(sources, "fmp_losers",
                            lambda: (_ for _ in ()).throw(AssertionError("fmp reached")))
        market_data._cache._local.pop("universe:v1", None)
        losers, status = app.stable_universe()
        assert status["data_source"] == "alpaca-failover"
        assert losers[0]["Symbol"] == "ZZAL1"

    def test_universe_falls_to_fmp_when_alpaca_dead(self, monkeypatch):
        import app
        monkeypatch.setattr(app, "scrape_yahoo_losers",
                            lambda: ([], {"success": False, "message": "boom"}))
        monkeypatch.setattr(sources, "alpaca_losers",
                            lambda: market_data.Sourced.unavailable("alpaca:movers-losers", "down"))
        monkeypatch.setattr(sources, "fmp_losers",
                            lambda: market_data.Sourced.live(
                                [{"Symbol": "ZZFL1", "Name": "F", "Change": "-1",
                                  "Percent Change": "-9.00%", "Volume": "n/a",
                                  "Market Cap": "n/a"}], "fmp:biggest-losers"))
        market_data._cache._local.pop("universe:v1", None)
        losers, status = app.stable_universe()
        assert status["data_source"] == "fmp-failover"

    def test_ratings_finnhub_first_yahoo_never_touched(self, monkeypatch):
        calls = []
        monkeypatch.setattr(market_data, "_ticker",
                            lambda s: calls.append(s))
        monkeypatch.setattr(sources, "ratings_spread",
                            lambda s: market_data.Sourced.live(
                                {"strongBuy": 5, "buy": 10, "hold": 4,
                                 "sell": 1, "strongSell": 0, "total": 20},
                                "finnhub:recommendation-trends"))
        market_data._cache._local.pop("recs:v2:ZZPP1", None)
        result = market_data.analyst_recommendations("ZZPP1")
        assert result.ok and result.source == "finnhub:recommendation-trends"
        assert calls == []

    def test_ratings_yahoo_backup_engages(self, monkeypatch):
        import pandas as pd
        class YahooHas:
            recommendations = pd.DataFrame([{"strongBuy": 2, "buy": 3, "hold": 1,
                                             "sell": 0, "strongSell": 0}])
        monkeypatch.setattr(market_data, "_ticker", lambda s: YahooHas())
        monkeypatch.setattr(sources, "ratings_spread",
                            lambda s: market_data.Sourced.unavailable(
                                "finnhub:recommendation-trends", "quota"))
        market_data._cache._local.pop("recs:v2:ZZPP2", None)
        result = market_data.analyst_recommendations("ZZPP2")
        assert result.ok
        assert result.source == "yfinance:recommendations"
        assert result.value["total"] == 6

    def test_news_finnhub_first(self, monkeypatch):
        monkeypatch.setattr(market_data, "_ticker",
                            lambda s: (_ for _ in ()).throw(AssertionError("yahoo touched")))
        monkeypatch.setattr(sources, "company_news",
                            lambda sym, limit=5: market_data.Sourced.live(
                                [{"title": "T", "publisher": "P", "published": None,
                                  "url": None}], "finnhub:company-news"))
        market_data._cache._local.pop("news:ZZPP3:5", None)
        result = market_data.headlines("ZZPP3")
        assert result.ok and result.source == "finnhub:company-news"

    def test_earnings_finnhub_first_fmp_untouched(self, monkeypatch):
        def fake(url, params=None, headers=None, timeout=None, **kw):
            if "calendar/earnings" in url:
                return FakeResponse({"earningsCalendar": [
                    {"symbol": "ZZPP4", "date": "2026-09-10"}]})
            raise AssertionError(f"unexpected call {url}")
        monkeypatch.setattr(sources.requests, "get", fake)
        result = sources.earnings_confirmed("ZZPP4")
        assert result.ok and result.value["date"] == "2026-09-10"
        assert "finnhub" in result.source

    def test_blocked_profile_fills_name_industry_from_finnhub(self, monkeypatch):
        monkeypatch.setattr(market_data, "_info",
                            lambda sym, allow_fetch=True: {"ok": False, "reason": "401"})
        monkeypatch.setattr(sources, "short_percent_float",
                            lambda sym, allow_fetch=True: market_data.Sourced.unavailable(
                                "finra", "cold"))
        monkeypatch.setattr(sources, "company_profile",
                            lambda sym: market_data.Sourced.live(
                                {"name": "Zeta Corp", "industry": "Semiconductors"},
                                "finnhub:profile2"))
        prof = market_data.profile("ZZPP5")
        assert prof["industry"].ok and prof["industry"].value == "Semiconductors"
        assert prof["name"].value == "Zeta Corp"
        assert not prof["sector"].ok  # profile2 has no GICS sector; stays honest

    def test_paper_orders_refuse_while_market_open(self, monkeypatch):
        monkeypatch.setattr(market_data, "market_phase", lambda: {"phase": "open"})
        monkeypatch.setattr(sources.requests, "post",
                            lambda *a, **k: (_ for _ in ()).throw(AssertionError("submitted")))
        result = sources.paper_execute_picks([{"symbol": "ZZMO1", "price": 10.0}])
        assert not result.ok
        assert "market is open" in result.reason

    def test_paper_orders_proceed_after_hours(self, monkeypatch):
        monkeypatch.setattr(market_data, "market_phase", lambda: {"phase": "after_hours"})

        class Resp:
            status_code = 200
            text = "{}"
            def raise_for_status(self): pass
            def json(self): return {"id": "x", "status": "accepted"}
        monkeypatch.setattr(sources.requests, "post", lambda *a, **k: Resp())
        result = sources.paper_execute_picks([{"symbol": "ZZMO2", "price": 10.0}])
        assert result.ok and result.value["submitted"][0]["symbol"] == "ZZMO2"
