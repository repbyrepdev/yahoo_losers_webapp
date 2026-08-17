"""Cached access to real market data via yfinance.

Yahoo's v7 and v10 REST endpoints began requiring authentication and now return
401 to unauthenticated callers. yfinance performs the crumb/cookie handshake
those endpoints expect, so it reaches the same data. Every accessor here returns
`Sourced` values, so a provider outage surfaces as unavailable rather than being
absorbed into a substituted number.

Caching matters operationally: the app runs on a 0.5 CPU / 512 MB instance and a
refresh touches ~25 symbols. TTLs are set by how fast each field actually moves,
so a page refresh does not re-fetch slow-moving fundamentals.
"""

import logging
import os
import random
import threading
import time
from datetime import datetime
from typing import Dict, List, Optional

import yfinance as yf

from provenance import Sourced

logger = logging.getLogger(__name__)

# Bump when a cached payload's shape changes, so old entries in a shared Redis
# are ignored instead of being deserialised into code that no longer matches.
CACHE_SCHEMA_VERSION = "v1"

# How long each kind of data stays fresh during market hours, in seconds.
# These are floors: _effective_ttl extends them while the market is closed.
TTL_QUOTE = 5 * 60
TTL_TECHNICALS = 30 * 60      # derived from daily bars; only the last bar moves
TTL_OPTIONS = 15 * 60
TTL_NEWS = 60 * 60
TTL_EARNINGS = 24 * 60 * 60
TTL_TARGETS = 6 * 60 * 60     # analyst revisions are episodic, not continuous
TTL_PROFILE = 7 * 24 * 60 * 60

# A fetch that failed because of an outage should be retried soon. A fetch that
# "failed" because the security genuinely has no options chain or no analyst
# coverage is a stable fact, and re-asking every minute is pure waste.
TTL_NEGATIVE_TRANSIENT = 60
TTL_NEGATIVE_STRUCTURAL = 6 * 60 * 60

# Yahoo rejects bursts from datacenter IPs. Treating that as an ordinary
# failure is actively harmful: the 60s negative TTL means every page render
# retries the whole universe, which keeps the limiter engaged indefinitely.
_RATE_LIMIT_MARKERS = ("ratelimit", "rate limit", "too many requests", "429")

# Back off for a meaningful interval once limited, so the app stops adding
# load to a provider that is already refusing it.
TTL_RATE_LIMITED = 15 * 60

# Minimum gap between outbound provider calls, process-wide. Yahoo tolerates a
# steady trickle far better than a burst, and the cache means most page renders
# never reach this path at all.
MIN_CALL_INTERVAL_SECONDS = float(os.environ.get("MARKET_DATA_MIN_INTERVAL", 0.35))

_throttle_lock = threading.Lock()
_last_call_at = [0.0]


def _throttle():
    """Space out provider calls process-wide."""
    with _throttle_lock:
        elapsed = time.time() - _last_call_at[0]
        wait = MIN_CALL_INTERVAL_SECONDS - elapsed
        if wait > 0:
            time.sleep(wait)
        _last_call_at[0] = time.time()


def _is_rate_limited(reason: str) -> bool:
    lowered = (reason or "").lower()
    return any(marker in lowered for marker in _RATE_LIMIT_MARKERS)


_STRUCTURAL_MARKERS = (
    "no analyst coverage", "no listed options", "no 13F holders",
    "no ratings published", "insufficient history", "no earnings date published",
    "only ", "empty chain", "no recent headlines",
)


def _is_structural(reason: str) -> bool:
    """True when a failure reflects the security itself, not a provider outage."""
    lowered = (reason or "").lower()
    return any(marker in lowered for marker in _STRUCTURAL_MARKERS)


def market_is_open() -> bool:
    """Rough NYSE session check, used only to decide cache lifetimes.

    Holidays are deliberately not modelled. Being wrong on a holiday means
    caching for longer than necessary, which is harmless; the inverse error
    would mean serving stale prices during a live session.
    """
    try:
        import pytz

        now = datetime.now(pytz.timezone("America/New_York"))
    except Exception:
        now = datetime.now()
    if now.weekday() >= 5:
        return False
    minutes = now.hour * 60 + now.minute
    return 9 * 60 + 30 <= minutes <= 16 * 60


