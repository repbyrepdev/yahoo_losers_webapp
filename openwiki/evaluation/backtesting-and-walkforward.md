---
type: evaluation guide
title: Backtesting and Walk-Forward Evaluation
description: Documents the technical-only backtest CLI and snapshot-based walk-forward fitting, including no-lookahead rules, horizons, outputs, and limitations.
tags: [evaluation, backtesting, walk-forward, scoring]
---

# Backtesting and Walk-Forward Evaluation

This repository has two evaluation surfaces:

- `backtest.py`: standalone technical-only historical backtest CLI.
- `walkforward.py`: snapshot-based, report-only factor-weight fitting used on the `/track-record` page.

They answer different questions. `backtest.py` can evaluate only factors reconstructable from historical OHLCV. `walkforward.py` evaluates the actual recorded factor payloads after snapshots have accumulated, but it waits for resolved future returns.

## `backtest.py` technical-only CLI

`backtest.py` deliberately avoids look-ahead bias by recomputing technical indicators from data available at each historical index.

Entry points:

- `main()` parses `--symbols`, `--years`, and `--step-days`.
- `run(symbols, years=3, step_days=21, horizons=DEFAULT_HORIZONS)` fetches yfinance daily OHLCV and builds observations.
- `_technicals_at(history, index)` slices history through the point-in-time bar before computing RSI-14, Bollinger `%B`, 20-day MA gap, and volume ratio.
- `score_technical_only(technicals)` calls the same internal factor scorers used by `recommendation.py` and renormalizes the technical and volume factors.

Default parameters:

| Name | Value | Meaning |
| --- | --- | --- |
| `DEFAULT_HORIZONS` | `(5, 20, 60)` | Trading-day forward-return windows reported by the CLI. |
| `MIN_HISTORY_BARS` | `40` | Minimum bars before a point-in-time technical score can be computed. |
| `DEFAULT_UNIVERSE` | Liquid, sector-diverse large-cap list | Used when `--symbols` is omitted. |

Outputs include observation count, symbols used, baseline mean forward returns, production-like score buckets, bucket excess returns, bucket win rates, and Spearman rank correlation between score and realized return.

## What `backtest.py` cannot validate

The module excludes factors whose historical point-in-time data is not available from the current provider APIs:

- Analyst targets.
- Analyst rating spread.
- Options chains.
- Short interest.

Using today's values for those factors on past dates would introduce look-ahead bias. The CLI therefore tests the technical subset only and states limitations: survivorship bias, no costs/slippage/borrow costs, close-to-close returns, dividend ignorance, and past-performance limits.

Example usage:

```bash
python backtest.py --symbols AAPL,MSFT,NVDA --years 3 --step-days 21
```

## `walkforward.py` snapshot-based evaluation

`walkforward.py` uses the app's own committed snapshot record, so it can evaluate actual factor scores after enough time passes.

Entry points:

- `_training_rows(directory=None)` reads snapshots through `tracking.load_snapshots()`, extracts row factor scores, and resolves 7-day forward returns from later snapshots.
- `_fit_ridge(matrix, returns)` solves ridge regression with `RIDGE_LAMBDA = 1.0`.
- `walk_forward(directory=None)` fits on past days and evaluates strictly later days.

Guardrails:

- Needs `MIN_FIT_DAYS = 20` distinct snapshot days with resolved forward returns before reporting results.
- `FIT_HORIZON = 7`, matching the first `tracking.HORIZONS` value.
- Training rows require `resolved_on < test_day`; a future-resolved row cannot train an earlier test day.
- The factor set and imputation means are derived from training rows only, not all rows.
- Missing factor scores are imputed as `50.0 / 100.0`, and the imputed share is reported.
- The live `recommendation.WEIGHTS` are unchanged; output status says report-only.

## Relationship to live scoring

`recommendation.py` remains the production scoring implementation. `backtest.py` imports it to reuse factor scoring math for technical factors. `walkforward.py` reports fitted weights beside the hand-chosen weights, but it does not change live scoring. A future change to production weights should use the walk-forward report as evidence and then update `recommendation.WEIGHTS` explicitly with tests.

## Validation and tests

`tests/test_gold_standard.py` covers walk-forward and calibration-adjacent invariants. `tests/test_polish.py` and `tests/test_sources.py` cover the snapshot evidence needed by these evaluations. There is no heavy live-provider backtest test in CI; the normal suite is offline.

Focused validation:

```bash
python -m pytest tests/test_gold_standard.py tests/test_polish.py -q
python backtest.py --symbols AAPL,MSFT --years 1 --step-days 21
```
