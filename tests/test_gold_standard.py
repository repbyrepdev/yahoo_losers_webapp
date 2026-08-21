"""Tests for the gold-standard batch: XBRL fundamentals, Form 4 direction,
sector context, calibration, walk-forward fitting, and the honesty UX."""

import json
import os
import sys
import time


sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import market_data
import tracking
import walkforward


BUY_XML = """<?xml version="1.0"?>
<ownershipDocument>
  <nonDerivativeTable>
    <nonDerivativeTransaction>
      <transactionCoding><transactionCode>P</transactionCode></transactionCoding>
      <transactionAmounts>
        <transactionShares><value>1000</value></transactionShares>
        <transactionPricePerShare><value>25.50</value></transactionPricePerShare>
      </transactionAmounts>
    </nonDerivativeTransaction>
  </nonDerivativeTable>
</ownershipDocument>"""

SELL_XML = BUY_XML.replace(">P<", ">S<").replace("25.50", "10.00")

GRANT_XML = BUY_XML.replace(">P<", ">A<")  # award, not an open-market trade


class TestForm4Parsing:
    def test_buy_parsed(self):
        totals = market_data._parse_form4_xml(BUY_XML)
        assert totals == {"buy_value": 25500.0, "sell_value": 0.0,
                          "buys": 1, "sells": 0, "unpriced": 0}

    def test_unpriced_transaction_excluded_from_totals(self):
        xml = BUY_XML.replace(
            "<transactionPricePerShare><value>25.50</value></transactionPricePerShare>", "")
        totals = market_data._parse_form4_xml(xml)
        assert totals["unpriced"] == 1
        assert totals["buy_value"] == 0.0 and totals["buys"] == 0

    def test_mismatched_flow_periods_omit_fcf(self, monkeypatch):
        facts = {"facts": {"us-gaap": {
            "NetCashProvidedByUsedInOperatingActivities": {"units": {"USD": [
                {"start": "2026-04-01", "end": "2026-06-30", "val": 80.0}]}},
            "PaymentsToAcquirePropertyPlantAndEquipment": {"units": {"USD": [
                {"start": "2025-07-01", "end": "2026-06-30", "val": 30.0}]}},
        }}}
        monkeypatch.setattr(market_data, "_edgar_cik_table",
                            lambda: {"ok": True, "table": {"ZZPD1": "0000123456"}})

        class FakeResponse:
            def raise_for_status(self):
                pass
            def json(self):
                return facts

        import requests
        monkeypatch.setattr(requests, "get", lambda url, **kw: FakeResponse())
        monkeypatch.setattr(market_data, "_throttle", lambda: None)
        result = market_data.sec_fundamentals("ZZPD1")
        assert result.ok
        assert "free_cash_flow" not in result.value  # quarterly OCF vs annual capex

    def test_calibration_no_observed_price_stays_unresolved(self, tmp_path):
        _snap(tmp_path, "2026-01-05",
              [{"symbol": "GONE", "price": 100.0,
                "predictions": {"short_term:t1": {"probability": 0.5, "target_pct": 5.0,
                                                  "horizon_days": 7}}}])
        # Later snapshots exist past the window but never price GONE again.
        _snap(tmp_path, "2026-01-09", [{"symbol": "OTHER", "price": 50.0}])
        _snap(tmp_path, "2026-01-20", [{"symbol": "OTHER", "price": 51.0}])
        calib = tracking.compute_calibration(str(tmp_path))
        assert calib["n_resolved"] == 0 and calib["n_unresolved"] == 1

    def test_sell_parsed(self):
        totals = market_data._parse_form4_xml(SELL_XML)
        assert totals["sell_value"] == 10000.0 and totals["sells"] == 1

    def test_grants_are_not_trades(self):
        assert market_data._parse_form4_xml(GRANT_XML) is None

    def test_malformed_xml_is_none_not_crash(self):
        assert market_data._parse_form4_xml("<not-xml") is None

    def test_insider_filings_aggregates_direction(self, monkeypatch):
        monkeypatch.setattr(market_data, "_edgar_cik_table",
                            lambda: {"ok": True, "table": {"ZZF4A": "0000123456"}})

        class FakeResponse:
            def __init__(self, payload, text=None):
                self._payload, self.text = payload, text
            def raise_for_status(self):
                pass
            def json(self):
                return self._payload

        submissions = {"filings": {"recent": {
            "form": ["4", "4/A", "8-K"],
            "filingDate": ["2099-01-05", "2099-01-03", "2099-01-02"],
            "accessionNumber": ["0001-99-000001", "0001-99-000002", "0001-99-000003"],
            "primaryDocument": ["xslF345X05/form4.xml", "form4a.xml", "eightk.htm"],
        }}}

        def fake_get(url, **kwargs):
            if "submissions" in url:
                return FakeResponse(submissions)
            if "000199000001" in url:
                return FakeResponse(None, text=BUY_XML)
            return FakeResponse(None, text=SELL_XML)

        import requests
        monkeypatch.setattr(requests, "get", fake_get)
        monkeypatch.setattr(market_data, "_throttle", lambda: None)

        result = market_data.insider_filings("ZZF4A", window_days=365000)
        assert result.ok
        assert result.value["count"] == 2
        assert result.value["parsed"] == 2
        assert result.value["buy_value"] == 25500.0
        assert result.value["sell_value"] == 10000.0
        assert result.value["net_value"] == 15500.0

    def test_stylesheet_prefix_stripped(self, monkeypatch):
        """xslF345X05/form4.xml must fetch the raw XML, not the styled view."""
        monkeypatch.setattr(market_data, "_edgar_cik_table",
                            lambda: {"ok": True, "table": {"ZZF4B": "0000123456"}})
        urls = []

        class FakeResponse:
            text = BUY_XML
            def raise_for_status(self):
                pass
            def json(self):
                return {"filings": {"recent": {
                    "form": ["4"], "filingDate": ["2099-01-05"],
                    "accessionNumber": ["0001-99-000001"],
                    "primaryDocument": ["xslF345X05/form4.xml"]}}}

        def fake_get(url, **kwargs):
            urls.append(url)
            return FakeResponse()

        import requests
        monkeypatch.setattr(requests, "get", fake_get)
        monkeypatch.setattr(market_data, "_throttle", lambda: None)
        market_data.insider_filings("ZZF4B", window_days=365000)
        doc_urls = [u for u in urls if "Archives" in u]
        assert doc_urls and doc_urls[0].endswith("/form4.xml")
        assert "xslF345X05" not in doc_urls[0]


