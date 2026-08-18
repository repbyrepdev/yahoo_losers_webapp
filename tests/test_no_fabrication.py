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
        """Patches market_phase, which _effective_ttl actually consults.

        An earlier version patched market_is_open, which the implementation
        no longer reads -- the test silently depended on the real wall clock
        and jitter, passing or failing by time of day.
        """
        monkeypatch.setattr(self.md, "market_phase",
                            lambda: {"phase": "open", "changes_at": None})
        open_ttl = self.md._effective_ttl(300)
        monkeypatch.setattr(self.md, "market_phase",
                            lambda: {"phase": "closed", "changes_at": None})
        closed_ttl = self.md._effective_ttl(300)
        monkeypatch.setattr(self.md, "market_phase",
                            lambda: {"phase": "pre_market", "changes_at": None})
        extended_ttl = self.md._effective_ttl(300)
        assert closed_ttl > extended_ttl > open_ttl


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

    def test_lowercase_env_var_still_resolves(self, monkeypatch):
        """A dashboard entry of `fred_api_key` must not silently do nothing.

        Environment variables are case-sensitive, so a casing mismatch would
        otherwise leave the feature reporting itself unconfigured with no
        obvious cause.
        """
        monkeypatch.delenv("FRED_API_KEY", raising=False)
        monkeypatch.setenv("fred_api_key", "lowercase-value")
        assert self.store.get("FRED_API_KEY") == "lowercase-value"

    def test_exact_case_wins_over_a_variant(self, monkeypatch):
        monkeypatch.setenv("FRED_API_KEY", "canonical")
        monkeypatch.setenv("fred_api_key", "variant")
        assert self.store.get("FRED_API_KEY") == "canonical"


class TestEmpiricalProbabilities:
    """Target probabilities must be measured, not assumed.

    The previous implementation started at a hard-coded 70, applied fixed
    adjustments, multiplied by a signal factor and capped at 95 -- so every
    target on a page displayed an identical 95%.
    """

    def setup_method(self):
        import numpy as np
        import timeframes
        self.np = np
        self.tf = timeframes

    def _series(self, pattern, repeats=40):
        return self.np.array(pattern * repeats, dtype=float)

    def test_probability_reflects_actual_frequency(self):
        # A series that reliably rises 10% within a few bars should report a
        # high hit rate for a 5% target.
        closes = self._series([100.0, 104.0, 108.0, 112.0, 100.0])
        result = self.tf.hit_rate(closes, target_pct=5.0, horizon_bars=4)
        assert result is not None
        assert result["probability"] > 0.5
        assert result["windows"] >= self.tf.MIN_WINDOWS

    def test_unreachable_target_reports_near_zero(self):
        closes = self._series([100.0, 100.5, 101.0, 100.2])
        result = self.tf.hit_rate(closes, target_pct=50.0, horizon_bars=4)
        assert result is not None
        assert result["probability"] == 0.0
        assert result["hits"] == 0

    def test_harder_targets_are_never_more_likely(self):
        closes = self._series([100.0, 103.0, 107.0, 99.0, 101.0])
        easy = self.tf.hit_rate(closes, target_pct=2.0, horizon_bars=5)
        hard = self.tf.hit_rate(closes, target_pct=15.0, horizon_bars=5)
        assert easy["probability"] >= hard["probability"]

    def test_every_result_carries_its_denominator(self):
        closes = self._series([100.0, 102.0, 98.0, 101.0])
        result = self.tf.hit_rate(closes, target_pct=1.0, horizon_bars=3)
        assert result["windows"] > 0
        assert result["hits"] <= result["windows"]

    def test_insufficient_history_returns_none_not_a_guess(self):
        assert self.tf.hit_rate(self.np.array([100.0, 101.0]), 5.0, 5) is None

    def test_target_at_or_below_current_price_is_not_a_forecast(self):
        closes = self._series([100.0, 101.0, 99.0])
        sourced = self.tf.target_probability(closes, target_pct=0.0, band="short")
        assert sourced.ok is False
        assert "at or below" in sourced.reason

    def test_annotated_targets_never_carry_an_unmeasured_probability(self):
        closes = self._series([100.0, 103.0, 99.0])
        targets = {"a": {"upside_percent": 2.0}, "b": {"upside_percent": None}}
        out = self.tf.annotate_targets(closes, targets, "short")
        assert out["a"]["probability_available"] is True
        assert out["b"]["probability_available"] is False
        assert "probability" not in out["b"]


class TestCacheRecovery:
    """A bad provider state must not be able to outlive a refresh."""

    def setup_method(self):
        import market_data
        self.md = market_data
        self.md._cache._local.clear()

    def test_failures_are_not_persisted_to_disk(self, tmp_path, monkeypatch):
        """A rate-limit entry written to disk would survive a restart.

        That is the opposite of what a restart is for: it would restore the
        very state the restart was meant to clear.
        """
        import json
        cache_file = tmp_path / "cache.json"
        monkeypatch.setattr(self.md, "CACHE_FILE", str(cache_file))
        self.md._cache.set("info:GOOD", {"ok": True, "target_mean": 1.0}, 600)
        self.md._cache.set("info:BAD", {"ok": False, "reason": "YFRateLimitError"}, 600)
        self.md.save_cache_to_disk()

        stored = json.loads(cache_file.read_text())
        assert "info:GOOD" in stored
        assert "info:BAD" not in stored

    def test_clear_cache_empties_everything(self, tmp_path, monkeypatch):
        monkeypatch.setattr(self.md, "CACHE_FILE", str(tmp_path / "cache.json"))
        self.md._cache.set("info:X", {"ok": True}, 600)
        assert self.md.clear_cache() >= 1
        assert self.md._cache.get("info:X") is None

    def test_rate_limit_backs_off_longer_than_a_transient_error(self):
        assert self.md.TTL_RATE_LIMITED > self.md.TTL_NEGATIVE_TRANSIENT

    def test_allow_fetch_false_never_hits_the_network(self, monkeypatch):
        """The per-render budget depends on this being honoured."""
        def explode():
            raise AssertionError("producer must not run when allow_fetch is False")
        result = self.md._cached("info:NEVER", 600, explode, allow_fetch=False)
        assert result["ok"] is False
        assert "not fetched" in result["reason"]


