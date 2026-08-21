# Architecture

Flask monolith (~15.5K lines of Python) on Render, one worker tier, with
GitHub Actions as the scheduler for anything stateful. There is no
database: state lives in **committed JSON snapshots** (`data/snapshots/`),
the **cache** (Redis when configured, in-process dict otherwise), and the
**Alpaca paper account** (broker-resident orders as the exit engine).

## Modules

| Module | Role |
|---|---|
| `app.py` | Flask routes, page rendering, rate limiting, page-level caching |
| `sources.py` | Every external provider call: Yahoo, Alpaca, Finnhub, FMP, FINRA, EDGAR, FRED. Paper-trading order lifecycle. Provider helpers redact secrets at the boundary |
| `market_data.py` | The caching engine (`_cached`): TTL classification (transient / rate-limited / structural), day-claims (`claim_once`), lane cooldowns, technicals computation with the fallback chain |
| `provenance.py` | `Sourced` — value + source + ok/reason. The no-fabrication contract. `redact_secrets` |
| `recommendation.py` | Scoring engine: factor blend → empirical odds from the recorded track record |
| `tracking.py` | Snapshot record, calibration (Brier), forward returns, `live_readiness()` graduation gate |
| `timeframes.py` / `sophisticated_timeframe.py` | Horizon predictions (the second is legacy-heavy; kept working, not extended) |
| `backtest.py` / `walkforward.py` | Historical validation of the scoring weights |
| `econ_calendar.py` / `social.py` | Fed/FRED macro calendar; StockTwits/Reddit sentiment (Reddit user-gated OFF) |
| `secrets_store.py` | Env-var / Keychain secret access (`get_secret`) |

## Layering rule

`app.py` renders what `market_data`/`sources` return; producers return
dict payloads with `ok/reason/detail`; only the edge converts to
`Sourced` for display. Nothing in a template invents a number.

## Why no database

The snapshot record doubles as the audit trail: every night's universe,
predictions, and paper-trading events are one committed JSON file, so git
history IS the immutable record the calibration math runs on. A DB would
add ops burden and delete the "diff the record" property.
