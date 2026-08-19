"""Daily snapshots and the live track record computed from them.

The model's largest honesty gap is that most of its factors have no historical
record to validate against. This module closes it going forward: once a day a
GitHub Action calls /api/snapshot and commits the result to data/snapshots/ in
the repository. Git history makes the record tamper-evident -- a past
recommendation cannot be quietly rewritten without leaving a diff -- and the
deployed app ships with its own history, which /track-record turns into
realized forward returns.

Day one the page honestly says "no forward returns yet". That is the point:
the numbers only ever come from recommendations the app actually logged.
"""

import glob
import json
import logging
import os
from datetime import date, datetime, timezone

import pytz

# The record is keyed to the trading day, and trading days are US-Eastern.
# The server clock is UTC, so date.today() after 8 PM Eastern names tomorrow --
# which is how the evening snapshot of the 08-17 session got filed as 08-18.
EASTERN = pytz.timezone("America/New_York")


def trading_date_today() -> date:
    """Today's date on the exchange clock, not the server clock."""
    return datetime.now(EASTERN).date()

logger = logging.getLogger(__name__)

MODEL_VERSION = "3.1"
SNAPSHOT_DIR = os.environ.get("SNAPSHOT_DIR", os.path.join(os.path.dirname(__file__), "data", "snapshots"))

# Forward-return horizons, in calendar days between snapshots (trading-day
# precision would need an exchange calendar; nearest-snapshot matching keeps
# the arithmetic simple and the approximation is stated on the page).
HORIZONS = (7, 30)

# A pick is anything at or above the model's "strong" boundary.
PICK_SCORE = 70.0


def _load_snapshots(directory=None):
    """All committed snapshots, oldest first. Malformed files are skipped loudly."""
    directory = directory or SNAPSHOT_DIR
    snapshots = []
    for path in sorted(glob.glob(os.path.join(directory, "*.json"))):
        try:
            with open(path, encoding="utf-8") as handle:
                snap = json.load(handle)
            if snap.get("date") and isinstance(snap.get("universe"), list):
                snapshots.append(snap)
            else:
                logger.warning(f"snapshot {os.path.basename(path)} missing required keys; skipped")
        except (OSError, ValueError) as e:
            logger.warning(f"snapshot {os.path.basename(path)} unreadable: {type(e).__name__}")
    return snapshots


def _price_on(snapshot, symbol):
    """A symbol's price in a snapshot: from its universe row or tracked prices."""
    for row in snapshot.get("universe", []):
        if row.get("symbol") == symbol and row.get("price"):
            return float(row["price"])
    tracked = snapshot.get("tracked_prices") or {}
    value = tracked.get(symbol)
    return float(value) if value else None


# Public aliases: walkforward.py consumes the same snapshot store, and the
# underscore names would make that coupling look accidental.
def load_snapshots(directory=None):
    return _load_snapshots(directory)


def price_on(snapshot, symbol):
    return _price_on(snapshot, symbol)


def build_snapshot(universe_rows, tracked_prices):
    """Assemble one day's record. Caller supplies scored rows and price map."""
    return {
        "date": trading_date_today().isoformat(),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "generated_at_eastern": datetime.now(EASTERN).strftime("%Y-%m-%d %I:%M %p %Z"),
        "model_version": MODEL_VERSION,
        "universe": universe_rows,
        "tracked_prices": tracked_prices,
        "note": ("Recorded so every factor -- including those with no public "
                 "historical archive -- accumulates an auditable record. "
                 "Committed to git; history is tamper-evident."),
    }


def tracked_symbols(directory=None, lookback_days=70):
    """Symbols whose forward prices the next snapshot should carry."""
    cutoff = None
    symbols = set()
    for snap in _load_snapshots(directory):
        try:
            snap_date = date.fromisoformat(snap["date"])
        except ValueError:
            continue
        if cutoff is None:
            cutoff = trading_date_today().toordinal() - lookback_days
        if snap_date.toordinal() < cutoff:
            continue
        for row in snap.get("universe", []):
            if row.get("symbol"):
                symbols.add(row["symbol"])
    return sorted(symbols)


