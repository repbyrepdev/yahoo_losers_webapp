---
type: configuration reference
title: Configuration and Secrets
description: Environment variable and secret reference for app runtime, caches, warmers, scoring thresholds, providers, snapshot storage, CORS, and live-trading gates.
tags: [configuration, secrets, operations]
---

# Configuration and Secrets

Configuration is spread across `app.py`, `market_data.py`, `sources.py`, `tracking.py`, and deployment files. Secrets are never read from files in the repo; `secrets_store.py` reads environment variables first and can fall back to the macOS Keychain locally.

## Secret precedence

`secrets_store.get(name)` resolves credentials in this order:

1. Exact environment variable name, e.g. `FRED_API_KEY`.
2. Uppercase variant of the name.
3. macOS Keychain via `security find-generic-password` when running on Darwin and the `security` command exists.
4. `None` when not configured.

`secrets_store.status(names)` reports a boolean map and never includes secret values.

## Provider credentials

| Variable | Owner | Unlocks | Required? | Failure behavior |
| --- | --- | --- | --- | --- |
| `ALPACA_API_KEY` | `sources._alpaca_keys()` | Alpaca market data, calendar, corporate actions, and paper account. | No | Alpaca-backed calls return unavailable with `Alpaca keys not configured`. |
| `ALPACA_API_SECRET` | `sources._alpaca_keys()` | Same as above. | No | Same as above. |
| `FINNHUB_API_KEY` | `sources._finnhub_get()` | Ratings, news, earnings, profile2 fallback. | No | Finnhub calls return unavailable; callers may use Yahoo/FMP fallback. |
| `FMP_API_KEY` | `sources._fmp_get()` | FMP losers, targets, grades, earnings, float, splits, EOD history, delisted companies. | No | FMP calls return unavailable; daily budget still enforced when configured. |
| `FRED_API_KEY` | `econ_calendar.fred_releases()` and `market_data.fred_latest()` | FRED release dates and macro series. | No | FRED data is reported unavailable; no schedule is guessed. |
| `REDDIT_CLIENT_ID` | `social._reddit_token()` | Reddit OAuth search. | No | Reddit source unavailable; StockTwits can still supply sentiment. |
| `REDDIT_CLIENT_SECRET` | `social._reddit_token()` | Reddit OAuth search. | No | Same as above. |
| `EDGAR_USER_AGENT` | `market_data` | SEC EDGAR requests. | No | Defaults to repository-specific user agent string. |

## Runtime and route configuration

| Variable | Default | Owner | Impact |
| --- | --- | --- | --- |
| `PORT` | `8080` in Gunicorn config, `5000` for direct `python app.py` | `gunicorn.conf.py`, `app.py` | Network bind port. |
| `REDIS_URL` | `redis://localhost:6379/0` in `app.py`; optional in `market_data.TTLCache` | `app.py`, `market_data.py` | Enables shared page cache, Celery backend/broker, market-data cache, lease coordination, and FMP budget counter. Invalid values fall back gracefully for app page cache. |
| `CORS_ORIGINS` | `*` | `app.py` | Comma-separated origins for Flask-CORS; credentials are disabled. |
| `CACHE_MINUTES_MARKET_OPEN` | `10` | `app.PAGE_CADENCE_MINUTES` | Homepage cache cadence during regular market. |
| `CACHE_MINUTES_EXTENDED` | `30` | `app.PAGE_CADENCE_MINUTES` | Homepage cadence for pre-market and after-hours. |
| `DEGRADED_SCORE_RATIO` | `0.6` | `app.degraded_state()` | Minimum scored-row share before a page is considered degraded. |
| `DEGRADED_CACHE_SECONDS` | `90` | `app.save_cache()` | Upper bound for caching degraded pages. |
| `MIN_UPSIDE_PERCENT` | `65` | `app.calculate_investment_potential()` | Legacy upside filter threshold; current recommendation flow uses rebound score. |
| `MIN_REBOUND_SCORE` | `70` | `app.filter_ai_recovery_potential()` and `/api/snapshot` paper selection | Pick/recommendation threshold. |
| `QUOTE_WORKERS` | `8` | `app.get_stock_details()` | Thread pool size for per-symbol chart quote fetches. |
| `ECON_HORIZON_DAYS` | `30` | `app.get_economic_calendar_impact()` | Forward macro-event window. |
| `UNIVERSE_TTL_SECONDS` | `600` | `app.stable_universe()` | Losers universe cadence shared by page and warmers. |

## Market-data cache and warmer configuration

