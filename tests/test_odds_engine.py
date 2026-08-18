"""Tests for the conditioned odds engine: post-drop masks, intraday-high
touch, the evidence ladder, RSI conditioning, cohort shrinkage, the modest
rung, and the earnings-in-window flag."""

import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import market_data
import timeframes


class TestDayDropMask:
    def test_marks_hard_down_days_only(self):
        closes = np.array([100.0, 95.0, 94.5, 90.0, 91.0])  # -5%, -0.5%, -4.8%, +1.1%
        mask = timeframes.day_drop_mask(closes, min_drop_pct=4.0)
        assert mask.tolist() == [False, True, False, True, False]

    def test_short_series_all_false(self):
        assert timeframes.day_drop_mask(np.array([100.0])).tolist() == [False]

    def test_zero_prior_close_does_not_crash(self):
        mask = timeframes.day_drop_mask(np.array([0.0, 50.0, 47.0]))
        assert mask[1] == False  # noqa: E712 -- undefined return treated as no-drop
        assert mask[2] == True   # noqa: E712


class TestOversoldMask:
    def test_flags_low_rsi_after_decline(self):
        # Fourteen up days then a long slide: RSI must end oversold.
        closes = np.concatenate([np.linspace(100, 110, 15), np.linspace(109, 70, 25)])
        mask = timeframes.oversold_mask(closes)
        assert mask[-1] == True   # noqa: E712
        assert mask[:14].sum() == 0  # warm-up NaNs are never oversold

    def test_rsi_series_bounds(self):
        closes = np.concatenate([np.linspace(100, 110, 20), np.linspace(110, 90, 20)])
        rsi = timeframes.rsi_series(closes)
        valid = rsi[~np.isnan(rsi)]
        assert ((valid >= 0) & (valid <= 100)).all()


class TestIntradayTouch:
    def _flat_closes(self, n=80):
        return np.full(n, 100.0)

    def test_high_spike_counts_with_highs_only(self):
        closes = self._flat_closes()
        highs = closes.copy()
        highs[10] = 106.0  # intraday spike through +5%, faded by the close
        by_close = timeframes.hit_rate(closes, 5.0, 7)
        by_high = timeframes.hit_rate(closes, 5.0, 7, highs=highs)
        assert by_close["hits"] == 0
        assert by_high["hits"] > 0
        assert by_high["touch_basis"] == "intraday-high"
        assert by_close["touch_basis"] == "close"

    def test_misaligned_highs_are_ignored(self):
        closes = self._flat_closes()
        result = timeframes.hit_rate(closes, 5.0, 7, highs=np.array([1.0, 2.0]))
        assert result["touch_basis"] == "close"


class TestEvidenceLadder:
    def test_prefers_first_adequate_basis(self):
        # Conditioned basis too thin (no drop days) -> falls to unconditional.
        closes = np.full(80, 100.0)
        bases = [
            {"closes": closes, "mask": timeframes.day_drop_mask(closes),
             "min_windows": timeframes.MIN_WINDOWS_CONDITIONAL, "label": "post-drop"},
            {"closes": closes, "label": "all windows"},
        ]
        measured = timeframes.best_hit_rate(bases, 5.0, 7)
        assert measured["conditioning"] == "all windows"

    def test_conditioned_basis_wins_when_adequate(self):
        # Alternate -5% / +5.3% days: every other day is a drop day, so the
        # conditioned rung has plenty of windows -- and post-drop days recover.
        pattern = [100.0]
        for _ in range(60):
            pattern.append(pattern[-1] * 0.95)
            pattern.append(pattern[-1] * 1.053)
        closes = np.array(pattern)
        bases = [
            {"closes": closes, "mask": timeframes.day_drop_mask(closes),
             "min_windows": timeframes.MIN_WINDOWS_CONDITIONAL, "label": "post-drop"},
            {"closes": closes, "label": "all windows"},
        ]
        measured = timeframes.best_hit_rate(bases, 5.0, 7)
        assert measured["conditioning"] == "post-drop"
        assert measured["probability"] > 0.9  # every post-drop day bounced

    def test_no_adequate_basis_is_none(self):
        assert timeframes.best_hit_rate(
            [{"closes": np.full(10, 100.0), "label": "all windows"}], 5.0, 7) is None


