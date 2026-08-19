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
        assert not second.ok
        assert "already spent today" in second.reason
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
        assert not second.ok and "already spent today" in second.reason


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
        result = sources.paper_execute_picks(["ZZPW1"])
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
        result = sources.paper_execute_picks(["ZZPW2"])
        assert result.ok
        assert captured["client_order_id"] == "snap-2026-08-19-ZZPW2"