def compute_track_record(directory=None):
    """Join every logged pick with the prices later snapshots recorded.

    Returns per-horizon aggregates for picks (score >= PICK_SCORE) and for the
    rest of the universe as the baseline, plus the individual pick rows so the
    page can show its work. Picks without a later price yet are counted as
    pending, never dropped silently.
    """
    snapshots = _load_snapshots(directory)
    result = {
        "model_version": MODEL_VERSION,
        "pick_threshold": PICK_SCORE,
        "snapshot_days": len(snapshots),
        "first_date": snapshots[0]["date"] if snapshots else None,
        "last_date": snapshots[-1]["date"] if snapshots else None,
        "horizons": {},
        "picks": [],
        "pending": 0,
    }
    if not snapshots:
        return result

    by_date = {}
    for snap in snapshots:
        try:
            by_date[date.fromisoformat(snap["date"])] = snap
        except ValueError:
            continue
    ordered_dates = sorted(by_date)

    def later_price(entry_date, symbol, horizon):
        """Price at the snapshot nearest to entry_date + horizon (within +/-40%)."""
        target = entry_date.toordinal() + horizon
        best, best_gap = None, None
        for d in ordered_dates:
            if d <= entry_date:
                continue
            gap = abs(d.toordinal() - target)
            if gap <= max(2, int(horizon * 0.4)):
                price = _price_on(by_date[d], symbol)
                if price and (best_gap is None or gap < best_gap):
                    best, best_gap = (price, d), gap
        return best

    horizon_rows = {h: {"picks": [], "baseline": []} for h in HORIZONS}

    for snap_date in ordered_dates:
        snap = by_date[snap_date]
        for row in snap.get("universe", []):
            symbol, entry = row.get("symbol"), row.get("price")
            score = row.get("score")
            if not symbol or not entry:
                continue
            is_pick = isinstance(score, (int, float)) and score >= PICK_SCORE
            resolved_any = False
            pick_row = {"date": snap.get("date"), "symbol": symbol, "score": score,
                        "entry": entry, "returns": {}}
            for horizon in HORIZONS:
                hit = later_price(snap_date, symbol, horizon)
                if not hit:
                    continue
                resolved_any = True
                price, at = hit
                ret = (price - entry) / entry * 100.0
                entry_row = {"pct": round(ret, 2), "as_of": at.isoformat()}
                # Same-span SPY return, when both endpoints recorded it. A pick
                # that beat its own history but trailed the market is a worse
                # trade than the raw number suggests.
                spy_entry = _price_on(snap, "SPY")
                spy_exit = _price_on(by_date[at], "SPY")
                if spy_entry and spy_exit:
                    spy_ret = (spy_exit - spy_entry) / spy_entry * 100.0
                    entry_row["vs_spy"] = round(ret - spy_ret, 2)
                    if is_pick:
                        horizon_rows[horizon].setdefault("vs_spy", []).append(ret - spy_ret)
                pick_row["returns"][str(horizon)] = entry_row
                bucket = "picks" if is_pick else "baseline"
                horizon_rows[horizon][bucket].append(ret)
            if is_pick:
                if not resolved_any:
                    result["pending"] += 1
                result["picks"].append(pick_row)

    for horizon in HORIZONS:
        picks = horizon_rows[horizon]["picks"]
        base = horizon_rows[horizon]["baseline"]
        entry = {"n_picks": len(picks), "n_baseline": len(base)}
        if picks:
            entry["picks_mean"] = round(sum(picks) / len(picks), 2)
            entry["picks_win_rate"] = round(sum(1 for r in picks if r > 0) / len(picks), 3)
        if base:
            entry["baseline_mean"] = round(sum(base) / len(base), 2)
        if picks and base:
            entry["excess"] = round(entry["picks_mean"] - entry["baseline_mean"], 2)
        spy_rows = horizon_rows[horizon].get("vs_spy") or []
        if spy_rows:
            entry["vs_spy_mean"] = round(sum(spy_rows) / len(spy_rows), 2)
            entry["n_vs_spy"] = len(spy_rows)
        result["horizons"][str(horizon)] = entry

    # Newest first for display; cap so the page stays light.
    result["picks"] = sorted(result["picks"], key=lambda p: p["date"], reverse=True)[:200]
    return result


# Calibration needs enough resolved predictions to say anything; below this
# the page reports "collecting" with the honest counts instead of a curve.
MIN_CALIBRATION_RESOLVED = 20
CALIBRATION_BUCKETS = ((0, 20), (20, 40), (40, 60), (60, 80), (80, 101))


def _default_highs_lookup(symbol):
    """Cached intraday highs for grading, as (dates, highs) or None.

    Reads the same OHLCV entries the warmer maintains. Import is local and
    failure-tolerant so the tracking module stays usable standalone.
    """
    try:
        import market_data
        payload = market_data._cache.get(f"ohlcv:{symbol.upper()}:1y")
    except Exception:
        return None
    if not (payload and payload.get("ok")):
        return None
    dates = [d[:10] for d in (payload.get("index") or [])]
    highs = payload.get("high") or []
    if len(dates) != len(highs) or not dates:
        return None
    return dates, highs


