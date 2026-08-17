"""Regression tests for the rules that keep invented data out of the app.

Every test here corresponds to a defect that actually shipped. They are written
against behaviour rather than implementation so they keep their meaning if the
internals are refactored.

No network access: these must run offline and deterministically.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from provenance import Sourced, safe_ratio, UNAVAILABLE_DISPLAY  # noqa: E402
import recommendation  # noqa: E402
import social  # noqa: E402


class TestProvenance:
    """A failed fetch must never surface as a number."""

    def test_unavailable_never_renders_a_number(self):
        target = Sourced.unavailable("yahoo:targetMeanPrice", "401 from upstream")
        assert target.format(".2f", prefix="$") == UNAVAILABLE_DISPLAY
        assert target.value is None
        assert target.ok is False

    def test_unavailable_keeps_the_reason_for_display(self):
        target = Sourced.unavailable("yahoo:targetMeanPrice", "only 1 analyst estimate")
        assert "analyst" in target.reason

    def test_live_value_renders_normally(self):
        assert Sourced.live(322.2844, "yfinance").format(".2f", prefix="$") == "$322.28"

    def test_derived_is_distinguishable_from_reported(self):
        # The UI labels derived values differently, so this flag must survive.
        assert Sourced.derived({"a": 1}, "calendar-window").is_derived is True
        assert Sourced.live(1.0, "yfinance").is_derived is False

    def test_zero_is_a_real_value_not_a_missing_one(self):
        # 0.0% short interest is a genuine reading and must not read as absent.
        assert Sourced.live(0.0, "yfinance").ok is True
        assert Sourced.live(0.0, "yfinance").format(".1f") == "0.0"


class TestSafeRatio:
    """The ZeroDivisionError that crashed institutional flow outside market hours."""

    @pytest.mark.parametrize("denominator", [0, 0.0, None])
    def test_zero_or_missing_denominator_returns_default(self, denominator):
        assert safe_ratio(5, denominator) is None
        assert safe_ratio(5, denominator, default=0) == 0

    def test_normal_division_is_unchanged(self):
        assert safe_ratio(1, 4) == 0.25


class TestMoneyParsing:
    """`.replace()` on a float broke CSV export; '$12.34' broke the AI scorer."""

    def setup_method(self):
        import app
        self.parse = app.parse_money

    @pytest.mark.parametrize("raw,expected", [
        ("$12.34", 12.34),
        ("1,234.50", 1234.50),
        ("$1,234.50", 1234.50),
        (14.075, 14.075),
        (0, 0.0),
    ])
    def test_parses_real_values(self, raw, expected):
        assert self.parse(raw) == pytest.approx(expected)

    @pytest.mark.parametrize("raw", [UNAVAILABLE_DISPLAY, "N/A", None, "", "unavailable"])
    def test_non_numeric_returns_none_never_a_default(self, raw):
        # Returning 0 here would silently become a real-looking price.
        assert self.parse(raw) is None


class TestStartupRobustness:
    def test_malformed_redis_url_does_not_crash_import(self, monkeypatch):
        """A bad REDIS_URL must degrade to the file cache, not kill the app."""
        monkeypatch.setenv("REDIS_URL", "")
        import importlib
        import app
        importlib.reload(app)
        assert app.USE_REDIS is False


class TestScoringRefusesToInvent:
    """The model must not fill absent inputs with a neutral score."""

    def test_declines_to_score_below_minimum_coverage(self):
        result = recommendation.score_rebound(current_price=10.0)
        assert result["scored"] is False
        assert result["recommendation"] == "Insufficient data"
        assert "score" not in result

    def test_missing_factors_are_reported_not_scored(self):
        result = recommendation.score_rebound(
            current_price=10.0,
            technicals={"rsi14": 25, "percent_b": 0.1, "pct_from_ma20": -0.2,
                        "volume_ratio_20d": 2.0},
            short_pct_float=0.25,
        )
        assert result["scored"] is True
        missing_keys = {m["key"] for m in result["missing"]}
        assert "analyst_upside" in missing_keys
        assert result["factors_used"] < result["factors_total"]

    def test_weights_renormalise_over_available_factors(self):
        result = recommendation.score_rebound(
            current_price=10.0,
            technicals={"rsi14": 25, "percent_b": 0.1, "pct_from_ma20": -0.2,
                        "volume_ratio_20d": 2.0},
            short_pct_float=0.25,
        )
        total = sum(f["effective_weight"] for f in result["factors"])
        assert total == pytest.approx(1.0, abs=0.01)

    def test_contribution_matches_score_times_effective_weight(self):
        result = recommendation.score_rebound(
            current_price=10.0, target_mean=20.0, analyst_count=12,
            ratings={"strongBuy": 3, "buy": 5, "hold": 2, "sell": 0,
                     "strongSell": 0, "total": 10},
            technicals={"rsi14": 30, "percent_b": 0.2, "pct_from_ma20": -0.1,
                        "volume_ratio_20d": 1.5},
        )
        for factor in result["factors"]:
            assert factor["contribution"] == pytest.approx(
                factor["score"] * factor["effective_weight"], abs=0.05)
        assert result["score"] == pytest.approx(
            sum(f["contribution"] for f in result["factors"]), abs=0.1)

    def test_absent_inputs_do_not_drag_the_score_to_neutral(self):
        """Regression: missing inputs used to score 50, pulling every result mid.

        A strong setup measured on three factors should stay strong, not be
        averaged against three imaginary average ones.
        """
        strong = {"rsi14": 20, "percent_b": 0.02, "pct_from_ma20": -0.25,
                  "volume_ratio_20d": 2.5}
        partial = recommendation.score_rebound(
            current_price=10.0, technicals=strong, short_pct_float=0.3)
        assert partial["score"] > 65, partial

    def test_thin_analyst_coverage_is_damped_not_trusted_whole(self):
        many = recommendation.score_rebound(
            current_price=10.0, target_mean=20.0, analyst_count=20,
            technicals={"rsi14": 50, "percent_b": 0.5, "pct_from_ma20": 0.0,
                        "volume_ratio_20d": 1.0},
            short_pct_float=0.05)
        few = recommendation.score_rebound(
            current_price=10.0, target_mean=20.0, analyst_count=3,
            technicals={"rsi14": 50, "percent_b": 0.5, "pct_from_ma20": 0.0,
                        "volume_ratio_20d": 1.0},
            short_pct_float=0.05)
        assert many["score"] > few["score"]

    def test_coverage_is_reported_so_scores_are_comparable(self):
        result = recommendation.score_rebound(
            current_price=10.0,
            technicals={"rsi14": 25, "percent_b": 0.1, "pct_from_ma20": -0.2,
                        "volume_ratio_20d": 2.0},
            short_pct_float=0.25)
        assert 0 < result["coverage"] <= 1
        assert result["confidence"] in {"High", "Moderate", "Low"}

    def test_methodology_is_always_returned(self):
        # The score is meaningless without the basis being stated alongside it.
        for result in (recommendation.score_rebound(current_price=10.0),
                       recommendation.score_rebound(
                           current_price=10.0,
                           technicals={"rsi14": 25, "percent_b": 0.1,
                                       "pct_from_ma20": -0.2, "volume_ratio_20d": 2.0},
                           short_pct_float=0.25)):
            assert "not investment advice" in result["methodology"]


class TestPhraseExtraction:
    """Trending phrases must come from message text, not a hard-coded list."""

    def test_repeated_phrases_are_surfaced_with_counts(self):
        messages = [
            "earnings miss really hurt this one",
            "that earnings miss was brutal",
            "earnings miss again, unbelievable",
        ]
        phrases = social._phrases(messages)
        assert phrases
        assert phrases[0]["phrase"] == "earnings miss"
        assert phrases[0]["count"] == 3

    def test_single_occurrences_are_not_trending(self):
        assert social._phrases(["a completely unique sentence here"]) == []

    def test_cashtags_and_urls_are_stripped(self):
        tokens = social._clean_tokens("$AAPL going up see https://x.com/foo now")
        assert not any(t.startswith("aapl") for t in tokens)
        assert "https" not in tokens

    def test_subphrases_of_a_chosen_phrase_are_not_duplicated(self):
        messages = ["guidance cut again"] * 3
        phrases = social._phrases(messages)
        chosen = [p["phrase"] for p in phrases]
        # "guidance cut again" and "guidance cut" must not both appear.
        assert not any(a != b and a in b for a in chosen for b in chosen)


class TestCacheLifetimes:
    """Caching decides what the small instance can afford."""

    def setup_method(self):
        import market_data
        self.md = market_data

    def test_structural_absences_are_held_longer_than_outages(self):
        assert self.md._is_structural("no listed options") is True
        assert self.md._is_structural("no analyst coverage published") is True
        assert self.md._is_structural("HTTPError") is False
        assert self.md._is_structural("fed calendar unreachable (Timeout)") is False

    def test_ttl_is_never_degenerate(self):
        for base in (60, 300, 3600, 86400):
            assert self.md._effective_ttl(base) >= 30

    def test_closed_market_extends_lifetime(self, monkeypatch):
        monkeypatch.setattr(self.md, "market_is_open", lambda: True)
        open_ttl = self.md._effective_ttl(300)
        monkeypatch.setattr(self.md, "market_is_open", lambda: False)
        closed_ttl = self.md._effective_ttl(300)
        assert closed_ttl > open_ttl


class TestSecretHandling:
    """Credentials come from the environment or the Keychain, never from code."""

    def setup_method(self):
        import secrets_store
        self.store = secrets_store
        self.store._resolved.clear()

    def test_environment_takes_precedence_over_keychain(self, monkeypatch):
        # Deployment and CI must stay authoritative; a stale Keychain entry on a
        # laptop cannot be allowed to silently override them.
        monkeypatch.setenv("FRED_API_KEY", "from-environment")
        assert self.store.get("FRED_API_KEY") == "from-environment"

    def test_missing_credential_returns_none_not_a_placeholder(self, monkeypatch):
        monkeypatch.delenv("DEFINITELY_NOT_A_REAL_CREDENTIAL", raising=False)
        assert self.store.get("DEFINITELY_NOT_A_REAL_CREDENTIAL") is None

    def test_status_reports_presence_without_revealing_values(self, monkeypatch):
        monkeypatch.setenv("FRED_API_KEY", "sensitive-value")
        report = self.store.status(["FRED_API_KEY"])
        assert report == {"FRED_API_KEY": True}
        assert "sensitive-value" not in str(report)

    def test_no_credential_is_hardcoded_in_source(self):
        """Guard against a key being pasted into a module during debugging."""
        import pathlib
        import re
        root = pathlib.Path(__file__).resolve().parent.parent
        pattern = re.compile(r"(api_key|secret|token|password)\s*=\s*['\"][A-Za-z0-9_\-]{20,}['\"]",
                             re.IGNORECASE)
        offenders = [
            path.name for path in root.glob("*.py")
            if pattern.search(path.read_text(encoding="utf-8"))
        ]
        assert offenders == [], f"hardcoded credential in {offenders}"