class TestRenderPathMakesNoProviderCalls:
    """Rendering must never call a provider.

    Render's platform issues HEAD / as a health check on a schedule. With
    fetching in the request path, each check triggered a full refresh -- the
    logs showed single requests taking 31, 45 and 54 seconds while Yahoo's
    limiter engaged. Fetching belongs on the background warmer.
    """

    def test_accessors_default_to_cache_only_when_asked(self):
        import market_data
        market_data._cache._local.clear()

        def explode():
            raise AssertionError("no fetch may occur on the render path")

        for key in ("info:X", "tech:X", "hist:X:5y", "options:X", "recs:X"):
            result = market_data._cached(key, 600, explode, allow_fetch=False)
            assert result["ok"] is False

    def test_request_warm_queues_without_fetching(self):
        import market_data
        market_data._cache._local.clear()
        with market_data._warm_queue_lock:
            market_data._warm_queue.clear()
        market_data.request_warm(["AAA", "BBB"])
        with market_data._warm_queue_lock:
            assert "AAA" in market_data._warm_queue
            assert "BBB" in market_data._warm_queue

    def test_warmer_starts_once_per_process(self, monkeypatch):
        """Regression: the warmer must start in a worker, not at import.

        gunicorn runs with preload_app, so import happens in the master and
        threads are not inherited across fork. Starting at import left every
        traffic-serving worker without a warmer, and the provider cache stayed
        empty indefinitely.
        """
        import market_data
        monkeypatch.setattr(market_data, "_warmer_started", False)
        started = []
        monkeypatch.setattr(market_data.threading, "Thread",
                            lambda *a, **k: type("T", (), {"start": lambda self: started.append(1)})())
        assert market_data.start_background_warmer() is True
        assert market_data.start_background_warmer() is False
        assert len(started) == 1

    def test_warmer_can_source_symbols_without_a_request(self):
        """The queue must not depend on a page render having happened."""
        import market_data
        market_data.set_symbol_source(lambda: ["ZZA", "ZZB"])
        assert market_data._symbol_source[0]() == ["ZZA", "ZZB"]


class TestHealthEndpoint:
    """A cold cache is a normal startup state, not a failure."""

    def setup_method(self):
        import app
        self.client = app.app.test_client()
        self.app = app

    def test_healthy_with_an_empty_cache(self, monkeypatch):
        """Regression: requiring a warm cache made every deploy hang.

        Render marks an instance live only once its health check passes. With
        cache presence in the health condition, a new instance answered 503
        until something populated it -- which nothing did, because it never
        received traffic.
        """
        monkeypatch.setattr(self.app, "get_cache_status", lambda: {"exists": False})
        response = self.client.get("/health")
        assert response.status_code == 200
        assert response.get_json()["status"] == "healthy"

    def test_cache_state_is_still_reported(self, monkeypatch):
        monkeypatch.setattr(self.app, "get_cache_status", lambda: {"exists": False})
        assert self.client.get("/health").get_json()["cache"]["status"] == "unavailable"


class TestRecentIPOs:
    """A young stock gets measured where possible, honesty where not."""

    def setup_method(self):
        import numpy as np
        import timeframes
        self.np = np
        self.tf = timeframes

    def test_three_months_of_history_measures_the_short_horizon(self):
        # 66 bars, FRVO's shape on 2026-08-17. The old floor of 60 windows made
        # it miss the 7-day horizon by exactly one window and show nothing.
        closes = self.np.linspace(20, 18, 66)
        sourced = self.tf.target_probability(closes, 8.0, "short")
        assert sourced.ok, sourced.reason

    def test_unmeasurable_horizon_names_the_shortfall(self):
        closes = self.np.linspace(20, 18, 66)
        sourced = self.tf.target_probability(closes, 80.0, "long")
        assert not sourced.ok
        assert "66 trading days" in sourced.reason
        assert "166" in sourced.reason

    def test_stale_invented_probability_is_removed_when_unavailable(self):
        """The predictor's capped 95% must not survive next to unavailable."""
        closes = self.np.linspace(20, 18, 30)  # too short to measure anything
        targets = {"t": {"upside_percent": 10.0, "probability": 95.0,
                         "confidence": "Very High"}}
        out = self.tf.annotate_targets(closes, targets, "short")
        assert out["t"]["probability_available"] is False
        assert "probability" not in out["t"]
        assert "confidence" not in out["t"]