class TestShrinkage:
    def test_thin_sample_moves_toward_prior(self):
        # 3/41 raw = 7.3%; shrunk toward a 20% cohort with m=20: 7/61 = 11.5%
        assert timeframes.shrink_toward(3, 41, 0.20) == pytest.approx(7.0 / 61.0)

    def test_large_sample_barely_moves(self):
        raw = 400 / 1000
        shrunk = timeframes.shrink_toward(400, 1000, 0.10)
        assert abs(shrunk - raw) < 0.01

    def test_annotate_reports_raw_beside_shrunk(self):
        closes = np.full(80, 100.0)
        highs = closes.copy()
        highs[5] = 106.0  # a single touch: thin evidence, shrinkage will bite
        targets = {"t1": {"upside_percent": 5.0, "target_price": 105.0}}
        annotated = timeframes.annotate_targets(
            closes, targets, "short",
            bases=[{"closes": closes, "highs": highs, "label": "all windows"}],
            cohort_prior=0.30)
        entry = annotated["t1"]
        assert entry["probability_available"]
        assert "probability_raw" in entry
        assert "shrunk toward cohort 30%" in entry["evidence"]
        assert entry["probability"] != entry["probability_raw"]

    def test_no_prior_means_no_shrinkage(self):
        closes = np.full(80, 100.0)
        targets = {"t1": {"upside_percent": 5.0, "target_price": 105.0}}
        annotated = timeframes.annotate_targets(closes, targets, "short")
        assert "probability_raw" not in annotated["t1"]


class TestAnnotateLadderLabels:
    def test_evidence_names_conditioning_and_basis(self):
        pattern = [100.0]
        for _ in range(60):
            pattern.append(pattern[-1] * 0.95)
            pattern.append(pattern[-1] * 1.053)
        closes = np.array(pattern)
        highs = closes * 1.001
        bases = [{"closes": closes, "highs": highs,
                  "mask": timeframes.day_drop_mask(closes),
                  "min_windows": timeframes.MIN_WINDOWS_CONDITIONAL,
                  "label": "post-drop (≥4% down day)"}]
        annotated = timeframes.annotate_targets(
            closes, {"t1": {"upside_percent": 5.0}}, "short", bases=bases)
        evidence = annotated["t1"]["evidence"]
        assert "post-drop" in evidence
        assert "intraday-touch" in evidence


class TestCohortPrior:
    def test_prior_averages_cached_universe(self, monkeypatch):
        import app
        pattern = [100.0]
        for _ in range(80):
            pattern.append(pattern[-1] * 0.97)
            pattern.append(pattern[-1] * 1.06)
        closes = [round(c, 4) for c in pattern]
        for sym in ("ZZCP1", "ZZCP2", "ZZCP3", "ZZCP4", "ZZCP5"):
            market_data._cache.set(f"hist:{sym}:5y", {"ok": True, "closes": closes}, 60)
        monkeypatch.setattr(market_data, "_symbol_source",
                            [lambda: ["ZZCP1", "ZZCP2", "ZZCP3", "ZZCP4", "ZZCP5"]])
        market_data._cache._local.pop("cohort:hit:short:5", None)
        prior = app._cohort_prior("short", 5.0)
        assert prior is not None and 0.0 < prior <= 1.0

    def test_too_few_symbols_no_prior(self, monkeypatch):
        import app
        monkeypatch.setattr(market_data, "_symbol_source", [lambda: ["ZZCPX"]])
        market_data._cache._local.pop("cohort:hit:medium:10", None)
        assert app._cohort_prior("medium", 10.0) is None


