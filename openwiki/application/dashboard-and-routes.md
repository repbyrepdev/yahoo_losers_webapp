---
type: application surface
title: Dashboard and Routes
description: Route-by-route guide to the Flask dashboard, JSON APIs, cache behavior, security headers, rate limits, PWA assets, task placeholders, and browser error telemetry.
tags: [flask, api, ui, routes]
---

# Dashboard and Routes

`app.py` owns the Flask public surface. It defines `app = Flask(__name__)`, initializes `SophisticatedTimeframePredictor`, configures compression and CORS, registers request logging and security headers, and exposes both HTML and JSON endpoints.

The single most important route is `/`, which builds the daily-losers board described in [Architecture Overview](../architecture/overview.md). Domain API implementations are split across [Rebound Score and Board Ranking](../scoring/rebound-score.md), [Empirical Odds and Timeframes](../scoring/empirical-odds-and-timeframes.md), [Professional Analysis APIs](../analysis/professional-analysis-apis.md), and [Snapshots, Track Record, and Calibration](../tracking/snapshots-track-record-and-calibration.md).

## HTTP middleware

| Hook/helper | Responsibility |
| --- | --- |
| `_ensure_warmer_running()` | Starts `market_data.start_background_warmer()`, the page prebuilder, and the sophisticated-timeframe prewarmer once per worker after a request arrives. This is request-time because `gunicorn.conf.py` has `preload_app = True`. |
| `log_request_info()` / `log_request_end()` | Emits structured request start/end logs with method, path, status, duration, remote address, user agent, and content length. |
| `add_security_headers()` | Adds CSP, HSTS only for secure requests, `X-Content-Type-Options`, `X-Frame-Options`, XSS protection, Referrer Policy, Permissions Policy, and strong no-store headers for the `index` endpoint. |
| `rate_limit()` | In-memory per-client rate limiter. Defaults are `MAX_REQUESTS_PER_MINUTE = 30` and `MAX_AI_REQUESTS_PER_MINUTE = 10`. |
| `generate_etag()` and `add_cache_headers()` | ETag generation from JSON-serializable content and public cache headers with `Vary: Accept-Encoding`. |

CORS is configured with `CORS_ORIGINS` or `*`, and `supports_credentials=False` by design because the app has no cookie/auth state.

## Homepage route `/`

`index()` first calls `load_cache()` and returns a cached render when valid. On a cache miss, it performs the full board build:

1. `stable_universe()` gives a consistent losers universe for `UNIVERSE_TTL_SECONDS`.
2. `get_stock_details(symbols)` queues background warming and fetches chart quote fields in parallel using `QUOTE_WORKERS`.
3. `calculate_enhanced_investment_analysis()` adds score, probability columns, sector, analyst revisions, filings/earnings/liquidity chips, and composite rank.
4. `filter_ai_recovery_potential()` builds the recommendations panel.
5. `get_market_status()` and `get_comprehensive_market_analysis()` add broad context.
6. `degraded_state()` decides whether the page is transiently degraded.
7. `save_cache()` stores the full template variables unless the universe failed.
8. `format_results_as_html()` returns the embedded HTML template string for `render_template_string()`.

A failed universe page is returned with `Cache-Control: no-store`, preventing provider outages from becoming cached product state.

### `stable_universe()` details

`stable_universe()` is single-flight under `_universe_lock`. It first checks `market_data._cache.get('universe:v1')`; when a hit exists, it returns the cached list with `status['data_source'] = 'cached'` and a message naming how many seconds ago the scrape occurred. On a miss, one caller holds the lock and scrapes Yahoo; concurrent callers re-check the cache after waiting so they do not race separate loser lists into the same page cadence.

Only successful universes are stored under `universe:v1` for `UNIVERSE_TTL_SECONDS`. If Yahoo fails and both fallbacks fail, the error row returned by `scrape_yahoo_losers()` is not cached; the next request can try providers again.

