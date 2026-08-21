"""Tests for the composite rank and the default board ordering."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import market_data


class TestCompositeRank:
    def test_full_composite_math(self):
        import app
        enhanced = {"Rebound Score": 80.0,
                    "P Short": {"sort": 30.0, "miss": -4.0}}
        composite = app._composite_rank(enhanced)
        # 0.40*0.80 + 0.35*0.30 + 0.25*((-4+10)/10) = 0.32+0.105+0.15 = 0.575
        assert composite["value"] == pytest.approx(57.5, abs=0.1)
        assert composite["components"] == 3
        assert "ranking device" in composite["basis"]

    def test_missing_component_imputes_neutral_not_renormalizes(self):
        """Audit 2026-08-19: renormalizing let a row with NO probability
        evidence outrank an identically-scored row with full evidence."""
        import app
        app._composite_rank({"Rebound Score": 60.0,
                                    "P Short": {"sort": 30.0, "miss": -2.0}})
        missing = app._composite_rank({"Rebound Score": 60.0})
        # missing components count as neutral 0.5:
        # 0.40*0.60 + 0.35*0.5 + 0.25*0.5 = 0.54
        assert missing["value"] == pytest.approx(54.0, abs=0.1)
        assert missing["components"] == 1
        # and a fully-measured row with BETTER-than-neutral downside is not
        # penalized below the evidence-free row purely for having evidence
        strong = app._composite_rank({"Rebound Score": 60.0,
                                      "P Short": {"sort": 55.0, "miss": -1.0}})
        assert strong["value"] > missing["value"]

    def test_downside_is_clipped(self):
        import app
        crash = app._composite_rank({"P Short": {"sort": -1, "miss": -50.0}})
        floor = app._composite_rank({"P Short": {"sort": -1, "miss": -10.0}})
        assert crash["value"] == floor["value"]  # clipped at -10
        best = app._composite_rank({"P Short": {"sort": -1, "miss": 2.0}})
        assert best["value"] == app._composite_rank(
            {"P Short": {"sort": -1, "miss": 0.0}})["value"]  # capped at 0

    def test_nothing_measurable_is_none(self):
        import app
        assert app._composite_rank({}) is None
        assert app._composite_rank({"P Short": {"sort": -1.0}}) is None

    def test_board_sort_key_orders_composite_first(self):
        import app
        strong = {"Composite": {"value": 70.0}, "Rebound Score": 50.0, "Coverage": 0.5}
        weak = {"Composite": {"value": 40.0}, "Rebound Score": 90.0, "Coverage": 1.0}
        unranked = {"Composite": None, "Rebound Score": 95.0, "Coverage": 1.0}
        ordered = sorted([weak, unranked, strong], key=app._board_sort_key, reverse=True)
        assert ordered[0] is strong
        assert ordered[-1] is unranked  # nothing measurable ranks last

    def test_horizon_summaries_surface_ev_and_miss(self):
        import app
        pattern = [100.0]
        for _ in range(80):
            pattern.append(pattern[-1] * 0.95)
            pattern.append(pattern[-1] * 1.06)
        closes = pattern + [round(pattern[-1] * 0.95, 6)]
        market_data._cache.set("hist:ZZRK1:5y", {"ok": True, "closes": closes}, 60)
        market_data._cache.set("tech:ZZRK1", {"ok": True, "ma20": None}, 60)
        out = app._horizon_summaries("ZZRK1")
        assert "ev" in out["short"] and "miss" in out["short"]
        assert "upside" in out["short"] and out["short"]["bars"] == 7


class TestRankTemplates:
    def _source(self):
        return open(os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "app.py"), encoding="utf-8").read()

    def test_rank_column_in_both_tables(self):
        source = self._source()
        assert source.count('>Rank</th>') == 2
        assert source.count("stock['Composite'].value") >= 3  # 2 cells + card attr

    def test_rank_chip_default_active_on_both_card_groups(self):
        source = self._source()
        assert source.count('''onclick="sortCards('cards-recs', 'rank', this)">Rank''') == 1
        assert source.count('''onclick="sortCards('cards-all', 'rank', this)">Rank''') == 1
        for group in ("cards-recs", "cards-all"):
            source.split(f"sortCards('{group}'")[1 if group == "cards-recs" else 0]
        # The active chip in each sorter is the Rank chip.
        import re
        for chunk in re.findall(r'class="sort-chip active"[^>]*onclick="sortCards\([^)]*\)">(\w+)', source):
            assert chunk == "Rank"

    def test_header_indices_shifted(self):
        source = self._source()
        assert source.count("sortLoserTable(this, 8, 'num')\">Today") == 2


class TestPickCompositeRecompute:
    def test_pick_composite_reflects_fresh_score(self, monkeypatch):
        """CR outside-diff Major on PR 51: the picks pass re-scores, so the
        composite must be recomputed from the fields the row displays --
        never carried stale from the main board pass."""
        import app
        stale = {"value": 1.0, "components": 1, "basis": "stale"}
        stock = {"Symbol": "ZZPK1", "Current Price": 10.0,
                 "Composite": stale,
                 "P Short": {"sort": 30.0, "miss": -4.0}}
        fresh = {"scored": True, "score": 80.0, "recommendation": "x",
                 "recommendation_color": "green", "confidence": "High",
                 "coverage": 1.0, "factors_used": 6, "factors_total": 6,
                 "factors": [], "missing": []}
        monkeypatch.setattr(app, "score_stock", lambda *a, **k: fresh)
        monkeypatch.setattr(app, "MIN_REBOUND_SCORE", 0)
        picks = app.filter_ai_recovery_potential([stock])
        assert len(picks) == 1
        composite = picks[0]["Composite"]
        assert composite is not stale
        # 0.40*0.80 + 0.35*0.30 + 0.25*((-4+10)/10) = 0.575
        assert composite["value"] == 57.5

    def test_sorters_keep_finite_zero(self):
        """CR outside-diff Minor: a rank of exactly 0.0 must stay sortable;
        both sort functions must use Number.isFinite, not `|| -Infinity`."""
        source = open(os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "app.py"), encoding="utf-8").read()
        assert source.count("Number.isFinite") >= 3
        assert "parseFloat(b.dataset[key]) || -Infinity" not in source
        assert "?? ca?.innerText) || -Infinity" not in source