def _effective_ttl(base_ttl: int) -> int:
    """Stretch TTLs when the market is closed, with jitter to avoid stampedes.

    Prices do not move overnight or at weekends, so refetching then spends the
    instance's small request budget for nothing. Jitter keeps 25 symbols from
    all expiring in the same second and stampeding the provider.
    """
    ttl = base_ttl if market_is_open() else min(base_ttl * 8, 12 * 60 * 60)
    return max(30, int(ttl * random.uniform(0.9, 1.1)))

# An analyst "consensus" drawn from one or two estimates is noise, not consensus.
MIN_ANALYSTS_FOR_CONSENSUS = 3


class TTLCache:
    """Small thread-safe TTL cache.

    Redis is used when reachable so the app's two workers share entries;
    otherwise this degrades to a per-process dict, which is correct but colder.
    """

    def __init__(self, redis_url: Optional[str] = None):
        self._local: Dict[str, tuple] = {}
        self._lock = threading.Lock()
        self._redis = None
        if redis_url:
            try:
                import redis

                client = redis.from_url(redis_url, socket_timeout=2, decode_responses=True)
                client.ping()
                self._redis = client
                logger.info("market_data cache using Redis")
            except Exception as e:
                logger.info(f"market_data cache falling back to memory: {type(e).__name__}")

    def _key(self, key: str) -> str:
        return f"md:{CACHE_SCHEMA_VERSION}:{key}"

    def get(self, key: str):
        if self._redis is not None:
            try:
                import json

                raw = self._redis.get(self._key(key))
                if raw is not None:
                    return json.loads(raw)
            except Exception:
                pass
        with self._lock:
            entry = self._local.get(key)
        if entry and entry[0] > time.time():
            return entry[1]
        if entry:
            # Expired locally; drop it so the dict cannot grow without bound in
            # a long-lived worker.
            with self._lock:
                self._local.pop(key, None)
        return None

    def set(self, key: str, value, ttl: int):
        if self._redis is not None:
            try:
                import json

                self._redis.setex(self._key(key), ttl, json.dumps(value, default=str))
            except Exception:
                pass
        with self._lock:
            self._local[key] = (time.time() + ttl, value)


_cache = TTLCache(os.environ.get("REDIS_URL"))


def _cached(key: str, ttl: int, producer, allow_fetch: bool = True):
    """Return a cached payload, or produce and store one.

    Three lifetimes apply. Successes use the market-aware TTL. Failures caused
    by an outage are retried within a minute. Failures that describe the
    security itself -- no options listed, no analyst coverage -- are held far
    longer, because re-asking Yahoo every minute whether a micro-cap has an
    options chain is a guaranteed-negative request repeated forever.
    """
    hit = _cache.get(key)
    if hit is not None:
        return hit

    if not allow_fetch:
        return {"ok": False, "reason": "not fetched yet (opened on the detail view)"}

    # One retry, because Yahoo's limiter often clears within a second or two
    # and a single symbol failing starves the whole score of that factor.
    value = None
    for attempt in range(2):
        _throttle()
        try:
            value = producer()
            break
        except Exception as e:
            name = type(e).__name__
            detail = f"{name}: {e}"
            if _is_rate_limited(detail) and attempt == 0:
                time.sleep(1.5 + random.random())
                continue
            logger.warning(f"market_data fetch failed for {key}: {detail}")
            value = {"ok": False, "reason": name}
            break

    if value.get("ok"):
        lifetime = _effective_ttl(ttl)
    elif _is_rate_limited(value.get("reason", "")):
        lifetime = TTL_RATE_LIMITED
    elif _is_structural(value.get("reason", "")):
        lifetime = _effective_ttl(TTL_NEGATIVE_STRUCTURAL)
    else:
        lifetime = TTL_NEGATIVE_TRANSIENT

    _cache.set(key, value, lifetime)
    return value


