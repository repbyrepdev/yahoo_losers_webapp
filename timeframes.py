"""Empirical probability that a price target is reached, from real history.

The previous implementation started every estimate at a hard-coded 70, applied
fixed integer adjustments, multiplied by a "signal multiplier" and capped the
result at 95. The cap was reached constantly, so every target on a page showed
the identical 95% -- and the displayed arithmetic read "95% x 1.88 = 95%",
which is not arithmetic.

This measures the real thing instead. For a target of +9.7% within 7 trading
days, it walks that stock's own history, counts how many 7-day windows actually
delivered a 9.7% gain, and reports the frequency with its sample size. That is
a probability with a denominator, and it can be checked by hand.

Limitations, stated rather than buried:

* It assumes the past distribution of moves is informative about the next one.
  For a company whose situation has fundamentally changed, it is not.
* Windows overlap, so they are not statistically independent; the sample size
  overstates how much evidence is present.
* The history is split-adjusted but ignores dividends.
* A stock with too little history returns unavailable rather than a guess.
"""

import logging
from typing import Dict, List, Optional

import numpy as np

from provenance import Sourced

logger = logging.getLogger(__name__)

# Below this many windows the frequency is noise, not an estimate. Forty
# overlapping windows is already thin -- the sample-size caveat in the module
# docstring applies doubly here -- but it lets a stock with three months of
# trading history get a measured short-term rate instead of nothing. FRVO, a
# recent IPO, had 59 usable 7-day windows and failed the previous floor of 60
# by exactly one.
MIN_WINDOWS = 40

# Trading days per horizon band.
HORIZON_BARS = {"short": 7, "medium": 21, "long": 126}


def hit_rate(closes: np.ndarray, target_pct: float, horizon_bars: int,
             mask: Optional[np.ndarray] = None) -> Optional[dict]:
    """How often this stock gained at least `target_pct` within `horizon_bars`.

    A window counts as a hit if the target was reached at any point inside it,
    not only at the close, because a target being touched is what the display
    claims. The median number of days to first touch is returned alongside, so
    the timeframe shown is measured rather than assumed.
    """
    if closes is None or len(closes) < horizon_bars + MIN_WINDOWS:
        return None  # caller states the shortfall, with the actual bar count

    threshold = 1.0 + (target_pct / 100.0)
    hits = 0
    windows = 0
    days_to_hit: List[int] = []
    miss_end_returns: List[float] = []

    # Vectorised over the window, looped over start points: clear to read and
    # fast enough for the few hundred windows involved.
    for start in range(len(closes) - horizon_bars):
        if mask is not None and (start >= len(mask) or not mask[start]):
            continue  # window's start day falls outside the requested regime
        entry = closes[start]
        if not entry or entry <= 0:
            continue
        windows += 1
        forward = closes[start + 1: start + 1 + horizon_bars]
        reached = np.nonzero(forward >= entry * threshold)[0]
        if reached.size:
            hits += 1
            days_to_hit.append(int(reached[0]) + 1)
        else:
            # What actually happened when the target was NOT reached: the
            # realised return at the end of the window. This is the loss side
            # of the expected-value arithmetic, measured rather than assumed.
            miss_end_returns.append(float(forward[-1] / entry - 1.0) * 100.0)

    if windows < MIN_WINDOWS:
        return None

    p = hits / windows
    miss_median = float(np.median(miss_end_returns)) if miss_end_returns else None

    # Expected value of "buy now, take profit at the target or exit at the
    # horizon": P x gain + (1-P) x median outcome of the windows that missed.
    # Every term is measured from this stock's own history. Median rather than
    # mean on the miss side so one crash window cannot dominate the figure.
    expected_value = None
    if miss_median is not None:
        expected_value = p * target_pct + (1 - p) * miss_median
    elif hits == windows:
        expected_value = target_pct  # every window reached the target

    return {
        "probability": round(p, 4),
        "hits": hits,
        "windows": windows,
        "median_days_to_hit": int(np.median(days_to_hit)) if days_to_hit else None,
        "horizon_bars": horizon_bars,
        "miss_median_return": round(miss_median, 2) if miss_median is not None else None,
        "expected_value": round(expected_value, 2) if expected_value is not None else None,
    }


VIX_BUCKETS = ((0, 17, "calm (VIX<17)"), (17, 25, "normal (VIX 17-25)"),
               (25, 999, "stressed (VIX>25)"))


def vix_bucket(level: float) -> str:
    for low, high, name in VIX_BUCKETS:
        if low <= level < high:
            return name
    return "unknown"


def regime_conditioned(closes: np.ndarray, close_dates, vix_by_date: dict,
                       target_pct: float, band: str) -> Optional[dict]:
    """The same hit rate, restricted to windows that began in today's VIX regime.

    MNSO's all-history bounce rate includes 2022's bear market; if today is
    calm, the calm-days-only rate is the fairer comparison. Needs the stock's
    dated closes and a date-to-VIX map; returns None when today's VIX is
    unknown or too few windows share the regime, rather than padding the
    sample with days that do not match.
    """
    if not vix_by_date or close_dates is None or len(close_dates) != len(closes):
        return None
    todays_vix = None
    for d in sorted(vix_by_date)[::-1]:
        todays_vix = vix_by_date[d]
        break
    if todays_vix is None:
        return None
    bucket = vix_bucket(todays_vix)
    mask = np.array([vix_by_date.get(d) is not None and
                     vix_bucket(vix_by_date[d]) == bucket for d in close_dates])
    horizon = HORIZON_BARS.get(band, 21)
    measured = hit_rate(closes, target_pct, horizon, mask=mask)
    if not measured:
        return None
    return {"bucket": bucket, "todays_vix": round(float(todays_vix), 1), **measured}