def compute_calibration(directory=None, highs_lookup=_default_highs_lookup):
    """Were the published probabilities right? Predicted vs realized, plus Brier.

    Each snapshot row may carry `predictions`: the empirical hit-rate odds the
    app displayed that day ({name: {probability, target_pct, horizon_days}}).
    A prediction resolves against later evidence: the predictions claim
    touches, so grading consults cached intraday highs inside the window
    first, then later snapshots' closes. Windows graded without high data are
    close-graded and slightly conservative; both counts are reported.
    """
    snapshots = _load_snapshots(directory)
    by_date = {}
    for snap in snapshots:
        try:
            by_date[date.fromisoformat(snap["date"])] = snap
        except ValueError:
            continue
    ordered_dates = sorted(by_date)

    # One pass builds a date -> {symbol: price} index; the resolution scan
    # below is quadratic-ish in snapshots x universe and previously re-walked
    # each universe list per lookup.
    prices_by_date = {}
    for snap_date, snap in by_date.items():
        table = {}
        for row in snap.get("universe", []):
            if row.get("symbol") and row.get("price"):
                table[row["symbol"]] = float(row["price"])
        for symbol, value in (snap.get("tracked_prices") or {}).items():
            if value:
                table.setdefault(symbol, float(value))
        prices_by_date[snap_date] = table

    pairs = []          # (predicted probability 0-1, hit 0/1)
    unresolved = 0
    graded_on_highs = 0
    highs_cache = {}

    def window_high_verdict(symbol, start_iso, end_iso, threshold):
        """Grade one window from cached intraday highs.

        "hit": a high inside the window reached the threshold. "miss": highs
        were observed across the whole window (the series extends past its
        end) and none reached it -- highs bound closes, so this is final.
        "partial": some in-window highs seen, none touched, but the series
        stops before the window ends. None: no in-window high data at all.
        """
        if symbol not in highs_cache:
            highs_cache[symbol] = highs_lookup(symbol) if highs_lookup else None
        series = highs_cache[symbol]
        if not series:
            return None
        dates, highs = series
        seen = extends_past_end = False
        for d, h in zip(dates, highs):
            if d > end_iso:
                extends_past_end = True
                continue
            if h is not None and start_iso < d <= end_iso:
                seen = True
                if h >= threshold:
                    return "hit"
        if not seen:
            return None
        return "miss" if extends_past_end else "partial"

    for snap_date in ordered_dates:
        for row in by_date[snap_date].get("universe", []):
            symbol, entry = row.get("symbol"), row.get("price")
            predictions = row.get("predictions") or {}
            if not symbol or not entry or not predictions:
                continue
            for pred in predictions.values():
                prob = pred.get("probability")
                target = pred.get("target_pct")
                horizon = pred.get("horizon_days")
                if prob is None or target is None or not horizon:
                    continue
                threshold = entry * (1 + target / 100.0)
                end_ordinal = snap_date.toordinal() + horizon
                hit = window_elapsed = price_observed = high_graded = False

                # Highs first: the prediction claimed a TOUCH, and intraday
                # highs are the matching evidence -- they credit touches the
                # daily closes missed, and a full-window high series that
                # never touched is a final miss (highs bound closes). Snapshot
                # closes are the fallback, not the primary.
                verdict = window_high_verdict(
                    symbol, snap_date.isoformat(),
                    date.fromordinal(end_ordinal).isoformat(), threshold)
                if verdict == "hit":
                    hit = high_graded = True
                elif verdict == "miss":
                    price_observed = window_elapsed = high_graded = True
                else:
                    if verdict == "partial":
                        price_observed = high_graded = True
                    for later in ordered_dates:
                        if later <= snap_date:
                            continue
                        if later.toordinal() > end_ordinal:
                            window_elapsed = True   # a snapshot exists past the window
                            break
                        price = prices_by_date[later].get(symbol)
                        if price is not None:
                            price_observed = True
                            if price >= threshold:
                                hit = True
                                high_graded = False  # the close, not a high, decided it
                                break
                        # 80% of the window observed counts as resolved-enough.
                        if later.toordinal() >= snap_date.toordinal() + horizon * 0.8:
                            window_elapsed = True
                # A miss must be supported by at least one recorded in-window
                # price. A symbol that vanished from every later snapshot has
                # no observed outcome, and grading it would fabricate one.
                if hit or (window_elapsed and price_observed):
                    pairs.append((float(prob), 1 if hit else 0))
                    if high_graded:
                        graded_on_highs += 1  # counted only when resolved
                else:
                    unresolved += 1

    result = {
        "n_resolved": len(pairs),
        "n_unresolved": unresolved,
        "n_graded_on_highs": graded_on_highs,
        "min_required": MIN_CALIBRATION_RESOLVED,
        "ready": len(pairs) >= MIN_CALIBRATION_RESOLVED,
        "buckets": [],
        "brier": None,
        "note": ("predictions claim touches, so grading consults cached intraday "
                 "highs inside each window first, then snapshot closes; windows "
                 "graded without high data are close-graded and slightly "
                 "conservative"),
    }
    if not pairs:
        return result

    result["brier"] = round(sum((p - h) ** 2 for p, h in pairs) / len(pairs), 4)
    for low, high in CALIBRATION_BUCKETS:
        rows = [(p, h) for p, h in pairs if low <= p * 100 < high]
        bucket = {"range": f"{low}-{min(high, 100)}%", "n": len(rows)}
        if rows:
            bucket["predicted_mean"] = round(sum(p for p, _ in rows) / len(rows) * 100, 1)
            bucket["realized_rate"] = round(sum(h for _, h in rows) / len(rows) * 100, 1)
        result["buckets"].append(bucket)
    return result