| Variable | Default | Owner | Impact |
| --- | --- | --- | --- |
| `MARKET_DATA_CACHE_FILE` | `/tmp/market_data_cache.json` | `market_data.py` | Disk snapshot of successful cache entries. |
| `MARKET_DATA_MIN_INTERVAL` | `0.8` | `market_data._throttle()` | Process-wide minimum gap between provider calls. |
| `MARKET_DATA_INFO_INTERVAL` | `4.0` | `market_data._info_throttle()` | Initial yfinance `.info` lane pacing. |
| `MARKET_DATA_INFO_INTERVAL_MAX` | `90.0` | `market_data._info_lane_refused()` | Upper bound for adaptive `.info` backoff. |
| `MARKET_DATA_DISABLE_WARMER` | unset | `app._ensure_warmer_running()`, `market_data.start_background_warmer()` | Disables background warmer/prebuilder threads; tests set it before importing `app.py`. |
| `MARKET_DATA_MAX_PROFILES` | `30` | `market_data._warm_loop()` | Max profile-cold symbols taken from the warm queue per cycle. |
| `MARKET_DATA_PRICE_REFRESH` | `300` | `market_data._warm_loop()` | Latest-bar patch cadence during non-closed phases. |

## Snapshot and evaluation configuration

| Variable | Default | Owner | Impact |
| --- | --- | --- | --- |
| `SNAPSHOT_DIR` | `data/snapshots` beside `tracking.py` | `tracking.py` | Directory scanned by track-record, calibration, and walk-forward evaluation. |

Model/evaluation thresholds are constants rather than env vars:

- `tracking.PICK_SCORE = 70.0`.
- `tracking.LIVE_MIN_RESOLVED = 100`.
- `tracking.LIVE_MAX_BRIER = 0.20`.
- `tracking.LIVE_MIN_GRADED_FILLS = 20`.
- `tracking.LIVE_MIN_SNAPSHOT_DAYS = 28`.
- `walkforward.MIN_FIT_DAYS = 20`, `FIT_HORIZON = 7`, `RIDGE_LAMBDA = 1.0`.

## Trading configuration and live arming

| Variable | Owner | Safety rule |
| --- | --- | --- |
| `ALPACA_PAPER_BASE` | `sources._alpaca_trading_base()` | If set, must exactly equal `https://paper-api.alpaca.markets`; otherwise paper mode raises. |
| `LIVE_TRADING_ARMED` | `sources._alpaca_trading_base('live')` | Must be exactly `yes-i-accept-losses` for live mode. |
| `LIVE_ALPACA_API_KEY` | `sources._alpaca_trading_context('live')` | Required only after live arming phrase is present. |
| `LIVE_ALPACA_API_SECRET` | `sources._alpaca_trading_context('live')` | Required only after live arming phrase is present. |

Paper-trading behavior itself is controlled by constants in `sources.py`: `PAPER_NOTIONAL_PER_PICK`, `PAPER_MAX_PICKS`, `PAPER_ENTRY_BAND_PCT`, `PAPER_TP_PCT`, `PAPER_STOP_PCT`, `PAPER_MAX_SESSIONS`, `PAPER_REENTRY_COOLDOWN_SESSIONS`, `PAPER_DAILY_LOSS_HALT_PCT`, and `PAPER_CATASTROPHE_STOP_PCT`.

## Deployment defaults

- `Dockerfile` installs `requirements.txt`, runs as non-root `appuser`, exposes `8080`, and starts `gunicorn -c gunicorn.conf.py app:app`.
- `gunicorn.conf.py` binds to `0.0.0.0:${PORT:-8080}`, uses 2 `gthread` workers, 4 threads, 120-second timeout, `max_requests = 1000`, jitter 50, stdout logs, and `preload_app = True`.
- `docker-compose.yml` sets `REDIS_URL=redis://redis:6379/0` and `PORT=8080` for the app service.
- `k8s-deployment.yaml` sets `PORT=8080` and `REDIS_URL=redis://redis-service:6379/0`.

## Validation

Relevant tests:

- `tests/test_no_fabrication.py::TestStartupRobustness` checks malformed `REDIS_URL` does not crash import.
- `tests/conftest.py` forces in-memory cache and disables warmers during tests.
- `tests/test_sources.py` monkeypatches secrets and providers for deterministic behavior.
- `tests/test_live_gate.py` verifies live-arming environment combinations.

Minimal validation:

```bash
python -m pytest tests/test_no_fabrication.py tests/test_sources.py tests/test_live_gate.py -q
```