def _ticker(symbol: str) -> yf.Ticker:
    return yf.Ticker(symbol.upper())


def _info(symbol: str) -> dict:
    """One `.info` call backs targets, sector, short interest and ownership.

    Deliberately cached as a single blob under the shortest of the lifetimes it
    serves (targets). Splitting sector onto its own 7-day TTL would look tidier
    but would cost a second network round trip per symbol to save nothing --
    the call has to happen for the targets regardless.
    """

    def produce():
        info = _ticker(symbol).info or {}
        if not info.get("symbol") and not info.get("regularMarketPrice"):
            return {"ok": False, "reason": "no profile returned"}
        return {
            "ok": True,
            "target_mean": info.get("targetMeanPrice"),
            "target_high": info.get("targetHighPrice"),
            "target_low": info.get("targetLowPrice"),
            "analysts": info.get("numberOfAnalystOpinions"),
            "recommendation": info.get("recommendationKey"),
            "name": info.get("longName") or info.get("shortName"),
            "sector": info.get("sector"),
            "industry": info.get("industry"),
            "short_pct_float": info.get("shortPercentOfFloat"),
            "shares_short": info.get("sharesShort"),
            "held_pct_institutions": info.get("heldPercentInstitutions"),
            "avg_volume": info.get("averageVolume"),
            "market_cap": info.get("marketCap"),
            "trailing_pe": info.get("trailingPE"),
            "current_price": info.get("currentPrice") or info.get("regularMarketPrice"),
            "fifty_two_week_low": info.get("fiftyTwoWeekLow"),
            "fifty_two_week_high": info.get("fiftyTwoWeekHigh"),
        }

    return _cached(f"info:{symbol.upper()}", TTL_TARGETS, produce)


def analyst_target(symbol: str) -> Dict[str, Sourced]:
    """Analyst consensus target, with the spread and the count behind it.

    The count is returned alongside the number because "$322.28" and
    "$322.28 across 41 analysts" are different claims.
    """
    info = _info(symbol)
    source = "yfinance:targetMeanPrice"

    if not info.get("ok"):
        reason = info.get("reason", "unavailable")
        return {
            "mean": Sourced.unavailable(source, reason),
            "high": Sourced.unavailable(source, reason),
            "low": Sourced.unavailable(source, reason),
            "analysts": Sourced.unavailable(source, reason),
        }

    mean, count = info.get("target_mean"), info.get("analysts")
    if not mean:
        gap = "no analyst coverage published"
        return {
            "mean": Sourced.unavailable(source, gap),
            "high": Sourced.unavailable(source, gap),
            "low": Sourced.unavailable(source, gap),
            "analysts": Sourced.live(count or 0, source),
        }
    if not count or count < MIN_ANALYSTS_FOR_CONSENSUS:
        thin = f"only {count or 0} analyst estimate(s)"
        return {
            "mean": Sourced.unavailable(source, thin),
            "high": Sourced.unavailable(source, thin),
            "low": Sourced.unavailable(source, thin),
            "analysts": Sourced.live(count or 0, source),
        }

    return {
        "mean": Sourced.live(mean, source),
        "high": Sourced.live(info.get("target_high"), source) if info.get("target_high") else Sourced.unavailable(source, "no high estimate"),
        "low": Sourced.live(info.get("target_low"), source) if info.get("target_low") else Sourced.unavailable(source, "no low estimate"),
        "analysts": Sourced.live(count, source),
    }