class TestInlineJavaScriptScope:
    """Identifiers used in the page's JS must be defined where they're used.

    Two shipped crashes motivate this: probabilityBadge(target) inside loops
    whose variable was named prediction, and ${targetsBreakdown} inside
    functions that only defined mediumTargetsBreakdown. Each killed an entire
    tab render inside its catch handler, so the page showed a blank tab with
    no console error.
    """

    def _functions(self):
        import re
        import app as app_module
        import inspect
        src = inspect.getsource(app_module)
        # Split the inline script into function bodies by declaration.
        spans = [(m.start(), m.group(1)) for m in
                 re.finditer(r'function\s+(\w+)\s*\(', src)]
        out = {}
        for i, (start, name) in enumerate(spans):
            end = spans[i + 1][0] if i + 1 < len(spans) else len(src)
            out[name] = src[start:end]
        return out

    def test_template_identifiers_resolve(self):
        import re
        functions = self._functions()
        top_level = set(functions)  # function declarations are global
        # Identifiers this codebase introduced and renders via ${...}.
        watched = {"targetsBreakdown", "mediumTargetsBreakdown",
                   "longTermTargetsBreakdown", "heuristicNote",
                   "shortTermFinalScoreSummary", "mediumFinalScoreSummary",
                   "longTermFinalScoreSummary", "probabilityBadge",
                   "socialDisplay"}
        failures = []
        for fn_name, body in functions.items():
            declared = set(re.findall(r'(?:const|let|var)\s+(\w+)', body))
            declared |= set(re.findall(r'forEach\(\(\[?\s*(\w+)(?:,\s*(\w+))?\]?',
                                       body) and
                            [x for pair in re.findall(
                                r'forEach\(\(\[?\s*(\w+)(?:,\s*(\w+))?\]?', body)
                             for x in pair if x])
            used = set(re.findall(r'\$\{(\w+)[\.\(\}]', body))
            used |= set(re.findall(r'\$\{(\w+)\s', body))
            for ident in used & watched:
                if ident not in declared and ident not in top_level:
                    failures.append(f"{ident} used in {fn_name} but not defined there")
        assert failures == [], failures


class TestOhlcvRoundTrip:
    """Cached OHLCV must survive serialisation across a DST boundary."""

    def test_mixed_offset_timestamps_rebuild(self, monkeypatch):
        """A year of exchange timestamps mixes -04:00 and -05:00 offsets.

        pd.to_datetime refuses to merge those without utc=True, and that
        ValueError previously escaped as a 500 from the analysis route.
        """
        import market_data
        payload = {
            "ok": True,
            "index": ["2025-11-01T00:00:00-04:00", "2025-12-01T00:00:00-05:00",
                      "2026-03-15T00:00:00-04:00"],
            "open": [1.0, 2.0, 3.0], "high": [1.0, 2.0, 3.0],
            "low": [1.0, 2.0, 3.0], "close": [1.0, 2.0, 3.0],
            "volume": [10, 20, 30],
        }
        market_data._cache.set("ohlcv:TZX:1y", payload, 600)
        frame = market_data.ohlcv_frame("TZX")
        assert frame is not None
        assert len(frame) == 3

    def test_malformed_payload_degrades_to_none(self):
        import market_data
        market_data._cache.set("ohlcv:BADX:1y", {"ok": True, "index": ["nonsense"],
                                                 "open": [1], "high": [1], "low": [1],
                                                 "close": [1], "volume": [1]}, 600)
        assert market_data.ohlcv_frame("BADX") is None


class TestTableSummaries:
    """The table's summary columns come from cache and sort by conviction."""

    def test_rows_sort_scored_first_then_by_score(self, monkeypatch):
        import app
        monkeypatch.setattr(app, "calculate_all_investment_analysis",
                            lambda l, d: [
                                {'Symbol': 'LOW', 'Name': 'x', 'Current Price': 5.0,
                                 'Change Today': '-1', 'Percent Change Today': '-1%'},
                                {'Symbol': 'NONE', 'Name': 'x', 'Current Price': 5.0,
                                 'Change Today': '-1', 'Percent Change Today': '-1%'},
                                {'Symbol': 'HIGH', 'Name': 'x', 'Current Price': 5.0,
                                 'Change Today': '-1', 'Percent Change Today': '-1%'},
                            ])
        def fake_score(symbol, price=None, full=False):
            return {'LOW': {'scored': True, 'score': 41.0, 'recommendation': 'x',
                            'recommendation_color': '', 'confidence': 'Low',
                            'coverage': 0.5, 'factors_used': 3, 'factors_total': 6},
                    'NONE': {'scored': False, 'reason': 'thin', 'coverage': 0.2,
                             'recommendation': 'Insufficient data',
                             'recommendation_color': ''},
                    'HIGH': {'scored': True, 'score': 88.0, 'recommendation': 'x',
                             'recommendation_color': '', 'confidence': 'High',
                             'coverage': 1.0, 'factors_used': 6, 'factors_total': 6}}[symbol]
        monkeypatch.setattr(app, "score_stock", fake_score)
        out = app.calculate_enhanced_investment_analysis([], [])
        assert [r['Symbol'] for r in out] == ['HIGH', 'LOW', 'NONE']
        for row in out:
            for key in ('P Short', 'P Medium', 'P Long'):
                assert 'display' in row[key] and 'sort' in row[key]

    def test_horizon_summary_unavailable_without_cached_history(self):
        import app
        out = app._horizon_summaries("NOCACHEXYZ", target_price=10.0)
        for band in out.values():
            assert band['display'] == '—'
            assert band['sort'] == -1.0