COMPANYFACTS = {"facts": {"us-gaap": {
    "CashAndCashEquivalentsAtCarryingValue": {"units": {"USD": [
        {"end": "2025-12-31", "val": 100.0}, {"end": "2026-06-30", "val": 500.0}]}},
    "LongTermDebt": {"units": {"USD": [{"end": "2026-06-30", "val": 200.0}]}},
    "AssetsCurrent": {"units": {"USD": [{"end": "2026-06-30", "val": 900.0}]}},
    "LiabilitiesCurrent": {"units": {"USD": [{"end": "2026-06-30", "val": 300.0}]}},
    "NetCashProvidedByUsedInOperatingActivities": {"units": {"USD": [
        {"end": "2026-06-30", "val": 80.0}]}},
    "PaymentsToAcquirePropertyPlantAndEquipment": {"units": {"USD": [
        {"end": "2026-06-30", "val": 30.0}]}},
}}}


class TestSecFundamentals:
    def _mock(self, monkeypatch, symbol, payload):
        monkeypatch.setattr(market_data, "_edgar_cik_table",
                            lambda: {"ok": True, "table": {symbol: "0000123456"}})

        class FakeResponse:
            def raise_for_status(self):
                pass
            def json(self):
                return payload

        import requests
        monkeypatch.setattr(requests, "get", lambda url, **kw: FakeResponse())
        monkeypatch.setattr(market_data, "_throttle", lambda: None)

    def test_latest_value_and_derivations(self, monkeypatch):
        self._mock(monkeypatch, "ZZXB1", COMPANYFACTS)
        facts = market_data.sec_fundamentals("ZZXB1")
        assert facts.ok
        assert facts.value["cash"] == 500.0          # latest end date wins
        assert facts.value["free_cash_flow"] == 50.0  # 80 - |30|
        assert facts.value["current_ratio"] == 3.0
        assert facts.value["as_of"] == "2026-06-30"

    def test_no_usd_facts_is_unavailable(self, monkeypatch):
        self._mock(monkeypatch, "ZZXB2", {"facts": {"us-gaap": {}}})
        facts = market_data.sec_fundamentals("ZZXB2")
        assert not facts.ok

    def test_unknown_ticker_is_unavailable(self, monkeypatch):
        monkeypatch.setattr(market_data, "_edgar_cik_table",
                            lambda: {"ok": True, "table": {}})
        assert not market_data.sec_fundamentals("ZZXB3").ok