def profile(symbol: str) -> Dict[str, Sourced]:
    """Sector, industry, short interest and institutional ownership."""
    info = _info(symbol)
    source = "yfinance:info"
    if not info.get("ok"):
        reason = info.get("reason", "unavailable")
        return {k: Sourced.unavailable(source, reason) for k in
                ("sector", "industry", "short_pct_float", "held_pct_institutions", "avg_volume")}

    def field(key):
        value = info.get(key)
        return Sourced.live(value, source) if value is not None else Sourced.unavailable(source, "not reported")

    # Yahoo sometimes reports institutional ownership above 100% (WING returns
    # 1.257). That is a real artifact of shares lent out being counted by both
    # lender and borrower, not a fetch error -- but rendering "126% owned by
    # institutions" as a clean fact is misleading, so it is flagged.
    held = info.get("held_pct_institutions")
    if held is not None and held > 1.0:
        held_sourced = Sourced.derived(
            {"value": held, "note": "exceeds 100%; share lending is double-counted in 13F aggregates"},
            "yfinance:heldPercentInstitutions",
        )
    else:
        held_sourced = field("held_pct_institutions")

    return {
        "name": field("name"),
        "sector": field("sector"),
        "industry": field("industry"),
        "short_pct_float": field("short_pct_float"),
        "held_pct_institutions": held_sourced,
        "avg_volume": field("avg_volume"),
    }


def earnings_date(symbol: str) -> Sourced:
    """Earnings date, explicitly flagged as upcoming or already reported.

    Yahoo returns the most recent earnings date when the next one is not yet
    scheduled, so the raw value cannot be labelled "next earnings" without
    checking it. WING, for example, returns a date already in the past.

    Yahoo also returns a range when a date is estimated rather than confirmed,
    which matters to anyone trading around the event, so the range is preserved
    instead of being flattened to a single day.
    """
    source = "yfinance:calendar"

    def produce():
        calendar = _ticker(symbol).calendar or {}
        dates = calendar.get("Earnings Date") or []
        if not dates:
            return {"ok": False, "reason": "no earnings date published"}
        return {
            "ok": True,
            "dates": [d.isoformat() for d in dates],
            "confirmed": len(dates) == 1,
        }

    payload = _cached(f"earnings:{symbol.upper()}", TTL_EARNINGS, produce)
    if not payload.get("ok"):
        return Sourced.unavailable(source, payload.get("reason", "unavailable"))

    from datetime import date as _date

    today = _date.today()
    dates = payload["dates"]
    last_day = _date.fromisoformat(dates[-1])
    upcoming = last_day >= today

    value = {
        "date": dates[0],
        "through": dates[-1] if len(dates) > 1 else None,
        "upcoming": upcoming,
        "confirmed": payload["confirmed"],
        "days_away": (last_day - today).days if upcoming else None,
        "label": ("Next earnings" if payload["confirmed"] else "Next earnings (estimated window)")
                 if upcoming else "Last reported",
    }
    # An unconfirmed window is an estimate by Yahoo's own admission.
    return Sourced.live(value, source) if payload["confirmed"] else Sourced.derived(value, "yfinance:calendar-window")


def headlines(symbol: str, limit: int = 5) -> Sourced:
    """Recent news headlines. Real titles, not a paraphrase."""
    source = "yfinance:news"

    def produce():
        items = _ticker(symbol).news or []
        out = []
        for item in items[:limit]:
            content = item.get("content") or item
            title = content.get("title")
            if not title:
                continue
            provider = content.get("provider") or {}
            url = (content.get("canonicalUrl") or {}).get("url") or content.get("link")
            out.append({
                "title": title,
                "publisher": provider.get("displayName") if isinstance(provider, dict) else str(provider),
                "published": content.get("pubDate") or content.get("displayTime"),
                "url": url,
            })
        if not out:
            return {"ok": False, "reason": "no recent headlines"}
        return {"ok": True, "items": out}

    payload = _cached(f"news:{symbol.upper()}:{limit}", TTL_NEWS, produce)
    if not payload.get("ok"):
        return Sourced.unavailable(source, payload.get("reason", "unavailable"))
    return Sourced.live(payload["items"], source)


def analyst_recommendations(symbol: str, allow_fetch: bool = True) -> Sourced:
    """Current analyst rating spread (strongBuy/buy/hold/sell/strongSell)."""
    source = "yfinance:recommendations"

    def produce():
        frame = _ticker(symbol).recommendations
        if frame is None or frame.empty:
            return {"ok": False, "reason": "no ratings published"}
        row = frame.iloc[0].to_dict()
        spread = {k: int(row.get(k, 0) or 0) for k in
                  ("strongBuy", "buy", "hold", "sell", "strongSell")}
        total = sum(spread.values())
        if total == 0:
            return {"ok": False, "reason": "no ratings published"}
        spread["total"] = total
        return {"ok": True, "spread": spread}

    payload = _cached(f"recs:{symbol.upper()}", TTL_TARGETS, produce, allow_fetch)
    if not payload.get("ok"):
        return Sourced.unavailable(source, payload.get("reason", "unavailable"))
    return Sourced.live(payload["spread"], source)