class TestWilsonIntervals:
    """Every hit rate carries a confidence interval, so thin samples show it."""

    def test_thin_sample_is_visibly_wide(self):
        import timeframes
        low, high = timeframes.wilson_interval(20, 59)
        assert high - low > 0.2          # ±10+ points on 59 windows
        low2, high2 = timeframes.wilson_interval(1004, 1233)
        assert high2 - low2 < 0.05       # fat sample, tight interval

    def test_bounds_stay_in_range(self):
        import timeframes
        for hits, windows in ((0, 50), (50, 50), (1, 1233)):
            low, high = timeframes.wilson_interval(hits, windows)
            assert 0.0 <= low <= high <= 1.0

    def test_zero_windows_does_not_divide(self):
        import timeframes
        assert timeframes.wilson_interval(0, 0) == (0.0, 0.0)

    def test_annotated_targets_carry_the_interval(self):
        import numpy as np
        import timeframes
        closes = np.array([100.0, 103.0, 99.0, 101.0] * 40)
        out = timeframes.annotate_targets(closes, {"t": {"upside_percent": 2.0}}, "short")
        assert "ci_low" in out["t"] and "ci_high" in out["t"]
        assert out["t"]["ci_low"] <= out["t"]["probability"] <= out["t"]["ci_high"]


class TestFallReason:
    """The why-it-fell label comes from real headline text, display-only."""

    def setup_method(self):
        import app
        self.classify = app.classify_fall_reason

    def test_earnings_language_classifies(self):
        out = self.classify([{"title": "Acme misses Q2 estimates, shares slide"}])
        assert out["label"] == "Earnings miss"
        assert out["matched"]

    def test_offering_language_classifies(self):
        out = self.classify([{"title": "Acme announces $200M secondary offering"}])
        assert out["label"] == "Dilution / offering"

    def test_no_headlines_says_so(self):
        out = self.classify([])
        assert out["label"] == "No headlines"

    def test_unmatched_text_is_unclassified_not_guessed(self):
        out = self.classify([{"title": "Acme appoints new head of design"}])
        assert out["label"] == "Unclassified"

    def test_label_carries_its_basis(self):
        out = self.classify([{"title": "Analyst downgrades Acme to underweight"}])
        assert "display-only" in out["basis"]


class TestTrackRecord:
    """Forward returns come only from prices the record actually contains."""

    def _write(self, tmp_path, day, universe, tracked=None):
        import json
        snap = {"date": day, "model_version": "test", "universe": universe,
                "tracked_prices": tracked or {}}
        (tmp_path / f"{day}.json").write_text(json.dumps(snap))

    def test_pick_joins_with_later_recorded_price(self, tmp_path):
        import tracking
        self._write(tmp_path, "2026-08-01",
                    [{"symbol": "AAA", "price": 10.0, "score": 80.0},
                     {"symbol": "BBB", "price": 20.0, "score": 40.0}])
        self._write(tmp_path, "2026-08-08", [], tracked={"AAA": 11.0, "BBB": 19.0})
        record = tracking.compute_track_record(str(tmp_path))
        seven = record["horizons"]["7"]
        assert seven["n_picks"] == 1
        assert seven["picks_mean"] == 10.0          # +10% on AAA
        assert seven["baseline_mean"] == -5.0       # -5% on BBB
        assert seven["excess"] == 15.0

    def test_unresolved_pick_is_pending_not_dropped(self, tmp_path):
        import tracking
        self._write(tmp_path, "2026-08-01",
                    [{"symbol": "AAA", "price": 10.0, "score": 90.0}])
        record = tracking.compute_track_record(str(tmp_path))
        assert record["pending"] == 1
        assert record["horizons"]["7"].get("n_picks", 0) == 0

    def test_empty_directory_reports_honestly(self, tmp_path):
        import tracking
        record = tracking.compute_track_record(str(tmp_path))
        assert record["snapshot_days"] == 0
        assert record["picks"] == []

    def test_tracked_symbols_covers_recent_universe(self, tmp_path):
        import tracking
        from datetime import date, timedelta
        recent = (date.today() - timedelta(days=3)).isoformat()
        self._write(tmp_path, recent, [{"symbol": "CCC", "price": 5.0, "score": 75.0}])
        assert "CCC" in tracking.tracked_symbols(str(tmp_path))


class TestFinraParsing:
    """The RegSHO feed carries fractional volumes; parsing must not choke."""

    def test_fractional_volumes_parse(self, monkeypatch):
        import market_data

        class FakeResponse:
            status_code = 200
            text = ("Date|Symbol|ShortVolume|ShortExemptVolume|TotalVolume|Market\n"
                    "20260817|WING|468113.5|7|828936.2|B,Q,N\n"
                    "20260817|ZERO|100|0|0|B,Q,N\n")

        import requests as rq
        monkeypatch.setattr(rq, "get", lambda *a, **k: FakeResponse())
        market_data._cache._local.pop("finra:daily", None)
        sourced = market_data.finra_short_volume("WING")
        assert sourced.ok
        assert abs(sourced.value["short_ratio"] - 0.5647) < 0.001
        # zero-total row is skipped, not divided by
        assert not market_data.finra_short_volume("ZERO").ok


class TestTradingDates:
    """The record is keyed to the exchange clock, not the server clock."""

    def test_snapshot_date_is_the_eastern_trading_day(self, monkeypatch):
        """Regression: at 01:05 UTC (9:05 PM EDT) the snapshot of the 08-17
        session was filed as 08-18, because date.today() follows the UTC
        server clock."""
        import tracking
        from datetime import datetime, date, timezone

        class LateEvening(datetime):
            @classmethod
            def now(cls, tz=None):
                # 2026-08-18 01:05 UTC == 2026-08-17 21:05 EDT
                fixed = datetime(2026, 8, 18, 1, 5, tzinfo=timezone.utc)
                return fixed.astimezone(tz) if tz else fixed.replace(tzinfo=None)

        monkeypatch.setattr(tracking, "datetime", LateEvening)
        assert tracking.trading_date_today() == date(2026, 8, 17)
        snap = tracking.build_snapshot([], {})
        assert snap["date"] == "2026-08-17"

    def test_no_snapshot_uses_a_utc_rolled_date(self):
        """Committed snapshots must carry Eastern trading dates and no duplicates."""
        import glob, json, os
        dates = []
        for path in glob.glob(os.path.join(os.path.dirname(__file__), "..", "data", "snapshots", "*.json")):
            payload = json.load(open(path))
            name = os.path.basename(path).replace(".json", "")
            assert payload["date"] == name, f"{name} content disagrees with filename"
            dates.append(name)
        assert len(dates) == len(set(dates))


