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

# Conditioned samples (post-drop days, oversold days) are structurally rarer
# than all-days samples; demanding the full forty would silence exactly the
# estimates this app is about. The Wilson interval shown alongside carries the
# extra width honestly.
MIN_WINDOWS_CONDITIONAL = 25

# A window "starts like today" when that day itself fell at least this much,
# close to close. Fixed rather than per-stock so the conditioning means the
# same thing on every row of the board.
SETUP_DROP_PCT = 4.0

RSI_OVERSOLD = 30.0

# Trading days per horizon band.
HORIZON_BARS = {"short": 7, "medium": 21, "long": 126}


def day_drop_mask(closes, min_drop_pct: float = SETUP_DROP_PCT,
                  max_drop_pct: Optional[float] = None) -> np.ndarray:
    """True on days whose close-to-close move was a drop inside the band.

    This is the conditioning the whole app implies: every symbol on the board
    is here because it just fell hard, so the informative history is windows
    that started from the same situation, not windows starting on any random
    Tuesday. With `max_drop_pct` the band is two-sided, for matching windows
    to drops of roughly today's magnitude -- a 4% dip and a 15% collapse are
    different situations.
    """
    closes = np.asarray(closes, dtype=float)
    mask = np.zeros(len(closes), dtype=bool)
    if len(closes) < 2:
        return mask
    prior = closes[:-1]
    with np.errstate(divide="ignore", invalid="ignore"):
        rets = np.where(prior > 0, closes[1:] / prior - 1.0, 0.0)
    hit = rets <= -(min_drop_pct / 100.0)
    if max_drop_pct is not None:
        hit &= rets >= -(max_drop_pct / 100.0)
    mask[1:] = hit
    return mask


def same_day_return_mask(dates, ref_dates, ref_closes,
                         max_ret_pct: Optional[float] = None,
                         min_ret_pct: Optional[float] = None) -> np.ndarray:
    """Per-date mask from another series' same-day return, aligned by date.

    Used for sector conditioning: True where the reference series (a sector
    ETF) moved inside the requested band that day. Dates absent from the
    reference are False -- an unknown sector day cannot satisfy a condition.
    """
    ref_returns = {}
    prior = None
    for d, c in zip(ref_dates, ref_closes):
        if c is not None and prior:
            ref_returns[d] = (c / prior - 1.0) * 100.0
        if c is not None:
            prior = c
    mask = np.zeros(len(dates), dtype=bool)
    for i, d in enumerate(dates):
        ret = ref_returns.get(d)
        if ret is None:
            continue
        if max_ret_pct is not None and ret > max_ret_pct:
            continue
        if min_ret_pct is not None and ret < min_ret_pct:
            continue
        mask[i] = True
    return mask


def rsi_series(closes, period: int = 14) -> np.ndarray:
    """Wilder RSI for every bar, NaN through the warm-up. Same formula as the
    board's RSI so 'oversold' means one thing everywhere."""
    import pandas as pd
    close = pd.Series(np.asarray(closes, dtype=float))
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, float("nan"))
    return (100 - (100 / (1 + rs))).to_numpy()


def oversold_mask(closes, threshold: float = RSI_OVERSOLD) -> np.ndarray:
    """True where RSI is below the oversold bound. NaN warm-up bars are False."""
    rsi = rsi_series(closes)
    return np.nan_to_num(rsi, nan=100.0) < threshold


SHRINK_PRIOR_WEIGHT = 20


def shrink_toward(hits: int, windows: int, prior_p: float,
                  prior_weight: int = SHRINK_PRIOR_WEIGHT) -> float:
    """Beta-binomial shrinkage of a thin sample toward a cohort rate.

    Twenty pseudo-windows of the cohort's rate are blended in, so 3/41 stops
    presenting itself as a precise 7% while a 400-window estimate barely moves.
    The display states both the raw and the shrunk figure.
    """
    return (hits + prior_p * prior_weight) / (windows + prior_weight)


# Recency half-life, in trading bars. A window from 2021 says less about the
# company the stock is now than one from last month; a one-year half-life
# means last year's evidence counts double the year before's. The raw counts
# stay reported; weighting shapes the probability and the effective sample.
RECENCY_HALF_LIFE_BARS = 252