def options_flow(symbol: str, allow_fetch: bool = True) -> Sourced:
    """Nearest-expiry options activity from the real chain.

    Reports only what the chain actually contains. The previous implementation
    compared today's volume against 80% of itself, which made the "unusual
    activity" ratio a constant 1.25x regardless of input.
    """
    source = "yfinance:option_chain"

    def produce():
        ticker = _ticker(symbol)
        expiries = ticker.options
        if not expiries:
            return {"ok": False, "reason": "no listed options"}
        expiry = expiries[0]
        chain = ticker.option_chain(expiry)
        calls, puts = chain.calls, chain.puts
        if calls.empty and puts.empty:
            return {"ok": False, "reason": "empty chain"}

        call_volume = int(calls["volume"].fillna(0).sum()) if not calls.empty else 0
        put_volume = int(puts["volume"].fillna(0).sum()) if not puts.empty else 0
        call_oi = int(calls["openInterest"].fillna(0).sum()) if not calls.empty else 0
        put_oi = int(puts["openInterest"].fillna(0).sum()) if not puts.empty else 0

        def top_strikes(frame):
            if frame.empty or "volume" not in frame:
                return []
            ranked = frame.dropna(subset=["volume"]).sort_values("volume", ascending=False)
            return [
                {"strike": float(r.strike), "volume": int(r.volume)}
                for r in ranked.head(3).itertuples()
            ]

        return {
            "ok": True,
            "expiry": expiry,
            "call_volume": call_volume,
            "put_volume": put_volume,
            "total_volume": call_volume + put_volume,
            "put_call_ratio": round(put_volume / call_volume, 3) if call_volume else None,
            "open_interest_put_call": round(put_oi / call_oi, 3) if call_oi else None,
            "top_calls": top_strikes(calls),
            "top_puts": top_strikes(puts),
            "contracts": int(len(calls) + len(puts)),
        }

    payload = _cached(f"options:{symbol.upper()}", TTL_OPTIONS, produce, allow_fetch)
    if not payload.get("ok"):
        return Sourced.unavailable(source, payload.get("reason", "unavailable"))
    return Sourced.live(payload, source)


def technicals(symbol: str) -> Sourced:
    """Mean-reversion indicators computed from real daily OHLCV.

    RSI uses Wilder's smoothing (the standard definition) rather than a simple
    moving average of gains and losses, which is a common and materially
    different shortcut. Bollinger %B places the close within the band: 0 is the
    lower band, 1 the upper.
    """
    source = "yfinance:history"

    def produce():
        hist = _ticker(symbol).history(period="6mo", interval="1d")
        if hist is None or hist.empty or len(hist) < 30:
            return {"ok": False, "reason": f"insufficient history ({0 if hist is None else len(hist)} bars)"}

        close = hist["Close"].dropna()
        volume = hist["Volume"].dropna()
        if len(close) < 30:
            return {"ok": False, "reason": "insufficient closing prices"}

        # Wilder RSI(14)
        delta = close.diff()
        gain = delta.clip(lower=0)
        loss = -delta.clip(upper=0)
        avg_gain = gain.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
        avg_loss = loss.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
        rs = avg_gain / avg_loss.replace(0, float("nan"))
        rsi_series = 100 - (100 / (1 + rs))
        rsi = rsi_series.iloc[-1]

        # Bollinger(20, 2)
        ma20 = close.rolling(20).mean()
        std20 = close.rolling(20).std()
        upper, lower = ma20 + 2 * std20, ma20 - 2 * std20
        band_width = (upper.iloc[-1] - lower.iloc[-1])
        percent_b = ((close.iloc[-1] - lower.iloc[-1]) / band_width) if band_width else None

        last_close = float(close.iloc[-1])
        ma20_last = float(ma20.iloc[-1]) if ma20.notna().iloc[-1] else None
        avg_volume_20 = float(volume.tail(20).mean()) if len(volume) >= 20 else None
        latest_volume = float(volume.iloc[-1]) if len(volume) else None

        # Drawdown from the 6-month peak: how far this has actually fallen.
        peak = float(close.max())
        drawdown = (last_close - peak) / peak if peak else None

        return {
            "ok": True,
            "close": last_close,
            "rsi14": round(float(rsi), 2) if rsi == rsi else None,
            "percent_b": round(float(percent_b), 3) if percent_b is not None and percent_b == percent_b else None,
            "ma20": round(ma20_last, 4) if ma20_last else None,
            "pct_from_ma20": round((last_close - ma20_last) / ma20_last, 4) if ma20_last else None,
            "volume_ratio_20d": round(latest_volume / avg_volume_20, 2) if avg_volume_20 and latest_volume else None,
            "drawdown_from_6mo_peak": round(drawdown, 4) if drawdown is not None else None,
            "bars": int(len(close)),
        }

    payload = _cached(f"tech:{symbol.upper()}", TTL_TECHNICALS, produce)
    if not payload.get("ok"):
        return Sourced.unavailable(source, payload.get("reason", "unavailable"))
    return Sourced.live(payload, source)