class TestExpectedValue:
    """EV combines only measured terms: hit rate, gain, median miss outcome."""

    def _series(self, pattern, repeats=40):
        import numpy as np
        return np.array(pattern * repeats, dtype=float)

    def test_ev_arithmetic_matches_its_parts(self):
        import timeframes
        closes = self._series([100.0, 104.0, 96.0, 101.0, 99.0])
        m = timeframes.hit_rate(closes, target_pct=3.0, horizon_bars=4)
        assert m["expected_value"] is not None
        p = m["probability"]
        expected = p * 3.0 + (1 - p) * m["miss_median_return"]
        assert abs(m["expected_value"] - expected) < 0.06

    def test_all_hits_means_ev_equals_the_gain(self):
        import timeframes
        closes = self._series([100.0, 110.0, 121.0, 133.0])
        m = timeframes.hit_rate(closes, target_pct=5.0, horizon_bars=3)
        if m["hits"] == m["windows"]:
            assert m["expected_value"] == 5.0

    def test_unreachable_target_ev_is_negative_when_stock_declines(self):
        import numpy as np, timeframes
        closes = np.linspace(100.0, 60.0, 200)  # steady decline
        m = timeframes.hit_rate(closes, target_pct=50.0, horizon_bars=10)
        assert m["probability"] == 0.0
        assert m["expected_value"] < 0

    def test_annotated_targets_carry_ev(self):
        import timeframes
        closes = self._series([100.0, 103.0, 99.0, 101.0])
        out = timeframes.annotate_targets(closes, {"t": {"upside_percent": 2.0}}, "short")
        assert "expected_value" in out["t"]

    def test_forward_distribution_orders_quantiles(self):
        import timeframes
        closes = self._series([100.0, 104.0, 96.0, 101.0, 99.0])
        d = timeframes.horizon_distribution(closes, 5)
        assert d["p10"] <= d["median"] <= d["p90"]
        assert d["windows"] >= timeframes.MIN_WINDOWS

    def test_short_history_has_no_distribution(self):
        import numpy as np, timeframes
        assert timeframes.horizon_distribution(np.array([100.0, 101.0]), 5) is None


class TestEdgarInsiders:
    """Insider filing counts are real, and direction is honestly absent."""

    def test_form4_counting_with_mocked_edgar(self, monkeypatch):
        import market_data, requests as rq
        from datetime import date, timedelta
        recent_day = (date.today() - timedelta(days=5)).isoformat()
        old_day = (date.today() - timedelta(days=400)).isoformat()

        class FakeResp:
            status_code = 200
            def raise_for_status(self): pass
            def __init__(self, payload): self._p = payload
            def json(self): return self._p

        def fake_get(url, **kw):
            if "company_tickers" in url:
                return FakeResp({"0": {"ticker": "TSTX", "cik_str": 1234, "title": "T"}})
            return FakeResp({"filings": {"recent": {
                "form": ["4", "10-K", "4", "4/A"],
                "filingDate": [recent_day, recent_day, old_day, recent_day]}}})

        monkeypatch.setattr(rq, "get", fake_get)
        market_data._cache._local.pop("edgar:ciks", None)
        market_data._cache._local.pop("edgar:form4:TSTX", None)
        out = market_data.insider_filings("TSTX")
        assert out.ok
        assert out.value["count"] == 2               # Form 4 + 4/A inside window
        assert out.value["latest"] == recent_day
        assert "not parsed" in out.value["note"]     # direction honestly absent

    def test_unknown_ticker_reports_reason(self, monkeypatch):
        import market_data
        market_data._cache.set("edgar:ciks", {"ok": True, "table": {"AAA": "1"}}, 600)
        out = market_data.insider_filings("NOPE")
        assert not out.ok and "not in SEC registry" in out.reason


class TestSolvency:
    """The balance-sheet label is derived, labelled derived, and sane."""

    def _with_info(self, monkeypatch, **fields):
        import market_data
        payload = {"ok": True, "total_cash": None, "total_debt": None,
                   "free_cashflow": None, "profit_margins": None}
        payload.update(fields)
        monkeypatch.setattr(market_data, "_info", lambda s, allow_fetch=True: payload)
        return market_data.solvency("X")

    def test_net_cash_positive_fcf(self, monkeypatch):
        out = self._with_info(monkeypatch, total_cash=500, total_debt=100, free_cashflow=50)
        assert out.value["label"] == "Net cash, cash-flow positive" and out.is_derived

    def test_missing_debt_never_becomes_a_zero_balance(self, monkeypatch):
        """CodeRabbit finding: (cash or 0) reported absent figures as zeros."""
        out = self._with_info(monkeypatch, total_cash=500, free_cashflow=50)
        assert out.value["net_cash"] is None
        assert "Net" not in out.value["label"]
        assert out.value["label"] == "cash-flow positive"

    def test_no_fcf_never_claims_cash_flow_posture(self, monkeypatch):
        out = self._with_info(monkeypatch, total_cash=100, total_debt=500)
        assert "cash-flow" not in out.value["label"]
        assert out.value["label"] == "Net debt"

    def test_payload_uses_documented_estimate_basis_field(self, monkeypatch):
        out = self._with_info(monkeypatch, total_cash=1, total_debt=1, free_cashflow=1)
        assert "estimate_basis" in out.value and "basis" != list(out.value)[-1]

    def test_burning_cash_with_net_debt_is_flagged_risk(self, monkeypatch):
        out = self._with_info(monkeypatch, total_cash=10, total_debt=500, free_cashflow=-50)
        assert out.value["tone"] == "risk"

    def test_no_fields_is_unavailable_not_guessed(self, monkeypatch):
        out = self._with_info(monkeypatch)
        assert not out.ok