def hit_rate(closes: np.ndarray, target_pct: float, horizon_bars: int,
             mask: Optional[np.ndarray] = None,
             highs: Optional[np.ndarray] = None,
             min_windows: int = MIN_WINDOWS,
             half_life_bars: Optional[int] = RECENCY_HALF_LIFE_BARS) -> Optional[dict]:
    """How often this stock gained at least `target_pct` within `horizon_bars`.

    A window counts as a hit if the target was reached at any point inside it,
    not only at the close, because a target being touched is what the display
    claims. When intraday highs are supplied (aligned with the closes), the
    touch test uses them -- a spike through the target that faded by the close
    is still a touch; testing closes alone systematically understates. The
    median number of days to first touch is returned alongside, so the
    timeframe shown is measured rather than assumed.
    """
    if closes is None or len(closes) < horizon_bars + min_windows:
        return None  # caller states the shortfall, with the actual bar count
    if highs is not None and len(highs) != len(closes):
        highs = None  # misaligned highs would test the wrong days

    threshold = 1.0 + (target_pct / 100.0)
    hits = 0
    windows = 0
    weight_sum = 0.0
    weight_sq_sum = 0.0
    weighted_hits = 0.0
    days_to_hit: List[int] = []
    miss_end_returns: List[float] = []
    last_start = len(closes) - horizon_bars - 1

    # Vectorised over the window, looped over start points: clear to read and
    # fast enough for the few hundred windows involved.
    for start in range(len(closes) - horizon_bars):
        if mask is not None and (start >= len(mask) or not mask[start]):
            continue  # window's start day falls outside the requested regime
        entry = closes[start]
        if not entry or entry <= 0:
            continue
        windows += 1
        weight = (0.5 ** ((last_start - start) / half_life_bars)
                  if half_life_bars else 1.0)
        weight_sum += weight
        weight_sq_sum += weight * weight
        forward = closes[start + 1: start + 1 + horizon_bars]
        touch = (highs[start + 1: start + 1 + horizon_bars]
                 if highs is not None else forward)
        reached = np.nonzero(touch >= entry * threshold)[0]
        if reached.size:
            hits += 1
            weighted_hits += weight
            days_to_hit.append(int(reached[0]) + 1)
        else:
            # What actually happened when the target was NOT reached: the
            # realised return at the end of the window. This is the loss side
            # of the expected-value arithmetic, measured rather than assumed.
            miss_end_returns.append(float(forward[-1] / entry - 1.0) * 100.0)

    if windows < min_windows:
        return None

    # Recency-weighted rate and Kish effective sample size. Raw counts stay
    # reported beside them -- the denominator the display shows is real.
    p = (weighted_hits / weight_sum) if weight_sum else hits / windows
    n_eff = (weight_sum * weight_sum / weight_sq_sum) if weight_sq_sum else windows
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
        "n_eff": round(n_eff, 1),
        "recency_half_life_bars": half_life_bars,
        "median_days_to_hit": int(np.median(days_to_hit)) if days_to_hit else None,
        "horizon_bars": horizon_bars,
        "miss_median_return": round(miss_median, 2) if miss_median is not None else None,
        "expected_value": round(expected_value, 2) if expected_value is not None else None,
        "touch_basis": "intraday-high" if highs is not None else "close",
    }


def shrink_toward_rate(p: float, n_eff: float, prior_p: float,
                       prior_weight: int = SHRINK_PRIOR_WEIGHT) -> float:
    """shrink_toward generalised to a weighted rate and effective sample."""
    return (p * n_eff + prior_p * prior_weight) / (n_eff + prior_weight)


def best_hit_rate(bases: List[dict], target_pct: float,
                  horizon_bars: int) -> Optional[dict]:
    """First adequate estimate down an evidence ladder.

    Each basis is {closes, highs, mask, min_windows, label}, best first --
    typically: post-drop intraday-touch, then unconditional intraday-touch,
    then post-drop close-basis, then the plain five-year close rate. The
    chosen rung's label rides in the result so the display can state exactly
    which question was answered.
    """
    for basis in bases:
        measured = hit_rate(basis["closes"], target_pct, horizon_bars,
                            mask=basis.get("mask"), highs=basis.get("highs"),
                            min_windows=basis.get("min_windows", MIN_WINDOWS))
        if measured:
            measured["conditioning"] = basis.get("label", "all windows")
            return measured
    return None


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