class TestSolvencySource:
    def test_prefers_filed_xbrl(self, monkeypatch):
        monkeypatch.setattr(market_data, "_edgar_cik_table",
                            lambda: {"ok": True, "table": {"ZZSV1": "0000123456"}})
        market_data._cache.set("edgar:facts:ZZSV1",
                               {"ok": True, "cash": 5e8, "debt": 1e8,
                                "free_cash_flow": 2e7, "current_ratio": 2.1,
                                "as_of": "2026-06-30"}, 60)
        market_data._cache.set("info:ZZSV1", {"ok": True, "profit_margins": 0.1}, 60)
        result = market_data.solvency("ZZSV1")
        assert result.ok
        assert result.source.endswith("sec-edgar:xbrl-companyfacts")
        assert result.value["current_ratio"] == 2.1
        assert "SEC-filed XBRL" in result.value["estimate_basis"]

    def test_falls_back_to_yfinance(self, monkeypatch):
        monkeypatch.setattr(market_data, "sec_fundamentals",
                            lambda s: market_data.Sourced.unavailable("sec-edgar:xbrl-companyfacts", "no facts"))
        market_data._cache.set("info:ZZSV2",
                               {"ok": True, "total_cash": 1e6, "total_debt": 2e6,
                                "free_cashflow": -5e5, "profit_margins": -0.2}, 60)
        result = market_data.solvency("ZZSV2")
        assert result.ok
        assert result.source.endswith("yfinance:balance-sheet-fields")
        assert "yfinance-reported" in result.value["estimate_basis"]

    def test_absent_fields_stay_absent(self, monkeypatch):
        monkeypatch.setattr(market_data, "sec_fundamentals",
                            lambda s: market_data.Sourced.unavailable("sec-edgar:xbrl-companyfacts", "no facts"))
        market_data._cache.set("info:ZZSV3", {"ok": True}, 60)
        assert not market_data.solvency("ZZSV3").ok


class TestSectorContext:
    def test_map_covers_yfinance_sectors(self):
        assert len(market_data.SECTOR_ETFS) == 11
        assert market_data.SECTOR_ETFS["Energy"] == "XLE"

    def _seed(self, symbol, sector, etf_closes):
        etf = market_data.SECTOR_ETFS[sector]
        market_data._cache.set(f"info:{symbol}", {"ok": True, "sector": sector}, 60)
        market_data._cache.set(f"hist:{etf}:5y", {"ok": True, "closes": etf_closes}, 60)

    def test_sector_wide_selloff(self):
        self._seed("ZZSC1", "Energy", [100.0, 98.5])  # -1.5%
        result = market_data.sector_context("ZZSC1")
        assert result.ok and result.value["classification"] == "sector_wide"
        assert "sector-wide" in result.value["label"]

    def test_company_specific(self):
        self._seed("ZZSC2", "Technology", [100.0, 100.2])
        result = market_data.sector_context("ZZSC2")
        assert result.ok and result.value["classification"] == "company_specific"

    def test_mixed_band(self):
        self._seed("ZZSC3", "Healthcare", [100.0, 99.4])  # -0.6%
        result = market_data.sector_context("ZZSC3")
        assert result.ok and result.value["classification"] == "mixed"

    def test_no_cached_profile_is_unavailable(self):
        assert not market_data.sector_context("ZZSC4").ok

    def test_day_move_needs_two_closes(self):
        assert market_data._day_move_pct({"closes": [100.0]}) is None
        assert market_data._day_move_pct({"closes": []}) is None


class TestFreshnessStamp:
    def test_computed_technicals_carry_fetched_at(self):
        # Both gains and losses, so Wilder RSI is defined.
        closes = [100.0 + (i % 7) - 3 + i * 0.05 for i in range(60)]
        volumes = [1e6] * 60
        payload = market_data._compute_technicals_from_closes(closes, volumes)
        assert payload["ok"] and payload["fetched_at"] <= time.time()


def _snap(directory, day, rows, tracked=None):
    snap = {"date": day, "universe": rows, "tracked_prices": tracked or {}}
    with open(os.path.join(directory, f"{day}.json"), "w") as handle:
        json.dump(snap, handle)


