---
type: testing guide
title: Testing Strategy and Fixtures
description: Explains offline test isolation, cache reset fixtures, domain-specific regression suites, CI checks, and focused validation commands for safe changes.
tags: [testing, pytest, ci]
---

# Testing Strategy and Fixtures

The test suite is designed to run offline and deterministically. Most bugs guarded by the suite are not generic correctness bugs; they are regressions from live incidents where missing provider data, cache behavior, or trading state could be misrepresented.

## Global fixtures

`tests/conftest.py` installs two autouse fixtures:

1. `_in_memory_cache_only(monkeypatch)` disables Redis on the real `market_data._cache`, clears `_local` before each test, and clears it again after the test. This prevents shared Redis state from leaking into cache-sensitive tests.
2. `_no_network_in_sources(monkeypatch)` replaces `sources.requests.get` and `sources.requests.post` with functions that raise `AssertionError` unless a test explicitly monkeypatches them. This prevents accidental live provider calls.

The file also sets `MARKET_DATA_DISABLE_WARMER=1` before importing `market_data` or `app.py`, so route tests do not spawn daemon warmers or page prebuilders.

## Test suite map

| File | Primary coverage |
| --- | --- |
| `tests/test_no_fabrication.py` | `Sourced`, `safe_ratio`, `parse_money`, startup Redis fallback, scoring no-fabrication, social phrase extraction, cache lifetimes. |
| `tests/test_sources.py` | Provider failover, FMP budget, losers fallback, grades, earnings, splits, calendar injection, price failover, paper-guard behavior. |
| `tests/test_odds_engine.py` | Post-drop masks, RSI masks, intraday-touch hit rates, evidence ladder, shrinkage, cohort priors, earnings-window flags. |
| `tests/test_freshness.py` | Last-bar refresh, page prebuild policy, recency evidence disclosure, long-horizon tracking lookback, systemic missing-factor banner, earnings chips. |
| `tests/test_rank.py` | Composite rank math, neutral imputation for missing rank components, board sort defaults, stale composite recomputation. |
| `tests/test_market_context.py` | Options-implied move, going-concern flags, cache-only render paths, drop/sector conditioning. |
| `tests/test_polish.py` | Recency weighting, calibration touch grading, liquidity chips, README methodology disclosures, audit regressions. |
| `tests/test_gold_standard.py` | SEC Form 4 parsing, XBRL fundamentals, solvency source choice, calibration unresolved states, broader gold-standard regressions. |
| `tests/test_live_gate.py` | Live-readiness criteria, live arming refusal paths, inspector page safety and rate limiting. |

## Running tests

Full offline suite:

```bash
python -m pytest tests/ -q
```

Focused commands by change area:

| Change area | Command |
| --- | --- |
| Provenance and missing-data display | `python -m pytest tests/test_no_fabrication.py -q` |
| Provider failover or FMP/Alpaca/Finnhub/FINRA integration | `python -m pytest tests/test_sources.py -q` |
| Market-data cache and warmers | `python -m pytest tests/test_freshness.py tests/test_sources.py -q` |
| Rebound score or ranking | `python -m pytest tests/test_no_fabrication.py tests/test_rank.py -q` |
| Empirical odds or target probabilities | `python -m pytest tests/test_odds_engine.py tests/test_polish.py -q` |
| SEC, solvency, implied move, sector context | `python -m pytest tests/test_market_context.py tests/test_gold_standard.py -q` |
| Paper trading and live gate | `python -m pytest tests/test_sources.py tests/test_live_gate.py -q` |
| Snapshot, calibration, walk-forward | `python -m pytest tests/test_polish.py tests/test_gold_standard.py tests/test_live_gate.py -q` |

CI uses `.github/workflows/tests.yml`, which runs the full suite under Python 3.13 and then performs grep guards for specific fabricated fallback patterns and bare `except:` in `app.py`.

## What tests imply for implementation changes

- If a route calls a provider directly, add a test proving the path is either explicitly allowed or cache-only. Many regressions were caused by accidental render-path fetches.
- If a provider failure can occur, test that the returned shape is unavailable with a reason, not a zero, neutral score, or guessed date.
- If a cache key's payload shape changes, bump or version the key. Tests often check that stale data cannot poison new code paths.
- If a new endpoint uses Celery, monkeypatch `.delay()` and `AsyncResult`; do not require a live worker in unit tests.
- If a new source uses credentials, route through `secrets_store.get()` and add tests that missing keys produce unavailable status.

## CI and audit gates

- `tests.yml`: offline pytest plus grep guards for known fabricated fallback patterns.
- `audit.yml`: `pip-audit` against resolved dependencies.
- `gitleaks.yml`: secret scanning.
- `lint.yml`: lint/format enforcement.
- `healthwatch.yml`: production source-health monitor, not a unit test.
- `snapshot.yml`: production data-recording workflow, not part of PR tests.

## Debugging flaky tests

Common causes:

- Redis state leaked in: confirm the autouse fixture still forces `market_data._cache._redis = None`.
- Background warmer started: confirm `MARKET_DATA_DISABLE_WARMER` is set before importing `app.py`.
- Unmocked `sources.requests` call escaped: the fixture intentionally raises so the test should monkeypatch the exact provider call it exercises.
- Wall-clock-dependent market phase: tests should monkeypatch `market_data.market_phase()` or use deterministic dates.