class TestEarningsWindow:
    """Mocks follow earnings_date's real contract: date, through, upcoming."""

    @staticmethod
    def _mock(monkeypatch, value):
        monkeypatch.setattr(market_data, "earnings_date",
                            lambda s: market_data.Sourced.live(value, "yfinance:calendar"))

    def test_confirmed_date_inside_window_flagged(self, monkeypatch):
        import app
        import tracking
        from datetime import timedelta
        soon = (tracking.trading_date_today() + timedelta(days=4)).isoformat()
        self._mock(monkeypatch, {"date": soon, "through": None,
                                 "upcoming": True, "confirmed": True})
        assert app._earnings_in_window("ZZE1", 10) == soon
        assert app._earnings_in_window("ZZE1", 2) is None

    def test_estimated_range_overlapping_window_flagged(self, monkeypatch):
        import app
        import tracking
        from datetime import timedelta
        today = tracking.trading_date_today()
        start = (today + timedelta(days=8)).isoformat()
        through = (today + timedelta(days=14)).isoformat()
        self._mock(monkeypatch, {"date": start, "through": through,
                                 "upcoming": True, "confirmed": False})
        # A 10-day horizon overlaps the front of the range.
        flagged = app._earnings_in_window("ZZE4", 10)
        assert flagged == f"{start} to {through} (est.)"
        # A 5-day horizon ends before the range begins.
        assert app._earnings_in_window("ZZE4", 5) is None

    def test_past_earnings_not_flagged(self, monkeypatch):
        import app
        import tracking
        from datetime import timedelta
        past = (tracking.trading_date_today() - timedelta(days=3)).isoformat()
        self._mock(monkeypatch, {"date": past, "through": None,
                                 "upcoming": False, "confirmed": True})
        assert app._earnings_in_window("ZZE2", 30) is None

    def test_unavailable_earnings_is_none(self, monkeypatch):
        import app
        monkeypatch.setattr(market_data, "earnings_date",
                            lambda s: market_data.Sourced.unavailable(
                                "yfinance:calendar", "no earnings date published"))
        assert app._earnings_in_window("ZZE3", 30) is None

    def test_live_contract_has_the_fields_this_reads(self, monkeypatch):
        """Drift guard: _earnings_in_window consumes date/through/upcoming,
        which must be exactly what earnings_date's payload constructor emits."""
        import inspect
        source = inspect.getsource(market_data.earnings_date)
        for field in ('"date"', '"through"', '"upcoming"'):
            assert field in source


class TestModestRung:
    def test_injected_into_short_band(self, monkeypatch):
        import app
        closes = list(np.concatenate([np.linspace(100, 110, 30),
                                      np.linspace(110, 80, 60)]))
        monkeypatch.setattr(market_data, "price_history",
                            lambda s, **kw: market_data.Sourced.live(closes, "yfinance:history"))
        monkeypatch.setattr(market_data, "ohlcv_history",
                            lambda s, **kw: market_data.Sourced.unavailable("x", "none"))
        monkeypatch.setattr(market_data, "earnings_date",
                            lambda s: market_data.Sourced.unavailable("x", "none"))
        monkeypatch.setattr(app, "_cohort_prior", lambda band, upside: None)
        result = app._attach_empirical_probabilities(
            "ZZMR1", {"timeframe_predictions": {"short_term": {}}})
        short = result["timeframe_predictions"]["short_term"]
        assert "modest_bounce" in short
        assert short["modest_bounce"]["upside_percent"] == 5.0
        assert short["modest_bounce"]["target_price"] == pytest.approx(
            closes[-1] * 1.05, rel=1e-3)

    def test_regression_unconditional_path_still_works(self, monkeypatch):
        """The original five-year close-basis behaviour survives as the last rung."""
        import app
        closes = list(np.concatenate([np.linspace(100, 130, 700),
                                      np.linspace(130, 100, 700)]))
        monkeypatch.setattr(market_data, "price_history",
                            lambda s, **kw: market_data.Sourced.live(closes, "yfinance:history"))
        monkeypatch.setattr(market_data, "ohlcv_history",
                            lambda s, **kw: market_data.Sourced.unavailable("x", "none"))
        monkeypatch.setattr(market_data, "earnings_date",
                            lambda s: market_data.Sourced.unavailable("x", "none"))
        monkeypatch.setattr(app, "_cohort_prior", lambda band, upside: None)
        result = app._attach_empirical_probabilities(
            "ZZMR2", {"timeframe_predictions": {
                "short_term": {"t1": {"upside_percent": 3.0, "target_price": 103.0}}}})
        entry = result["timeframe_predictions"]["short_term"]["t1"]
        assert entry["probability_available"]
        assert entry["windows"] >= timeframes.MIN_WINDOWS