class TestCalibration:
    def test_not_ready_below_minimum(self, tmp_path):
        _snap(tmp_path, "2026-01-05",
              [{"symbol": "AAA", "price": 100.0,
                "predictions": {"short_term:t1": {"probability": 0.6, "target_pct": 5.0,
                                                  "horizon_days": 10}}}])
        calib = tracking.compute_calibration(str(tmp_path))
        assert not calib["ready"]
        assert calib["n_resolved"] + calib["n_unresolved"] == 1

    def test_hits_and_misses_resolve(self, tmp_path):
        pred = {"short_term:t1": {"probability": 0.5, "target_pct": 5.0, "horizon_days": 7}}
        _snap(tmp_path, "2026-01-05",
              [{"symbol": "HIT", "price": 100.0, "predictions": dict(pred)},
               {"symbol": "MISS", "price": 100.0, "predictions": dict(pred)}])
        # Inside the window HIT reaches 105; MISS never does. A later snapshot
        # past the window resolves both.
        _snap(tmp_path, "2026-01-09",
              [{"symbol": "HIT", "price": 106.0}, {"symbol": "MISS", "price": 99.0}])
        _snap(tmp_path, "2026-01-20",
              [{"symbol": "HIT", "price": 90.0}, {"symbol": "MISS", "price": 101.0}])
        calib = tracking.compute_calibration(str(tmp_path))
        assert calib["n_resolved"] == 2
        # Brier for p=0.5 with one hit and one miss: (0.25 + 0.25) / 2
        assert calib["brier"] == 0.25
        bucket = next(b for b in calib["buckets"] if b["range"] == "40-60%")
        assert bucket["n"] == 2 and bucket["realized_rate"] == 50.0

    def test_unresolved_stays_unresolved(self, tmp_path):
        _snap(tmp_path, "2026-01-05",
              [{"symbol": "OPEN", "price": 100.0,
                "predictions": {"long_term:t1": {"probability": 0.7, "target_pct": 50.0,
                                                 "horizon_days": 183}}}])
        _snap(tmp_path, "2026-01-08", [{"symbol": "OPEN", "price": 101.0}])
        calib = tracking.compute_calibration(str(tmp_path))
        assert calib["n_resolved"] == 0 and calib["n_unresolved"] == 1


class TestWalkForward:
    def test_refuses_below_minimum_days(self, tmp_path):
        _snap(tmp_path, "2026-01-05",
              [{"symbol": "AAA", "price": 100.0, "factors": {"a": {"score": 80}}}])
        wf = walkforward.walk_forward(str(tmp_path))
        assert not wf["ready"]
        assert "collecting" in wf["status"]

    def test_fits_and_evaluates_out_of_sample(self, tmp_path):
        from datetime import date, timedelta
        start = date(2026, 1, 5)
        # 30 days; factor "a" genuinely predicts the 7-day return, factor "b"
        # is noise. Prices follow a's score so the fit has something to find.
        days = [start + timedelta(days=i) for i in range(38)]
        for i, day in enumerate(days):
            rows = []
            for j, symbol in enumerate(("AAA", "BBB", "CCC", "DDD")):
                a_score = (j * 25 + i * 3) % 100
                entry = 100.0 * (1 + a_score / 1000.0) ** i
                rows.append({"symbol": symbol, "price": round(entry, 4),
                             "factors": {"a": {"score": a_score},
                                         "b": {"score": (i * 7 + j * 13) % 100}}})
            _snap(tmp_path, day.isoformat(), rows)
        wf = walkforward.walk_forward(str(tmp_path))
        assert wf["ready"]
        assert wf["test_days"] > 0
        assert set(wf["latest_weights"]) == {"a", "b"}
        assert "report-only" in wf["status"]


class TestHonestyUX:
    def test_force_refresh_button_gone(self):
        source = open(os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "app.py"), encoding="utf-8").read()
        assert "🔄 Force Refresh" not in source  # the rendered button, not comments
        assert "view built" in source
        assert "prices as of" in source

    def test_snapshot_predictions_normalized(self):
        import app
        result = {"timeframe_predictions": {"short_term": {
            "t1": {"probability_available": True, "probability": 62.0, "upside_percent": 5.0},
            "t2": {"probability_available": True, "probability": 40.0, "upside_percent": -3.0},
            "t3": {"probability_available": False},
        }}}
        predictions = app._snapshot_predictions(result)
        assert predictions == {"short_term:t1": {"probability": 0.62, "target_pct": 5.0,
                                                 "target_price": None,
                                                 "horizon_days": 10,
                                                 "horizon_bars": 7}}

    def test_oldest_price_fetch_reads_stamps(self):
        import app
        market_data._cache.set("tech:ZZTS1", {"ok": True, "fetched_at": time.time() - 60}, 60)
        market_data._cache.set("tech:ZZTS2", {"ok": True, "fetched_at": time.time()}, 60)
        assert app._oldest_price_fetch(["ZZTS1", "ZZTS2"]) is not None
        assert app._oldest_price_fetch(["ZZNONE"]) is None

    def test_track_record_page_has_new_sections(self, tmp_path, monkeypatch):
        # Fixture-backed (CR, PR 45): the committed snapshot directory grows
        # daily, so rendering against it made this test slower every day and
        # hid walk-forward failures behind an empty section.
        import app
        import market_data
        _snap(tmp_path, "2026-01-05",
              [{"symbol": "AAA", "price": 100.0, "score": 75.0,
                "factors": {"a": {"score": 80}}}])
        monkeypatch.setattr(tracking, "SNAPSHOT_DIR", str(tmp_path))
        market_data._cache._local.pop("page:track-record", None)
        client = app.app.test_client()
        response = client.get("/track-record")
        assert response.status_code == 200
        html = response.data.decode()
        assert "Calibration" in html
        assert "Walk-forward" in html
        assert "dead-cat baseline" in html