def price_history(symbol: str, period: str = "5y") -> Sourced:
    """Split-adjusted daily closes, for measuring how often targets were hit."""
    source = "yfinance:history"

    def produce():
        hist = _ticker(symbol).history(period=period, interval="1d")
        if hist is None or hist.empty:
            return {"ok": False, "reason": "no price history"}
        closes = [float(c) for c in hist["Close"].dropna().tolist() if c and c > 0]
        if len(closes) < 120:
            return {"ok": False, "reason": f"only {len(closes)} bars of history"}
        return {"ok": True, "closes": closes}

    payload = _cached(f"hist:{symbol.upper()}:{period}", TTL_TECHNICALS, produce)
    if not payload.get("ok"):
        return Sourced.unavailable(source, payload.get("reason", "unavailable"))
    return Sourced.live(payload["closes"], source)


def institutional_holders(symbol: str, limit: int = 5) -> Sourced:
    """Top institutional holders, by name, from 13F filings."""
    source = "yfinance:institutional_holders"

    def produce():
        frame = _ticker(symbol).institutional_holders
        if frame is None or frame.empty:
            return {"ok": False, "reason": "no 13F holders reported"}
        out = []
        for row in frame.head(limit).itertuples():
            out.append({
                "holder": getattr(row, "Holder", None),
                "shares": int(getattr(row, "Shares", 0) or 0),
                "pct_held": float(getattr(row, "pctHeld", 0) or 0),
            })
        return {"ok": True, "holders": out}

    payload = _cached(f"holders:{symbol.upper()}", TTL_PROFILE, produce)
    if not payload.get("ok"):
        return Sourced.unavailable(source, payload.get("reason", "unavailable"))
    return Sourced.live(payload["holders"], source)


# Concurrency for cache warming. The work is network-bound, so threads help
# even on a fractional CPU, but the provider will throttle an aggressive fan-out.
WARM_WORKERS = int(os.environ.get("MARKET_DATA_WARM_WORKERS", 3))



def _compute_technicals_from_closes(closes, volumes):
    """Shared indicator maths, so batch and single-symbol paths cannot diverge."""
    import pandas as pd

    close = pd.Series(closes).dropna()
    volume = pd.Series(volumes).dropna()
    if len(close) < 30:
        return {"ok": False, "reason": f"insufficient history ({len(close)} bars)"}

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
    peak = float(close.max())

    if rsi != rsi or ma20_last is None:
        return {"ok": False, "reason": "indicators unavailable"}

    return {
        "ok": True,
        "close": last_close,
        "rsi14": round(float(rsi), 2),
        "percent_b": round(float(percent_b), 3) if percent_b is not None and percent_b == percent_b else None,
        "ma20": round(ma20_last, 4),
        "pct_from_ma20": round((last_close - ma20_last) / ma20_last, 4),
        "volume_ratio_20d": round(latest_volume / avg_volume_20, 2) if avg_volume_20 and latest_volume else None,
        "drawdown_from_6mo_peak": round((last_close - peak) / peak, 4) if peak else None,
        "bars": int(len(close)),
    }