Fallback ordering is Yahoo screener, then `sources.alpaca_losers()` labeled `alpaca-failover`, then `sources.fmp_losers()` labeled `fmp-failover`. Both fallback providers pass raw mover rows through `sources._normalize_loser_rows()`, which shapes them into Yahoo-like dictionaries with `Symbol`, `Name`, `Change`, `Percent Change`, `Volume`, and `Market Cap`, caps to 25, and filters sub-dollar, warrant/unit, and dotted-class tickers.

## Rendered page cache

`app.py` has a rendered page cache separate from `market_data.TTLCache`:

- Redis key: `yahoo_losers_cache` when `REDIS_URL` is valid.
- File fallback: `/tmp/yahoo_finance_cache.pkl`.
- Lifetime comes from `page_cache_policy()` and is capped at session-phase boundaries.
- Degraded pages are capped by `DEGRADED_CACHE_SECONDS`.
- Redis payloads include `expires_at` so `_page_prebuild_loop()` can rebuild before expiry.

`/refresh` clears this rendered page cache and calls `market_data.clear_cache()` so manual refresh can recover from stale data, not just stale HTML.

`_page_prebuild_loop()` uses the same route code as a visitor by issuing an internal `client.get('/')`, so a prebuilt page and a user-rendered page cannot diverge. It runs only when `_page_needs_prebuild()` sees no cache, an expired cache, or an expiry inside `PAGE_PREBUILD_LOOKAHEAD_SECONDS`, and when `market_data._holds_warm_lease()` grants this worker the provider-work lease. `_stf_prewarm_loop()` warms one missing `stf:<SYMBOL>` analysis per pass by calling `/api/sophisticated-timeframe/<symbol>` and sleeps longer after doing so because that path touches options data.

## HTML pages and exports

| Route | Function | Behavior |
| --- | --- | --- |
| `/` | `index()` | Main dashboard with daily losers, details, scores, recommendations, market context, and paper-account overview. |
| `/track-record` | `track_record_page()` | Cached static-ish page generated from `tracking.compute_track_record()`, `tracking.compute_calibration()`, `walkforward.walk_forward()`, and `_graduation_section()`. |
| `/methodology` | `methodology_page()` | Renders `README.md` through Markdown with tables and fenced code. |
| `/inspect/<symbol>` | `inspect_position()` | Fetch-free inspection page for recorded claims, current cached close, and paper lifecycle rails for a sanitized ticker. |
| `/export/csv` | `export_csv()` | CSV export of current cached or freshly built board data. Coerces cells to strings before comma handling so numeric fields cannot crash export. |
| `/sw.js` | `service_worker()` | Serves the root-scoped service worker from `static/sw.js`. |

The PWA manifest in `static/manifest.json` names the app `Daily Losers Analysis`, starts at `/`, uses standalone display, and declares icon assets. `static/sw.js` caches only static icons/manifest and deliberately uses network-first behavior for all live market pages.

`/inspect/<symbol>` sanitizes the path segment with `re.sub(r"[^A-Z0-9.\\-]", "", symbol.upper())[:6]` before embedding it in HTML. Provider failure strings shown through route payloads are also protected at the data boundary: [Market Data Cache and Background Warmers](../data/market-data-cache-and-warmers.md) documents how `_cached()` redacts not-ok `reason` and `detail` fields before cache storage, with FMP/Finnhub helper redaction as defense in depth.

## JSON APIs

| Route | Function | Backing system |
| --- | --- | --- |
| `/api/snapshot` | `api_snapshot()` | Full daily model output for `.github/workflows/snapshot.yml`; also records paper lifecycle/orders and survivorship context. |
| `/api/recovery-prediction/<symbol>` | `get_recovery_prediction()` | `predict_stock_recovery()` compatibility summary around the sophisticated analysis. |
| `/api/sophisticated-timeframe/<symbol>` | `get_sophisticated_timeframe()` | `_sophisticated_cached()`, `SophisticatedTimeframePredictor`, and empirical probability attachment. |
| `/api/social-sentiment/<symbol>` | `get_social_sentiment()` | `analyze_social_sentiment()` and `social.sentiment()`. |
| `/api/news-analysis/<symbol>` | `get_news_analysis()` | `analyze_stock_news()`, real headlines, fall-reason classifier, analyst posture. |
| `/api/options-flow/<symbol>` | `get_options_flow()` | `analyze_options_flow()` around `market_data.options_flow()` and earnings timing. |
| `/api/institutional-flow/<symbol>` | `get_institutional_flow()` | `track_institutional_flow()` with 13F holders, FINRA short volume, Form 4, solvency. |
| `/api/economic-calendar/<symbol>` | `get_economic_calendar()` | `get_economic_calendar_impact()` and `econ_calendar.upcoming_events()`. |
| `/api/professional-analysis/<symbol>` | `get_professional_analysis()` | Aggregates options, institutional, and economic-calendar analyses. |
| `/api/ai-analysis/<symbol>` | `get_ai_stock_analysis()` | `calculate_ai_rebound_prediction()` and the production rebound score. |

