"""The graduation gate: live money is earned by the record, never asserted."""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sources
import tracking


class TestLiveReadiness:
    def test_empty_record_is_not_ready(self, tmp_path):
        r = tracking.live_readiness(str(tmp_path))
        assert r["ready"] is False
        assert all(c["met"] is False for c in r["criteria"])
        names = [c["name"] for c in r["criteria"]]
        assert "resolved predictions" in names and "graded paper fills" in names

    def test_fills_counted_from_snapshots(self, tmp_path):
        for i in range(3):
            (tmp_path / f"2026-08-{10+i}.json").write_text(json.dumps({
                "date": f"2026-08-{10+i}", "universe": [],
                "paper_fills": [{"symbol": "A", "filled_at": "x"},
                                {"symbol": "B", "filled_at": "y"}]}))
        r = tracking.live_readiness(str(tmp_path))
        fills = next(c for c in r["criteria"] if c["name"] == "graded paper fills")
        assert fills["actual"] == 6


class TestLiveArming:
    def test_unarmed_refuses(self, monkeypatch):
        monkeypatch.delenv("LIVE_TRADING_ARMED", raising=False)
        with pytest.raises(RuntimeError, match="not armed"):
            sources._alpaca_trading_base("live")

    def test_armed_without_keys_refuses(self, monkeypatch):
        monkeypatch.setenv("LIVE_TRADING_ARMED", "yes-i-accept-losses")
        monkeypatch.setattr(sources, "get_secret", lambda name, **kw: None)
        with pytest.raises(RuntimeError, match="keys are not configured"):
            sources._alpaca_trading_base("live")

    def test_armed_with_keys_but_unearned_record_refuses(self, monkeypatch):
        monkeypatch.setenv("LIVE_TRADING_ARMED", "yes-i-accept-losses")
        monkeypatch.setattr(sources, "get_secret", lambda name, **kw: "k")
        monkeypatch.setattr(tracking, "live_readiness",
                            lambda directory=None: {"ready": False, "criteria": [
                                {"name": "resolved predictions", "actual": 1,
                                 "required": 100, "met": False}]})
        with pytest.raises(RuntimeError, match="has not earned live money"):
            sources._alpaca_trading_base("live")

    def test_fully_earned_returns_live_base(self, monkeypatch):
        monkeypatch.setenv("LIVE_TRADING_ARMED", "yes-i-accept-losses")
        monkeypatch.setattr(sources, "get_secret", lambda name, **kw: "k")
        monkeypatch.setattr(tracking, "live_readiness",
                            lambda directory=None: {"ready": True, "criteria": []})
        assert sources._alpaca_trading_base("live") == "https://api.alpaca.markets"

    def test_wrong_phrase_refuses(self, monkeypatch):
        monkeypatch.setenv("LIVE_TRADING_ARMED", "yes")
        with pytest.raises(RuntimeError, match="not armed"):
            sources._alpaca_trading_base("live")

    def test_paper_default_untouched(self):
        assert sources._alpaca_trading_base() == "https://paper-api.alpaca.markets"


    def test_scattered_snapshots_do_not_count_as_continuity(self, tmp_path):
        """28 files with week-long gaps must NOT satisfy the continuity
        criterion (local review: count != streak)."""
        from datetime import date, timedelta
        d = date(2026, 1, 5)
        for i in range(28):
            (tmp_path / f"{d.isoformat()}.json").write_text(json.dumps({
                "date": d.isoformat(), "universe": []}))
            d += timedelta(days=9)   # every gap exceeds the 4-day allowance
        r = tracking.live_readiness(str(tmp_path))
        cont = next(c for c in r["criteria"] if "continuous" in c["name"])
        assert cont["actual"] == 1 and cont["met"] is False

    def test_weekend_gaps_keep_the_streak(self, tmp_path):
        from datetime import date, timedelta
        d = date(2026, 6, 1)
        made = 0
        while made < 30:
            if d.weekday() < 5:
                (tmp_path / f"{d.isoformat()}.json").write_text(json.dumps({
                    "date": d.isoformat(), "universe": []}))
                made += 1
            d += timedelta(days=1)
        r = tracking.live_readiness(str(tmp_path))
        cont = next(c for c in r["criteria"] if "continuous" in c["name"])
        assert cont["actual"] >= 28 and cont["met"] is True

    def test_live_context_uses_live_credentials_only(self, monkeypatch):
        """Local review (security): the live base must pair with LIVE keys."""
        monkeypatch.setenv("LIVE_TRADING_ARMED", "yes-i-accept-losses")
        monkeypatch.setattr(sources, "get_secret",
                            lambda name, **kw: f"val-{name}")
        monkeypatch.setattr(tracking, "live_readiness",
                            lambda directory=None: {"ready": True, "criteria": []})
        base, headers = sources._alpaca_trading_context("live")
        assert base == "https://api.alpaca.markets"
        assert headers["APCA-API-KEY-ID"] == "val-LIVE_ALPACA_API_KEY"
        assert headers["APCA-API-SECRET-KEY"] == "val-LIVE_ALPACA_API_SECRET"

    def test_paper_context_unchanged(self, monkeypatch):
        monkeypatch.setattr(sources, "get_secret", lambda name, **kw: f"val-{name}")
        base, headers = sources._alpaca_trading_context()
        assert base == "https://paper-api.alpaca.markets"
        assert headers["APCA-API-KEY-ID"] == "val-ALPACA_API_KEY"
