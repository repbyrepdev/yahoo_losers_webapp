"""Point-in-time backtest of the technical portion of the rebound score.

Why this exists: a scoring model that has never been compared against realised
outcomes is an opinion with arithmetic attached. This measures whether the
score actually separates future winners from losers, and reports the answer
whichever way it comes out.

What can and cannot be tested with this data source
---------------------------------------------------
yfinance serves daily OHLCV history, so RSI, Bollinger %B, the gap to the
20-day mean and relative volume can all be recomputed exactly as they would
have appeared on any past date. Those factors are backtestable.

Analyst targets, rating spreads, options chains and short interest are only
available as of *now*. There is no historical snapshot, so using today's values
at a past date would be look-ahead bias -- the model would be scored on
information that did not exist yet, which reliably manufactures results that
evaporate in live use. Those factors are therefore excluded here, and the
headline result covers the technical subset only.

Known limitations, stated rather than papered over:

* Survivorship bias. The universe is chosen today, so companies that delisted
  are absent and the sample skews toward survivors.
* No transaction costs, slippage or borrow costs.
* Forward returns are close-to-close and ignore dividends.
* A backtest describes the past. It is not evidence of future performance.
"""

import argparse
import logging
import sys
import warnings
from typing import Dict, List, Optional

warnings.filterwarnings("ignore")

import pandas as pd
import yfinance as yf

import recommendation

logger = logging.getLogger(__name__)

# Trading days between scoring and measurement.
DEFAULT_HORIZONS = (5, 20, 60)

# Minimum bars of history before a score can be computed at all.
MIN_HISTORY_BARS = 40


def _technicals_at(history: pd.DataFrame, index: int) -> Optional[dict]:
    """Recompute the technical factors using only bars up to `index`.

    Slicing the frame before every calculation is what keeps this honest: no
    indicator can see a bar that had not printed yet.
    """
    window = history.iloc[: index + 1]
    if len(window) < MIN_HISTORY_BARS:
        return None

    close = window["Close"].dropna()
    volume = window["Volume"].dropna()
    if len(close) < MIN_HISTORY_BARS:
        return None

    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    avg_loss = loss.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    rs = avg_gain / avg_loss.replace(0, float("nan"))
    rsi = (100 - (100 / (1 + rs))).iloc[-1]

    ma20 = close.rolling(20).mean()
    std20 = close.rolling(20).std()
    upper, lower = ma20 + 2 * std20, ma20 - 2 * std20
    width = upper.iloc[-1] - lower.iloc[-1]
    percent_b = ((close.iloc[-1] - lower.iloc[-1]) / width) if width else None

    last_close = float(close.iloc[-1])
    ma20_last = float(ma20.iloc[-1]) if ma20.notna().iloc[-1] else None
    avg_volume_20 = float(volume.tail(20).mean()) if len(volume) >= 20 else None
    latest_volume = float(volume.iloc[-1]) if len(volume) else None

    if rsi != rsi or ma20_last is None:  # NaN guard
        return None

    return {
        "close": last_close,
        "rsi14": round(float(rsi), 2),
        "percent_b": round(float(percent_b), 3) if percent_b is not None and percent_b == percent_b else None,
        "ma20": ma20_last,
        "pct_from_ma20": (last_close - ma20_last) / ma20_last,
        "volume_ratio_20d": (latest_volume / avg_volume_20) if avg_volume_20 and latest_volume else None,
    }


def score_technical_only(technicals: dict) -> Optional[float]:
    """Score using only the backtestable factors, via the production model.

    This deliberately calls the same `score_rebound` the app uses rather than a
    reimplementation, so the backtest cannot drift away from live behaviour.
    Passing no analyst inputs leaves two factors available, which is below the
    production minimum, so the internal factor scorers are used directly and
    combined with the same renormalisation rule.
    """
    tech_factor = recommendation._score_technical_reversion(technicals)
    volume_factor = recommendation._score_volume_capitulation(
        technicals.get("volume_ratio_20d"))

    available = [f for f in (tech_factor, volume_factor) if f.available]
    if not available:
        return None
    total_weight = sum(f.weight for f in available)
    return sum(f.score * (f.weight / total_weight) for f in available)