class TestRegimeConditioning:
    """The regime rate uses only windows that began in today's VIX bucket."""

    def test_mask_restricts_the_sample(self):
        import numpy as np, timeframes
        closes = np.array([100.0, 105.0, 95.0, 102.0] * 40)
        full = timeframes.hit_rate(closes, 3.0, 3)
        mask = np.zeros(len(closes), dtype=bool)
        mask[: len(closes) // 2] = True
        half = timeframes.hit_rate(closes, 3.0, 3, mask=mask)
        assert half["windows"] < full["windows"]

    def test_bucket_boundaries(self):
        import timeframes
        assert timeframes.vix_bucket(12.0).startswith("calm")
        assert timeframes.vix_bucket(19.0).startswith("normal")
        assert timeframes.vix_bucket(31.0).startswith("stressed")

    def test_conditioned_rate_needs_alignment(self):
        import numpy as np, timeframes
        closes = np.array([100.0, 101.0] * 60)
        out = timeframes.regime_conditioned(closes, None, {"2026-01-01": 15}, 2.0, "short")
        assert out is None  # no dated closes -> no regime claim

    def test_conditioned_rate_reports_its_bucket(self):
        import numpy as np, timeframes
        n = 160
        closes = np.array([100.0, 104.0, 98.0, 101.0] * (n // 4))
        dates = [f"2026-01-{(i % 28) + 1:02d}x{i}" for i in range(n)]  # unique keys
        vix = {d: 12.0 for d in dates}
        out = timeframes.regime_conditioned(closes, dates, vix, 2.0, "short")
        assert out and out["bucket"].startswith("calm")
        assert out["windows"] >= timeframes.MIN_WINDOWS


class TestSpyRelativeRecord:
    """Pick returns are priced against SPY over the same recorded span."""

    def _write(self, tmp_path, day, universe, tracked=None):
        import json
        (tmp_path / f"{day}.json").write_text(json.dumps(
            {"date": day, "model_version": "t", "universe": universe,
             "tracked_prices": tracked or {}}))

    def test_vs_spy_excess_math(self, tmp_path):
        import tracking
        self._write(tmp_path, "2026-08-01",
                    [{"symbol": "AAA", "price": 10.0, "score": 90.0}],
                    tracked={"SPY": 100.0})
        self._write(tmp_path, "2026-08-08", [],
                    tracked={"AAA": 11.0, "SPY": 102.0})
        record = tracking.compute_track_record(str(tmp_path))
        seven = record["horizons"]["7"]
        assert seven["vs_spy_mean"] == 8.0     # +10% pick vs +2% SPY
        pick = record["picks"][0]["returns"]["7"]
        assert pick["vs_spy"] == 8.0

    def test_missing_spy_leaves_comparison_absent_not_zero(self, tmp_path):
        import tracking
        self._write(tmp_path, "2026-08-01",
                    [{"symbol": "AAA", "price": 10.0, "score": 90.0}])
        self._write(tmp_path, "2026-08-08", [], tracked={"AAA": 11.0})
        record = tracking.compute_track_record(str(tmp_path))
        assert "vs_spy_mean" not in record["horizons"]["7"]
        assert "vs_spy" not in record["picks"][0]["returns"]["7"]


class TestFredMacro:
    """Macro readings come from FRED or are absent, never guessed."""

    def test_parses_latest_numeric_observation(self, monkeypatch):
        import market_data, requests as rq

        class FakeResp:
            def raise_for_status(self): pass
            def json(self):
                return {"observations": [{"date": "2026-08-17", "value": "."},
                                          {"date": "2026-08-15", "value": "0.53"}]}
        monkeypatch.setattr(rq, "get", lambda *a, **k: FakeResp())
        monkeypatch.setattr(market_data.secrets_store if hasattr(market_data, 'secrets_store') else __import__('secrets_store'), "get", lambda n: "k")
        market_data._cache._local.pop("fred:T10Y2Y", None)
        out = market_data.fred_latest("T10Y2Y")
        assert out.ok and out.value["value"] == 0.53
        assert out.value["as_of"] == "2026-08-15"   # the dot row was skipped

    def test_no_key_is_unavailable(self, monkeypatch):
        import market_data, secrets_store
        monkeypatch.setattr(secrets_store, "get", lambda n: None)
        out = market_data.fred_latest("T10Y2Y")
        assert not out.ok and "not configured" in out.reason

    def test_render_path_fred_never_fetches(self, monkeypatch):
        import market_data, secrets_store
        # Pin a key so the test exercises the cache-only path on every
        # machine; without one the accessor short-circuits earlier ("not
        # configured"), which made this pass locally and fail in CI.
        monkeypatch.setattr(secrets_store, "get", lambda n: "test-key")
        market_data._cache._local.pop("fred:T10Y2Y", None)
        out = market_data.fred_latest("T10Y2Y", allow_fetch=False)
        assert not out.ok and "not fetched" in out.reason

    def test_malformed_observation_is_skipped_not_fatal(self, monkeypatch):
        import market_data, requests as rq, secrets_store

        class FakeResp:
            def raise_for_status(self): pass
            def json(self):
                return {"observations": [{"date": "d1", "value": "garbage"},
                                          {"date": "d2", "value": "1.25"}]}
        monkeypatch.setattr(rq, "get", lambda *a, **k: FakeResp())
        monkeypatch.setattr(secrets_store, "get", lambda n: "k")
        market_data._cache._local.pop("fred:T10Y2Y", None)
        out = market_data.fred_latest("T10Y2Y")
        assert out.ok and out.value["value"] == 1.25


class TestSessionPhases:
    """Freshness follows the real session calendar, extended hours included."""

    def _phase_at(self, monkeypatch, weekday, hour, minute):
        import market_data
        from datetime import datetime

        class Fixed(datetime):
            pass
        base = datetime(2026, 8, 10 + weekday, hour, minute)  # Mon=10th
        monkeypatch.setattr(market_data, "_eastern_now", lambda: base)
        return market_data.market_phase()

    def test_pre_market_is_its_own_phase(self, monkeypatch):
        p = self._phase_at(monkeypatch, 1, 5, 0)      # Tue 5:00 AM
        assert p["phase"] == "pre_market"
        assert p["changes_at"].hour == 9 and p["changes_at"].minute == 30

    def test_after_hours_ends_at_eight(self, monkeypatch):
        p = self._phase_at(monkeypatch, 1, 17, 30)    # Tue 5:30 PM
        assert p["phase"] == "after_hours"
        assert p["changes_at"].hour == 20

    def test_overnight_expires_at_next_pre_market(self, monkeypatch):
        p = self._phase_at(monkeypatch, 1, 21, 30)    # Tue 9:30 PM
        assert p["phase"] == "closed"
        assert p["changes_at"].hour == 4 and p["changes_at"].day == 12  # Wed

    def test_friday_night_expires_monday_pre_market(self, monkeypatch):
        p = self._phase_at(monkeypatch, 4, 22, 0)     # Fri 10:00 PM
        assert p["phase"] == "closed"
        assert p["changes_at"].weekday() == 0 and p["changes_at"].hour == 4

    def test_extended_hours_stretch_less_than_overnight(self):
        import market_data
        stretch = market_data.PHASE_TTL_STRETCH
        assert stretch["pre_market"] < stretch["closed"]
        assert stretch["after_hours"] < stretch["closed"]

    def test_page_lifetime_capped_at_phase_boundary(self, monkeypatch):
        """A 9:25 pre-market render must expire at 9:30, not 9:55."""
        import app, market_data
        from datetime import datetime
        base = datetime(2026, 8, 11, 9, 25)
        monkeypatch.setattr(market_data, "_eastern_now", lambda: base)
        policy = app.page_cache_policy()
        assert policy["phase"] == "pre_market"
        assert policy["seconds"] <= 5 * 60 + 1

    def test_closed_policy_names_the_reopen_time(self, monkeypatch):
        import app, market_data
        from datetime import datetime
        base = datetime(2026, 8, 11, 22, 0)
        monkeypatch.setattr(market_data, "_eastern_now", lambda: base)
        policy = app.page_cache_policy()
        assert "cannot change" in policy["description"]
        assert policy["seconds"] > 5 * 3600     # ~6h to 4 AM

    def test_cached_page_expiry_is_absolute_not_rederived(self, monkeypatch, tmp_path):
        """CodeRabbit finding: a 9:25 pre-market entry must not be revalidated
        under the 10-minute open-session policy after 9:30 and served stale."""
        import app, pickle, time
        from datetime import datetime
        cache_file = tmp_path / "page.pkl"
        monkeypatch.setattr(app, "CACHE_FILE", str(cache_file))
        monkeypatch.setattr(app, "USE_REDIS", False)
        # Written at 9:25 pre-market with a 5-minute lifetime...
        payload = {"expires_at": time.time() - 30, "timestamp": datetime.now(),
                   "data": {"x": 1}}
        cache_file.write_bytes(pickle.dumps(payload))
        # ...read after the boundary: expired regardless of the current phase.
        assert app.load_cache() is None

    def test_legacy_cache_without_expiry_is_rejected(self, monkeypatch, tmp_path):
        import app, pickle
        from datetime import datetime
        cache_file = tmp_path / "page.pkl"
        monkeypatch.setattr(app, "CACHE_FILE", str(cache_file))
        monkeypatch.setattr(app, "USE_REDIS", False)
        cache_file.write_bytes(pickle.dumps({"timestamp": datetime.now(), "data": {}}))
        assert app.load_cache() is None


class TestDegradedRenders:
    """A mid-warm page is honest but transient, and must not be pinned."""

    def test_low_scored_ratio_is_degraded_with_honest_note(self):
        import app
        rows = [{'Rebound Score': None}] * 20 + [{'Rebound Score': 80.0}] * 5
        degraded, note = app.degraded_state(rows)
        assert degraded and "5 of 25" in note

    def test_healthy_ratio_is_not_degraded(self):
        import app
        rows = [{'Rebound Score': 70.0}] * 20 + [{'Rebound Score': None}] * 5
        degraded, note = app.degraded_state(rows)
        assert not degraded and note is None

    def test_degraded_cache_entry_is_short_lived(self, monkeypatch, tmp_path):
        import app, pickle, time
        monkeypatch.setattr(app, "CACHE_FILE", str(tmp_path / "p.pkl"))
        monkeypatch.setattr(app, "USE_REDIS", False)
        monkeypatch.setattr(app, "page_cache_policy",
                            lambda: {"seconds": 1800, "description": "x", "phase": "pre_market"})
        app.save_cache({"degraded_note": "warming", "all_analysis": []})
        entry = pickle.loads((tmp_path / "p.pkl").read_bytes())
        assert entry["expires_at"] - time.time() <= app.DEGRADED_CACHE_SECONDS + 2

    def test_healthy_cache_entry_keeps_full_lifetime(self, monkeypatch, tmp_path):
        import app, pickle, time
        monkeypatch.setattr(app, "CACHE_FILE", str(tmp_path / "p.pkl"))
        monkeypatch.setattr(app, "USE_REDIS", False)
        monkeypatch.setattr(app, "page_cache_policy",
                            lambda: {"seconds": 1800, "description": "x", "phase": "pre_market"})
        app.save_cache({"degraded_note": None, "all_analysis": []})
        entry = pickle.loads((tmp_path / "p.pkl").read_bytes())
        assert entry["expires_at"] - time.time() > 1700


class TestInfoLane:
    """quoteSummary gets its own pacing and one shared cooldown."""

    def test_rate_limit_sets_shared_cooldown_not_per_symbol_poison(self, monkeypatch):
        import market_data, time

        class Limited:
            @property
            def info(self):
                raise RuntimeError("Too Many Requests. Rate limited. Try after a while.")
        monkeypatch.setattr(market_data, "_ticker", lambda s: Limited())
        monkeypatch.setattr(market_data, "_info_throttle", lambda: None)
        market_data._info_cooldown_until[0] = 0.0
        market_data._cache._local.pop("info:LANE1", None)
        out = market_data._info("LANE1")
        assert not out["ok"] and "cooling down" in out["reason"]
        assert market_data._info_cooldown_until[0] > time.time()
        # A second symbol during the cooldown never touches the provider.
        calls = []
        monkeypatch.setattr(market_data, "_ticker",
                            lambda s: calls.append(s) or Limited())
        market_data._cache._local.pop("info:LANE2", None)
        out2 = market_data._info("LANE2")
        assert not out2["ok"] and calls == []
        market_data._info_cooldown_until[0] = 0.0

    def test_cooldown_failures_retry_fast_not_fifteen_minutes(self, monkeypatch):
        import market_data, time
        market_data._cache._local.pop("info:LANE3", None)
        market_data._info_cooldown_until[0] = time.time() + 60
        market_data._info("LANE3")
        expires, _ = market_data._cache._local["info:LANE3"]
        assert expires - time.time() < 120        # 90s retry, not 15 min
        market_data._info_cooldown_until[0] = 0.0

    def test_info_ttl_jitter_spreads_the_herd(self, monkeypatch):
        """Entries written together must not expire together next morning."""
        import market_data
        seen = set()
        real_cached = market_data._cached
        def spy(key, ttl, producer, allow_fetch=True):
            if key.startswith("info:"):
                seen.add(ttl)
            return {"ok": False, "reason": "x"}
        monkeypatch.setattr(market_data, "_cached", spy)
        for i in range(12):
            market_data.analyst_target(f"JIT{i}")
        assert len(seen) > 6  # wide per-symbol spread, not one shared value

    def test_refusals_double_the_interval_and_successes_decay_it(self):
        import market_data
        market_data._info_interval[0] = market_data.INFO_CALL_INTERVAL_SECONDS
        market_data._info_lane_refused()
        market_data._info_lane_refused()
        assert market_data._info_interval[0] == market_data.INFO_CALL_INTERVAL_SECONDS * 4
        for _ in range(20):
            market_data._info_lane_succeeded()
        assert market_data._info_interval[0] == market_data.INFO_CALL_INTERVAL_SECONDS
        market_data._info_cooldown_until[0] = 0.0

    def test_interval_is_capped(self):
        import market_data
        market_data._info_interval[0] = market_data.INFO_CALL_INTERVAL_SECONDS
        for _ in range(20):
            market_data._info_lane_refused()
        assert market_data._info_interval[0] <= market_data.INFO_INTERVAL_MAX_SECONDS
        market_data._info_interval[0] = market_data.INFO_CALL_INTERVAL_SECONDS
        market_data._info_cooldown_until[0] = 0.0


class TestStableUniverse:
    """One list per cadence: page, warmer and refreshes share the same target."""

    class _FakeCache:
        def __init__(self): self.store = {}
        def get(self, key): return self.store.get(key)
        def set(self, key, value, ttl): self.store[key] = value

    def test_second_call_reuses_the_cached_list(self, monkeypatch):
        import app, market_data
        fake = self._FakeCache()
        monkeypatch.setattr(market_data, "_cache", fake)
        calls = []
        monkeypatch.setattr(app, "scrape_yahoo_losers",
                            lambda: (calls.append(1) or ([{'Symbol': 'AAA'}], {'success': True, 'data_source': 'live', 'message': 'scraped'})))
        first_losers, first_status = app.stable_universe()
        second_losers, second_status = app.stable_universe()
        assert len(calls) == 1
        assert first_losers == second_losers
        # The reused call says so instead of claiming a fresh scrape.
        assert second_status['data_source'] == 'cached'
        assert 'reused' in second_status['message']

    def test_failed_scrape_is_not_cached(self, monkeypatch):
        import app, market_data
        fake = self._FakeCache()
        monkeypatch.setattr(market_data, "_cache", fake)
        monkeypatch.setattr(app, "scrape_yahoo_losers",
                            lambda: ([], {'success': False}))
        app.stable_universe()
        assert fake.store.get('universe:v1') is None
