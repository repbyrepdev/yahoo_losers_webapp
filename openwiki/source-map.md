---
type: source map
title: Source Map
description: Compact engineering intent map from changes to owning files, important symbols, focused tests, and minimal validation commands.
tags: [source-map, navigation]
---

# Source Map

Use this page to jump from an engineering intent to the owning code and narrow tests. For subsystem explanations, follow the linked canonical pages.

| Intent | Primary source | Key symbols | Wiki page | Focused tests |
| --- | --- | --- | --- | --- |
| Change homepage data flow or cache behavior | `app.py` | `index`, `stable_universe`, `save_cache`, `load_cache`, `page_cache_policy`, `degraded_state` | [Dashboard and Routes](application/dashboard-and-routes.md) | `tests/test_freshness.py`, `tests/test_no_fabrication.py` |
| Add or adjust Flask routes | `app.py` | route decorators, `rate_limit`, `generate_etag`, `add_cache_headers` | [Dashboard and Routes](application/dashboard-and-routes.md) | Relevant domain test plus Flask client test if new route behavior matters |
| Change source provenance or unavailable semantics | `provenance.py`, callers in `app.py` and `market_data.py` | `Sourced`, `UNAVAILABLE_DISPLAY`, `safe_ratio`, `redact_secrets` | [Data Provenance and No-Fabrication Contract](data/provenance-and-honesty.md) | `tests/test_no_fabrication.py` |
| Modify yfinance cache producers | `market_data.py` | `_cached`, `_info`, `analyst_target`, `profile`, `technicals`, `options_flow`, `implied_move` | [Market Data Cache and Warmers](data/market-data-cache-and-warmers.md) | `tests/test_no_fabrication.py`, `tests/test_market_context.py`, `tests/test_freshness.py` |
| Modify non-Yahoo fallbacks | `sources.py` | `_fmp_get`, `_finnhub_get`, `_alpaca_get`, `price_targets`, `ratings_spread`, `options_putcall`, `short_percent_float`, `daily_bars` | [Provider Failover](data/provider-failover.md) | `tests/test_sources.py` |
| Change background warming | `app.py`, `market_data.py` | `_ensure_warmer_running`, `_warm_loop`, `_info_loop`, `request_warm`, `_holds_warm_lease`, `batch_history`, `refresh_last_bar` | [Market Data Cache and Warmers](data/market-data-cache-and-warmers.md) | `tests/test_freshness.py`, `tests/test_sources.py` |
| Change six-factor scoring | `recommendation.py`, `app.py` | `WEIGHTS`, `MIN_FACTORS_FOR_SCORE`, `score_rebound`, `score_stock`, `calculate_ai_rebound_prediction` | [Rebound Score and Board Ranking](scoring/rebound-score.md) | `tests/test_no_fabrication.py`, `tests/test_rank.py` |
| Change board ranking | `app.py` | `COMPOSITE_WEIGHTS`, `_composite_rank`, `_board_sort_key`, `filter_ai_recovery_potential` | [Rebound Score and Board Ranking](scoring/rebound-score.md) | `tests/test_rank.py` |
| Change target-hit odds | `timeframes.py`, `app.py` | `hit_rate`, `best_hit_rate`, `annotate_targets`, `_evidence_bases`, `_horizon_summaries`, `_attach_empirical_probabilities` | [Empirical Odds and Timeframes](scoring/empirical-odds-and-timeframes.md) | `tests/test_odds_engine.py`, `tests/test_polish.py` |
| Change sophisticated timeframe target builder | `sophisticated_timeframe.py`, `app.py` | `SophisticatedTimeframePredictor`, `_sophisticated_cached`, `get_sophisticated_timeframe`, `predict_stock_recovery` | [Empirical Odds and Timeframes](scoring/empirical-odds-and-timeframes.md) | `tests/test_odds_engine.py`, `tests/test_freshness.py` |
| Change social sentiment | `social.py`, `app.py` | `stocktwits`, `reddit`, `sentiment`, `_phrases`, `analyze_social_sentiment` | [Professional Analysis APIs](analysis/professional-analysis-apis.md) | `tests/test_no_fabrication.py::TestPhraseExtraction` |
| Change news/fall reason | `app.py`, `market_data.py` | `analyze_stock_news`, `classify_fall_reason`, `headlines`, `analyst_recommendations` | [Professional Analysis APIs](analysis/professional-analysis-apis.md) | `tests/test_sources.py`, add route/helper test for classifier changes |
| Change options/institutional/economic APIs | `app.py`, `market_data.py`, `econ_calendar.py` | `analyze_options_flow`, `track_institutional_flow`, `get_economic_calendar_impact`, `upcoming_events` | [Professional Analysis APIs](analysis/professional-analysis-apis.md) | `tests/test_market_context.py`, `tests/test_gold_standard.py`, `tests/test_sources.py` |
| Change snapshots or track record | `app.py`, `tracking.py` | `api_snapshot`, `build_snapshot`, `tracked_symbols`, `compute_track_record`, `compute_calibration` | [Snapshots, Track Record, and Calibration](tracking/snapshots-track-record-and-calibration.md) | `tests/test_polish.py`, `tests/test_gold_standard.py`, `tests/test_live_gate.py` |
| Change paper trading | `sources.py`, `app.py`, `tracking.py` | `paper_execute_picks`, `paper_manage_positions`, `paper_account_overview`, `_alpaca_trading_base`, `live_readiness` | [Paper Trading and Live Gate](trading/paper-trading-and-live-gate.md) | `tests/test_sources.py`, `tests/test_live_gate.py` |
| Change evaluation CLI or report-only fitting | `backtest.py`, `walkforward.py` | `run`, `main`, `walk_forward`, `_training_rows` | [Backtesting and Walk-Forward Evaluation](evaluation/backtesting-and-walkforward.md) | `tests/test_gold_standard.py`, CLI smoke for `backtest.py` |
| Change deployment | `Dockerfile`, `gunicorn.conf.py`, `docker-compose.yml`, `nginx.conf`, `k8s-deployment.yaml` | Gunicorn config, compose services, NGINX routes, HPA | [Deployment and Observability](operations/deployment-and-observability.md) | Container smoke plus `python -m pytest tests/ -q` |
| Change configuration/secrets | `secrets_store.py`, env reads in `app.py`, `market_data.py`, `sources.py`, `tracking.py` | `get`, `status`, env constants, live arming vars | [Configuration and Secrets](operations/configuration-and-secrets.md) | `tests/test_no_fabrication.py`, `tests/test_sources.py`, `tests/test_live_gate.py` |
| Change docs automation or authored/generated wiki boundaries | `.github/workflows/openwiki-update.yml`, `.github/openwiki-toolchain/package.json`, `.github/openwiki-toolchain/package-lock.json`, `.github/workflows/lint.yml`, `.markdownlint-cli2.yaml`, `tools/check_wiki_facts.py`, `wiki/index.md` | `openwiki code --update --print`, `wiki-facts`, `markdownlint-cli2`, `OPENWIKI_PUSH_TOKEN` PR boundary | [Deployment and Observability](operations/deployment-and-observability.md), [Testing Strategy and Fixtures](testing/strategy-and-fixtures.md) | `python3 tools/check_wiki_facts.py`, `npx --yes markdownlint-cli2 "**/*.md"`, `npm ci --prefix .github/openwiki-toolchain` |

## Minimal full validation

```bash
python -m pytest tests/ -q
```

Optional operational validation:

```bash
docker build -t yahoo-losers-webapp .
docker run --rm -p 8080:8080 yahoo-losers-webapp
curl http://localhost:8080/health
```