def run(symbols: List[str], years: int = 3, step_days: int = 21,
        horizons=DEFAULT_HORIZONS) -> Dict:
    """Score every symbol at regular past dates and measure realised returns."""
    observations = []
    fetched = 0

    for symbol in symbols:
        try:
            history = yf.Ticker(symbol).history(period=f"{years}y", interval="1d")
        except Exception as e:
            logger.warning(f"{symbol}: history failed ({type(e).__name__})")
            continue
        if history is None or len(history) < MIN_HISTORY_BARS + max(horizons) + 5:
            continue
        fetched += 1

        closes = history["Close"].values
        last_scoreable = len(history) - max(horizons) - 1

        for i in range(MIN_HISTORY_BARS, last_scoreable, step_days):
            technicals = _technicals_at(history, i)
            if not technicals:
                continue
            score = score_technical_only(technicals)
            if score is None:
                continue

            entry = closes[i]
            if not entry:
                continue

            row = {"symbol": symbol, "date": history.index[i].date().isoformat(),
                   "score": score, "rsi": technicals["rsi14"]}
            for horizon in horizons:
                exit_price = closes[i + horizon]
                row[f"fwd_{horizon}d"] = (exit_price - entry) / entry * 100
            observations.append(row)

    if not observations:
        return {"ok": False, "reason": "no scoreable observations"}

    frame = pd.DataFrame(observations)

    # Buckets follow the production recommendation bands so the backtest
    # answers the question the app actually asks.
    def bucket(score):
        if score >= 70:
            return "70+ (strong)"
        if score >= 58:
            return "58-70 (constructive)"
        if score >= 45:
            return "45-58 (neutral)"
        return "<45 (weak)"

    frame["bucket"] = frame["score"].apply(bucket)

    results = {"ok": True, "symbols_used": fetched, "observations": len(frame),
               "period_years": years, "horizons": list(horizons), "buckets": {}, "baseline": {}}

    for horizon in horizons:
        column = f"fwd_{horizon}d"
        results["baseline"][f"{horizon}d"] = round(float(frame[column].mean()), 3)

    order = ["70+ (strong)", "58-70 (constructive)", "45-58 (neutral)", "<45 (weak)"]
    for name in order:
        subset = frame[frame["bucket"] == name]
        if subset.empty:
            continue
        entry = {"n": int(len(subset))}
        for horizon in horizons:
            column = f"fwd_{horizon}d"
            mean = float(subset[column].mean())
            entry[f"{horizon}d_mean"] = round(mean, 3)
            entry[f"{horizon}d_excess"] = round(mean - results["baseline"][f"{horizon}d"], 3)
            entry[f"{horizon}d_win_rate"] = round(float((subset[column] > 0).mean()), 3)
        results["buckets"][name] = entry

    # Rank correlation between score and realised return: the single number
    # that says whether the ordering carries information at all.
    #
    # Computed as Pearson correlation of the ranks, which is the definition of
    # Spearman's rho. pandas' method="spearman" would pull in scipy for the same
    # arithmetic, and this tool should not add a heavy dependency for one number.
    score_ranks = frame["score"].rank()
    results["spearman"] = {
        f"{horizon}d": round(float(score_ranks.corr(frame[f"fwd_{horizon}d"].rank())), 4)
        for horizon in horizons
    }
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", default="", help="comma-separated; defaults to a built-in universe")
    parser.add_argument("--years", type=int, default=3)
    parser.add_argument("--step-days", type=int, default=21)
    args = parser.parse_args()

    universe = [s.strip().upper() for s in args.symbols.split(",") if s.strip()] or DEFAULT_UNIVERSE
    results = run(universe, years=args.years, step_days=args.step_days)

    if not results.get("ok"):
        print("Backtest failed:", results.get("reason"))
        return 1

    print(f"\nObservations: {results['observations']}  "
          f"Symbols: {results['symbols_used']}  Period: {results['period_years']}y")
    print("Baseline mean forward return: "
          + ", ".join(f"{h}d {v:+.2f}%" for h, v in results["baseline"].items()))
    print(f"\n{'Bucket':24}{'n':>7}" + "".join(f"{h}d excess".rjust(13) for h in results["horizons"])
          + "".join(f"{h}d win".rjust(11) for h in results["horizons"]))
    for name, entry in results["buckets"].items():
        row = f"{name:24}{entry['n']:>7}"
        row += "".join(f"{entry[f'{h}d_excess']:+12.2f}%" for h in results["horizons"])
        row += "".join(f"{entry[f'{h}d_win_rate']:10.1%}" for h in results["horizons"])
        print(row)
    print("\nSpearman rank correlation (score vs realised return):")
    for horizon, value in results["spearman"].items():
        print(f"  {horizon}: {value:+.4f}")
    return 0


# A liquid, sector-diverse universe. Chosen today, so it carries survivorship
# bias -- delisted names are absent. Stated here rather than hidden.
DEFAULT_UNIVERSE = [
    "AAPL", "MSFT", "GOOGL", "AMZN", "META", "NVDA", "TSLA", "AMD", "INTC", "CRM",
    "JPM", "BAC", "WFC", "GS", "MS", "C", "AXP", "SCHW",
    "JNJ", "PFE", "UNH", "MRK", "ABBV", "LLY", "BMY", "GILD",
    "XOM", "CVX", "COP", "SLB", "OXY",
    "WMT", "HD", "TGT", "COST", "NKE", "SBUX", "MCD", "LOW",
    "BA", "CAT", "GE", "MMM", "UPS", "FDX", "DE",
    "DIS", "NFLX", "CMCSA", "T", "VZ", "PYPL", "SQ", "UBER", "ABNB", "RIVN",
    "F", "GM", "PTON", "WING", "CHWY", "ETSY", "ROKU", "SNAP", "PINS", "LYFT",
]

if __name__ == "__main__":
    sys.exit(main())