def wilson_interval(hits: float, windows: float, z: float = 1.96):
    """95% Wilson score interval for a proportion.

    Accepts fractional evidence: recency weighting and shrinkage produce a
    real-valued effective sample, and the arithmetic is identical.

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
    conditioning = measured.get("conditioning")
    scope = f" {conditioning}" if conditioning and conditioning != "all windows" else " past"
    basis = (", intraday-touch" if measured.get("touch_basis") == "intraday-high" else "")
    n_eff = measured.get("n_eff")
    recency = ""
    if measured.get("recency_half_life_bars") and n_eff and n_eff < measured["windows"] * 0.95:
        recency = f" · recency-weighted (n_eff {n_eff:.0f})"
    return (f"{measured['hits']}/{measured['windows']}{scope} "
            f"{measured['horizon_bars']}-day windows{basis}{recency}")


def annotate_targets(closes: np.ndarray, targets: Dict[str, dict], band: str,
                     bases: Optional[List[dict]] = None,
                     cohort_prior: Optional[float] = None) -> Dict[str, dict]:
    """Attach an empirical probability to each target in a timeframe band.

    Targets themselves are real -- previous close, moving averages, analyst
    consensus. Only the probability attached to them was invented, so only that
    is replaced here.

    `bases` is the evidence ladder for best_hit_rate; without one, the plain
    five-year close-basis rate is used, which is the original behaviour.
    `cohort_prior` (0-1) shrinks thin samples toward the cohort's rate for
    similar targets; both the raw and shrunk figures are reported.
    """
    horizon = HORIZON_BARS.get(band, 21)
    if not bases:
        bases = [{"closes": closes, "label": "all windows"}]
    annotated = {}
    for name, target in targets.items():
        upside = target.get("upside_percent")
        entry = dict(target)

        if upside is None:
            entry["probability_available"] = False
            entry["probability_reason"] = "no upside computed"
            annotated[name] = entry
            continue

        if upside <= 0:
            sourced = Sourced.unavailable(
                "empirical:price-history", "target is at or below the current price")
        else:
            measured_best = best_hit_rate(bases, upside, horizon)
            if measured_best:
                sourced = Sourced.live(measured_best, "empirical:price-history")
            else:
                bars = 0 if closes is None else len(closes)
                sourced = Sourced.unavailable(
                    "empirical:price-history",
                    f"only {bars} trading days of history; measuring a "
                    f"{horizon}-day window needs at least {horizon + MIN_WINDOWS}")
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

            # The interval reflects the evidence actually carrying the rate:
            # the recency-weighted effective sample, not the raw window count.
            n_eff = measured.get("n_eff") or measured["windows"]
            probability = measured["probability"]
            evidence = describe(measured)
            expected_value = measured.get("expected_value")
            # cohort_prior may be a float or a callable of the target's upside,
            # since the right prior depends on how far the target sits.
            prior_p = cohort_prior(upside) if callable(cohort_prior) else cohort_prior
            if prior_p is not None:
                shrunk = shrink_toward_rate(probability, n_eff, prior_p)
                # Compare what the display will actually show: rates that
                # round to different one-decimal percentages must both appear.
                raw_pct = round(probability * 100, 1)
                if raw_pct != round(shrunk * 100, 1):
                    entry["probability_raw"] = raw_pct
                    evidence += (f" · shrunk toward cohort {prior_p * 100:.0f}% "
                                 f"for similar targets (raw {probability * 100:.0f}%)")
                probability = shrunk
                miss_median = measured.get("miss_median_return")
                if miss_median is not None:
                    expected_value = round(
                        probability * upside + (1 - probability) * miss_median, 2)
            # The interval belongs to the probability the page displays. After
            # shrinkage that is the posterior rate over the posterior evidence
            # (n_eff plus the prior's pseudo-windows); without shrinkage it is
            # the weighted rate over n_eff. Computing it before shrinkage
            # paired a shrunk number with raw-rate bounds (CR, PR 50).
            ci_n = n_eff + (SHRINK_PRIOR_WEIGHT if prior_p is not None else 0)
            # Fractional evidence stays fractional: rounding the effective
            # sample would compute bounds for a different rate than displayed.
            ci_low, ci_high = wilson_interval(probability * ci_n, max(1.0, ci_n))
            entry.update({
                "probability_available": True,
                "expected_value": expected_value,
                "miss_median_return": measured.get("miss_median_return"),
                "probability": round(probability * 100, 1),
                "ci_low": round(ci_low * 100, 1),
                "ci_high": round(ci_high * 100, 1),
                "evidence": evidence,
                "conditioning": measured.get("conditioning"),
                "touch_basis": measured.get("touch_basis"),
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