def target_probability(closes: np.ndarray, target_pct: float, band: str) -> Sourced:
    """Empirical probability for one target, sourced and with its denominator."""
    source = "empirical:price-history"
    horizon = HORIZON_BARS.get(band, 21)

    if target_pct <= 0:
        # A target at or below the current price is already satisfied; calling
        # that a forecast would be meaningless.
        return Sourced.unavailable(source, "target is at or below the current price")

    measured = hit_rate(closes, target_pct, horizon)
    if not measured:
        bars = 0 if closes is None else len(closes)
        needed = horizon + MIN_WINDOWS
        return Sourced.unavailable(
            source,
            f"only {bars} trading days of history; measuring a {horizon}-day "
            f"window needs at least {needed}")

    return Sourced.live(measured, source)


def wilson_interval(hits: int, windows: int, z: float = 1.96):
    """95% Wilson score interval for a proportion.

    Reported beside every hit rate so a thin sample cannot masquerade as a
    solid one: 20/59 reads plus-or-minus twelve points, 1004/1233 plus-or-minus
    two. Overlapping windows are not independent, so the true uncertainty is
    somewhat wider than this -- stated in the methodology rather than hidden.
    """
    if windows <= 0:
        return (0.0, 0.0)
    p = hits / windows
    denom = 1 + z * z / windows
    centre = (p + z * z / (2 * windows)) / denom
    half = (z / denom) * ((p * (1 - p) / windows + z * z / (4 * windows ** 2)) ** 0.5)
    return (max(0.0, centre - half), min(1.0, centre + half))


def horizon_distribution(closes: np.ndarray, horizon_bars: int) -> Optional[dict]:
    """The raw forward-return distribution at a horizon: p10 / median / p90.

    "What has H days later actually looked like for this stock" -- no target,
    no model, just the measured spread. Rendered so a reader can see the
    dispersion a point estimate hides.
    """
    if closes is None or len(closes) < horizon_bars + MIN_WINDOWS:
        return None
    entries = closes[:-horizon_bars]
    exits = closes[horizon_bars:]
    valid = entries > 0
    if valid.sum() < MIN_WINDOWS:
        return None
    returns = (exits[valid] / entries[valid] - 1.0) * 100.0
    return {
        "p10": round(float(np.percentile(returns, 10)), 2),
        "median": round(float(np.percentile(returns, 50)), 2),
        "p90": round(float(np.percentile(returns, 90)), 2),
        "windows": int(valid.sum()),
        "horizon_bars": horizon_bars,
    }


def describe(measured: dict) -> str:
    """Plain statement of the evidence, for display next to the number."""
    return (f"{measured['hits']}/{measured['windows']} past "
            f"{measured['horizon_bars']}-day windows")


def annotate_targets(closes: np.ndarray, targets: Dict[str, dict], band: str) -> Dict[str, dict]:
    """Attach an empirical probability to each target in a timeframe band.

    Targets themselves are real -- previous close, moving averages, analyst
    consensus. Only the probability attached to them was invented, so only that
    is replaced here.
    """
    annotated = {}
    for name, target in targets.items():
        upside = target.get("upside_percent")
        entry = dict(target)

        if upside is None:
            entry["probability_available"] = False
            entry["probability_reason"] = "no upside computed"
            annotated[name] = entry
            continue

        sourced = target_probability(closes, upside, band)
        if sourced.ok:
            measured = sourced.value
            # The displayed timeframe was produced by a formula ("5 hours",
            # "10 hours"). The median time to actually reach this target is
            # measured, so it replaces the estimate.
            median_days = measured["median_days_to_hit"]
            if median_days:
                entry["timeframe"] = (f"~{median_days} trading day"
                                      f"{'s' if median_days != 1 else ''} (median)")
                entry["timeframe_source"] = "empirical:price-history"

            ci_low, ci_high = wilson_interval(measured["hits"], measured["windows"])
            entry.update({
                "probability_available": True,
                "expected_value": measured.get("expected_value"),
                "miss_median_return": measured.get("miss_median_return"),
                "probability": round(measured["probability"] * 100, 1),
                "ci_low": round(ci_low * 100, 1),
                "ci_high": round(ci_high * 100, 1),
                "evidence": describe(measured),
                "hits": measured["hits"],
                "windows": measured["windows"],
                "median_days_to_hit": measured["median_days_to_hit"],
                "probability_source": sourced.source,
            })
        else:
            # The predictor's original dict may still carry its invented
            # probability (the capped 95). Leaving it beside
            # probability_available=False hands consumers a number that was
            # explicitly disclaimed, so it is removed.
            entry.pop("probability", None)
            entry.pop("confidence", None)
            entry.update({
                "probability_available": False,
                "probability_reason": sourced.reason,
                "probability_source": sourced.source,
            })
        annotated[name] = entry
    return annotated