def batch_history(symbols: List[str], period: str = "5y") -> int:
    """Fetch price history for many symbols in a single request.

    This is the change that keeps the app inside Yahoo's limits. Fetching 25
    symbols individually meant 25 requests per refresh; yf.download retrieves
    them all in one, and the result populates both the technicals and the
    raw-closes caches so no per-symbol call is needed afterwards.
    """
    symbols = [s.upper() for s in symbols]
    pending = [s for s in symbols
               if _cache.get(f"tech:{s}") is None or _cache.get(f"hist:{s}:{period}") is None]
    if not pending:
        return 0

    _throttle()
    try:
        frame = yf.download(pending, period=period, interval="1d",
                            group_by="ticker", auto_adjust=True,
                            progress=False, threads=False)
    except Exception as e:
        logger.warning(f"batch history failed for {len(pending)} symbols: {type(e).__name__}: {e}")
        return 0

    if frame is None or frame.empty:
        return 0

    populated = 0
    for symbol in pending:
        try:
            # yfinance returns a flat frame for one symbol and a MultiIndex for many.
            sub = frame[symbol] if len(pending) > 1 else frame
            closes = [float(c) for c in sub["Close"].dropna().tolist() if c and c > 0]
            volumes = [float(v) for v in sub["Volume"].fillna(0).tolist()]
        except Exception:
            continue
        if len(closes) < 30:
            continue

        _cache.set(f"hist:{symbol}:{period}", {"ok": True, "closes": closes},
                   _effective_ttl(TTL_TECHNICALS))
        _cache.set(f"tech:{symbol}",
                   _compute_technicals_from_closes(closes[-130:], volumes[-130:]),
                   _effective_ttl(TTL_TECHNICALS))
        populated += 1

    logger.info(f"batch history populated {populated}/{len(pending)} symbols in one request")
    return populated


def warm(symbols: List[str], include_options: bool = False) -> dict:
    """Populate the cache for a batch of symbols, concurrently.

    Rendering the loser list needs roughly four provider calls per symbol. Done
    sequentially for 25 symbols that took ~45s, which is far too slow for a page
    load. These calls are almost entirely spent waiting on the network, so they
    parallelise well even on a 0.5 CPU instance.

    Every task is individually guarded: one symbol failing must not abort the
    warm for the rest, and anything still missing simply resolves unavailable
    later rather than blocking the render.
    """
    from concurrent.futures import ThreadPoolExecutor

    # One request covers every symbol's history; only the profile remains
    # per-symbol, which takes a cold refresh from ~50 requests down to ~26.
    try:
        batch_history(symbols)
    except Exception as e:
        logger.warning(f"batch history unavailable: {type(e).__name__}: {e}")

    tasks = []
    for symbol in symbols:
        tasks.append((symbol, _info))
        if include_options:
            tasks.append((symbol, analyst_recommendations))
            tasks.append((symbol, options_flow))

    started = time.time()
    failures = 0

    def run(job):
        symbol, fn = job
        try:
            fn(symbol)
            return True
        except Exception as e:
            logger.warning(f"warm {fn.__name__} failed for {symbol}: {type(e).__name__}")
            return False

    with ThreadPoolExecutor(max_workers=WARM_WORKERS) as pool:
        for succeeded in pool.map(run, tasks):
            if not succeeded:
                failures += 1

    elapsed = time.time() - started
    logger.info(f"warmed {len(symbols)} symbols in {elapsed:.1f}s ({failures} task failures)")
    return {"symbols": len(symbols), "tasks": len(tasks), "failures": failures,
            "seconds": round(elapsed, 1)}