Most JSON routes add 60-second cache headers and ETags where the payload is expensive and deterministic enough for a short-lived client cache.

## Celery task endpoints

`make_celery(app)` configures a Celery app with Redis as broker/backend when `USE_REDIS` is true. If Redis is unavailable, the backend falls back to `rpc://` but the broker remains `redis://localhost:6379/0`, so the task endpoints are best treated as optional scaffolding unless Redis is present.

Task functions currently return placeholder `pending` payloads rather than running heavy analysis:

- `predict_recovery_task(symbol)`
- `analyze_sentiment_task(symbol)`
- `bulk_analysis_task(symbols)`

Public task routes:

- `/api/tasks/start/<symbol>` starts recovery and sentiment tasks with `.delay()` and returns task IDs.
- `/api/tasks/status/<task_id>` reads `AsyncResult` and returns one of `PENDING`, `PROGRESS`, `SUCCESS`, or failure with error text.

The task start route is rate-limited with `MAX_AI_REQUESTS_PER_MINUTE`; status is not decorated in the current source. There is limited direct test coverage for these endpoints, so changes should include a focused Flask test with Celery monkeypatched rather than relying on a running worker.

## Browser client-error telemetry

Client-side failures can be posted to `/api/client-error`. The handler:

- Reads JSON silently.
- Stores `at`, `msg`, `src`, and `line` in `_CLIENT_ERRORS`, a `deque(maxlen=50)`.
- Truncates message and source strings to avoid unbounded payloads.
- Logs an error but returns `{"ok": true}` even if storage fails.
- Is rate-limited with `rate_limit(10)`.

`/api/client-errors` returns the current in-memory list. This state is per process, not durable and not shared through Redis.

## Health, sources, and metrics

`/health` is liveness-oriented. It returns process memory, page cache backend, market-data backend, and market-data entry count. Empty cache is not unhealthy; only memory above the hard threshold changes status to `unhealthy`.

`/health/sources` probes raw Yahoo endpoints, Reddit, StockTwits, and yfinance separately, returning status `ok` or `degraded` with HTTP 207 for degraded upstreams. `.github/workflows/healthwatch.yml` calls it every 30 minutes and opens or closes a GitHub issue for provider degradation.

`/metrics` reports memory and page-cache state. `nginx.conf` restricts it to private network ranges.

## Tests and validation

- Route behavior is partly tested by `tests/test_freshness.py`, `tests/test_rank.py`, `tests/test_live_gate.py`, and `tests/test_no_fabrication.py`.
- `tests/test_no_fabrication.py::TestStableUniverse.test_second_call_reuses_the_cached_list` proves `universe:v1` reuse, and `test_failed_scrape_is_not_cached` proves a failed scrape is not pinned.
- `tests/test_freshness.py::TestPagePrebuildPolicy.test_near_expiry_needs_build` covers page prebuild timing.
- `tests/test_sources.py::TestLosersFailover.test_universe_uses_fmp_when_scrape_fails` proves the fallback universe path.
- API domain logic is primarily covered through helper functions, not through every Flask route wrapper.
- The test fixture sets `MARKET_DATA_DISABLE_WARMER=1` before importing `app.py`, so tests do not spawn background threads.

Minimal validation after route changes:

```bash
python -m pytest tests/test_no_fabrication.py tests/test_freshness.py tests/test_rank.py tests/test_live_gate.py -q
```
