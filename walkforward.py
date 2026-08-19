"""Walk-forward fitting of the factor weights against realized returns.

The live score's six weights are hand-chosen. Once enough snapshot days have
accumulated, this module fits weights on past days only and evaluates them on
strictly later days -- the out-of-sample discipline that separates a fitted
model from a curve-fit story. Report-only by design: the fitted weights are
published next to the hand-chosen ones with their out-of-sample performance,
and nothing changes in the live score until a human decides it should.
"""

import logging
from datetime import date

import numpy as np

import tracking

logger = logging.getLogger(__name__)

# Below this many distinct snapshot days with resolved forward returns, any
# fit would be noise dressed as insight. The page shows the count instead.
MIN_FIT_DAYS = 20
FIT_HORIZON = 7          # calendar days, matching tracking.HORIZONS[0]
RIDGE_LAMBDA = 1.0       # small L2 so a collinear factor cannot explode


def _training_rows(directory=None):
    """One dict per scored symbol: day, factor scores, forward return, resolved_on.

    Only rows whose 7-day forward return is resolvable from later snapshots
    qualify; unresolved rows are simply not training data yet.
    """
    snapshots = tracking.load_snapshots(directory)
    by_date = {}
    for snap in snapshots:
        try:
            by_date[date.fromisoformat(snap["date"])] = snap
        except ValueError:
            continue
    ordered = sorted(by_date)

    rows = []
    for snap_date in ordered:
        for row in by_date[snap_date].get("universe", []):
            symbol, entry = row.get("symbol"), row.get("price")
            factors = row.get("factors") or {}
            if not symbol or not entry or not factors:
                continue
            target = snap_date.toordinal() + FIT_HORIZON
            best = None
            for later in ordered:
                if later <= snap_date:
                    continue
                gap = abs(later.toordinal() - target)
                if gap <= max(2, int(FIT_HORIZON * 0.4)):
                    price = tracking.price_on(by_date[later], symbol)
                    if price and (best is None or gap < best[0]):
                        best = (gap, price, later)
            if best is None:
                continue
            fwd_return = (best[1] - entry) / entry * 100.0
            scores = {key: value.get("score") for key, value in factors.items()
                      if isinstance(value, dict) and isinstance(value.get("score"), (int, float))}
            if scores:
                # resolved_on rides along so the fit can honour the strict
                # out-of-sample boundary: a return is only usable as training
                # data once the snapshot that RESOLVED it exists.
                rows.append({"day": snap_date, "scores": scores,
                             "fwd_return": fwd_return, "resolved_on": best[2]})
    return rows


def _fit_ridge(matrix, returns):
    """Closed-form ridge regression; returns the weight vector."""
    identity = np.eye(matrix.shape[1])
    return np.linalg.solve(matrix.T @ matrix + RIDGE_LAMBDA * identity,
                           matrix.T @ returns)


def walk_forward(directory=None):
    """Fit on the past, test on the future, one day at a time.

    For each test day after the warm-up, weights are fitted on all strictly
    earlier days, and that day's symbols are ranked by fitted score. The
    reported number is the mean forward return of each day's top-3 fitted
    picks versus an equal-weight top-3 baseline -- the comparison a weight
    change would have to win before anyone should believe in it.
    """
    rows = _training_rows(directory)
    days = sorted({r["day"] for r in rows})
    result = {
        "fit_days_available": len(days),
        "min_days_required": MIN_FIT_DAYS,
        "ready": len(days) >= MIN_FIT_DAYS,
        "horizon_days": FIT_HORIZON,
    }
    if not result["ready"]:
        result["status"] = (f"collecting: {len(days)} of {MIN_FIT_DAYS} snapshot days "
                            "with resolved forward returns")
        return result

    factor_keys = sorted({key for r in rows for key in r["scores"]})

    def vectorize(scores):
        # 50.0 stands in for an absent factor; the imputation share is
        # counted and published so a fit built mostly on filler says so.
        return [scores.get(key, 50.0) / 100.0 for key in factor_keys]

    imputed = sum(1 for r in rows for key in factor_keys if key not in r["scores"])
    total_cells = max(1, len(rows) * len(factor_keys))

    fitted_daily, equal_daily = [], []
    weights = None
    # Strict out-of-sample, both ways the leak can happen: testing starts
    # only after MIN_FIT_DAYS full days exist, and a training row qualifies
    # only when the snapshot that RESOLVED its forward return predates the
    # test day -- otherwise future prices leak into the fitted weights.
    for test_day in days[MIN_FIT_DAYS:]:
        train = [r for r in rows
                 if r["day"] < test_day and r["resolved_on"] < test_day]
        test = [r for r in rows if r["day"] == test_day]
        if len({r["day"] for r in train}) < MIN_FIT_DAYS or not test:
            continue
        matrix = np.array([vectorize(r["scores"]) for r in train])
        returns = np.array([r["fwd_return"] for r in train])
        # Centered fit: the intercept (mean return) is absorbed before the
        # ridge solve, so factor weights measure deviation from the average
        # day instead of also carrying the market's base drift.
        return_mean = returns.mean()
        matrix_means = matrix.mean(axis=0)
        weights = _fit_ridge(matrix - matrix_means, returns - return_mean)
        by_fitted = sorted(test, key=lambda r: float(
            np.dot(np.array(vectorize(r["scores"])) - matrix_means, weights)),
            reverse=True)[:3]
        # Equal-weight mean, and labelled as such: reproducing the live
        # scorer's renormalized weights over historical factor rows would
        # claim a fidelity the snapshot data cannot verify.
        by_equal = sorted(test, key=lambda r: sum(r["scores"].values()) / len(r["scores"]),
                          reverse=True)[:3]
        fitted_daily.append(sum(r["fwd_return"] for r in by_fitted) / len(by_fitted))
        equal_daily.append(sum(r["fwd_return"] for r in by_equal) / len(by_equal))

    if not fitted_daily:
        result["ready"] = False
        result["status"] = "no out-of-sample test days resolvable yet"
        return result

    result.update({
        "test_days": len(fitted_daily),
        "fitted_top3_mean_return": round(float(np.mean(fitted_daily)), 2),
        "equal_weight_top3_mean_return": round(float(np.mean(equal_daily)), 2),
        "latest_weights": {key: round(float(w), 4)
                           for key, w in zip(factor_keys, weights)},
        "imputed_factor_share": round(imputed / total_cells, 3),
        "status": "report-only: live scoring still uses the hand-chosen weights",
    })
    return result
