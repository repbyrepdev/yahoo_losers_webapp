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
TTL_TARGETS = 24 * 60 * 60    # analyst data is slow-moving, and quoteSummary
                              # is Yahoo's scarcest endpoint: one spread-out
                              # call per symbol per day fits its budget
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
MIN_CALL_INTERVAL_SECONDS = float(os.environ.get("MARKET_DATA_MIN_INTERVAL", 0.8))

_throttle_lock = threading.Lock()
_last_call_at = [0.0]

# The .info lane (Yahoo quoteSummary) is far stricter than the chart lane and
# is the endpoint behind every morning limiter trip in the logs. It gets its
# own slower pacing, and one shared cooldown when it is refused -- individual
# symbols are not poisoned for fifteen minutes each.
# The options endpoint (chains behind options_flow and implied_move) is
# per-symbol and as limiter-hostile as quoteSummary. One shared cooldown:
# a refusal pauses ALL options calls, so a prewarmer walking the universe
# cannot hammer the endpoint symbol after symbol.
_options_cooldown_until = [0.0]


def _options_cooldown_active() -> bool:
    return time.time() < _options_cooldown_until[0]


def _options_refused():
    _options_cooldown_until[0] = time.time() + 15 * 60
    logger.warning("options endpoint refused; all option calls paused 15 min")


INFO_CALL_INTERVAL_SECONDS = float(os.environ.get("MARKET_DATA_INFO_INTERVAL", 4.0))
INFO_INTERVAL_MAX_SECONDS = max(
    float(os.environ.get("MARKET_DATA_INFO_INTERVAL", 4.0)),
    float(os.environ.get("MARKET_DATA_INFO_INTERVAL_MAX", 90.0)))
_info_last_call_at = [0.0]
_info_cooldown_until = [0.0]
# Adaptive: refusals double this, successes decay it toward the base. The
# lane converges on whatever rate the endpoint is actually granting.
_info_interval = [INFO_CALL_INTERVAL_SECONDS]


def _info_lane_refused():
    _info_interval[0] = min(INFO_INTERVAL_MAX_SECONDS, _info_interval[0] * 2)
    _info_cooldown_until[0] = time.time() + _info_interval[0]
    logger.warning(f"quoteSummary refused; info interval now {_info_interval[0]:.0f}s")


def _info_lane_succeeded():
    _info_interval[0] = max(INFO_CALL_INTERVAL_SECONDS,
                            _info_interval[0] * 0.5)


_info_throttle_lock = threading.Lock()


def _info_throttle():
    # Own lock: sleeping four seconds while holding the shared throttle lock
    # would stall the chart lane behind every info call.
    with _info_throttle_lock:
        elapsed = time.time() - _info_last_call_at[0]
        wait = _info_interval[0] - elapsed
        while wait > 0:
            time.sleep(min(wait, 30))
            wait = _info_interval[0] - (time.time() - _info_last_call_at[0])
        _info_last_call_at[0] = time.time()


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


# Compared lowercased against lowercased reasons; keep every marker lowercase
# or it can never match (the old "no 13F holders" was dead on arrival).
_STRUCTURAL_MARKERS = (
    "no analyst coverage", "no listed options", "no 13f holders",
    "no ratings published", "insufficient history", "no earnings date published",
    "only ", "empty chain", "no recent headlines", "not in sec registry",
    "no usd facts",
)


def _is_structural(reason: str) -> bool:
    """True when a failure reflects the security itself, not a provider outage."""
    lowered = (reason or "").lower()
    return any(marker in lowered for marker in _STRUCTURAL_MARKERS)


# Session phases, in minutes from Eastern midnight. Pre-market and after-hours
# are real trading sessions: prices move and, crucially for this app, analysts
# publish rating and target changes between six and nine-thirty in the
# morning. Treating everything outside 9:30-4 as a dead zone meant the
# stretched overnight caches could hold exactly those updates back.
PHASE_BOUNDS = {
    "pre_market": (4 * 60, 9 * 60 + 30),
    "open": (9 * 60 + 30, 16 * 60),
    "after_hours": (16 * 60, 20 * 60),
}


def _eastern_now():
    try:
        import pytz

        return datetime.now(pytz.timezone("America/New_York"))
    except Exception as e:
        logger.warning(f"eastern clock unavailable ({type(e).__name__}); "
                       "falling back to server time -- phase logic may misread")
        return datetime.now()


_trading_days_source = [None]


def set_trading_days_source(fn):
    """Register a callable returning a set of trading-day ISO dates (or None).

    Lets the app inject a real exchange calendar without a circular import;
    with none registered, weekday logic applies as before.
    """
    _trading_days_source[0] = fn


def market_phase() -> dict:
    """Current session phase and when it next changes, on the Eastern clock.

    Returns one of open / pre_market / after_hours / closed. With a trading
    calendar registered, holidays read as closed instead of rendering an
    open-market cadence on Labor Day.
    """
    now = _eastern_now()
    minutes = now.hour * 60 + now.minute

    source = _trading_days_source[0]
    if source is not None and now.weekday() < 5:
        try:
            days = source()
            if days is not None and now.date().isoformat() not in days:
                from datetime import timedelta as _td
                nxt = now + _td(days=1)
                for _ in range(10):
                    if nxt.date().isoformat() in days:
                        break
                    nxt = nxt + _td(days=1)
                changes = nxt.replace(hour=PHASE_BOUNDS["pre_market"][0] // 60,
                                      minute=PHASE_BOUNDS["pre_market"][0] % 60,
                                      second=0, microsecond=0)
                return {"phase": "closed", "changes_at": changes}
        except Exception as e:
            logger.debug(f"trading-days source failed: {type(e).__name__}")

    def at(day, minute):
        from datetime import timedelta as _td

        base = (now + _td(days=day)).replace(
            hour=minute // 60, minute=minute % 60, second=0, microsecond=0)
        return base

    if now.weekday() < 5:
        for phase, (start, end) in PHASE_BOUNDS.items():
            if start <= minutes < end:
                return {"phase": phase, "changes_at": at(0, end)}
        if minutes < PHASE_BOUNDS["pre_market"][0]:
            return {"phase": "closed", "changes_at": at(0, PHASE_BOUNDS["pre_market"][0])}
    # Evening after 8pm, or a weekend day: next pre-market open.
    days_ahead = 1
    while (now.weekday() + days_ahead) % 7 >= 5:
        days_ahead += 1
    return {"phase": "closed", "changes_at": at(days_ahead, PHASE_BOUNDS["pre_market"][0])}


def market_is_open() -> bool:
    """Regular NYSE session only; extended hours are their own phases."""
    return market_phase()["phase"] == "open"


# Cache-stretch multiplier per phase. Extended sessions move prices and carry
# the morning analyst updates, so they stretch far less than the dead of night.
PHASE_TTL_STRETCH = {"open": 1, "pre_market": 2, "after_hours": 2, "closed": 8}


def _effective_ttl(base_ttl: int, spread: float = 0.1) -> int:
    """Stretch TTLs when the market is closed, with jitter to avoid stampedes.

    Prices do not move overnight or at weekends, so refetching then spends the
    instance's small request budget for nothing. Jitter keeps 25 symbols from
    all expiring in the same second and stampeding the provider.
    """
    stretch = PHASE_TTL_STRETCH.get(market_phase()["phase"], 8)
    ttl = base_ttl if stretch == 1 else min(base_ttl * stretch, 12 * 60 * 60)
    return max(30, int(ttl * random.uniform(1.0 - spread, 1.0 + spread)))

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
        self._redis_failures = 0
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

    def _redis_failed(self, e):
        """Count consecutive failures; after three, stop paying the socket
        timeout on every cache operation and say so once. A process restart
        re-enables Redis -- simple, and impossible to flap."""
        self._redis_failures += 1
        if self._redis_failures >= 3 and self._redis is not None:
            logger.warning(f"Redis disabled after {self._redis_failures} consecutive "
                           f"failures ({type(e).__name__}); per-process cache only")
            self._redis = None

    def get(self, key: str):
        if self._redis is not None:
            try:
                import json

                raw = self._redis.get(self._key(key))
                self._redis_failures = 0
                if raw is not None:
                    return json.loads(raw)
            except Exception as e:
                self._redis_failed(e)
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
                self._redis_failures = 0
            except Exception as e:
                self._redis_failed(e)
        with self._lock:
            self._local[key] = (time.time() + ttl, value)


CACHE_FILE = os.environ.get("MARKET_DATA_CACHE_FILE", "/tmp/market_data_cache.json")

_cache = TTLCache(os.environ.get("REDIS_URL"))


def _load_cache_from_disk():
    """Restore unexpired entries written by a previous process.

    Fully guarded: this runs at import, and a corrupt or old-schema file must
    cost a cold start with a log line -- never the process.
    """
    try:
        import json
        with open(CACHE_FILE, "r", encoding="utf-8") as handle:
            stored = json.load(handle)
    except FileNotFoundError:
        return 0
    except (OSError, ValueError) as e:
        logger.warning(f"disk cache unreadable ({type(e).__name__}); cold-starting")
        return 0
    try:
        if stored.get("_schema") != CACHE_SCHEMA_VERSION:
            logger.info("disk cache from another schema version; cold-starting")
            return 0
        now = time.time()
        restored = 0
        for key, entry in stored.items():
            if key == "_schema":
                continue
            expires_at, value = entry
            if expires_at > now:
                _cache._local[key] = (expires_at, value)
                restored += 1
        if restored:
            logger.info(f"restored {restored} cached entries from disk")
        return restored
    except Exception as e:
        logger.warning(f"disk cache malformed ({type(e).__name__}); cold-starting")
        return 0


def cache_size() -> int:
    """Entries visible to this process, preferring the shared backend."""
    if _cache._redis is not None:
        try:
            return sum(1 for _ in _cache._redis.scan_iter(f"md:{CACHE_SCHEMA_VERSION}:*"))
        except Exception:
            pass
    return len(_cache._local)


def clear_cache():
    """Drop every cached entry, in memory, on disk and in Redis.

    Used by /refresh so a manual refresh can actually recover from a bad state
    rather than only clearing the rendered page on top of it.
    """
    redis_clear_failed = False
    with _cache._lock:
        cleared = len(_cache._local)
        _cache._local.clear()
    # Count the shared backend too. Each gunicorn worker's local dict can be
    # near-empty while the real entries live in Redis, so reporting only the
    # local count could truthfully clear everything and still say "0 entries".
    if _cache._redis is not None:
        try:
            for key in _cache._redis.scan_iter(f"md:{CACHE_SCHEMA_VERSION}:*"):
                cleared += int(_cache._redis.delete(key) or 0)
        except Exception as e:
            logger.warning(f"Redis cache clear failed: {type(e).__name__}")
            redis_clear_failed = True
    try:
        os.remove(CACHE_FILE)
    except OSError:
        pass
    if redis_clear_failed:
        # Claiming a clean slate while poisoned entries survive in Redis
        # (and immediately re-share to every worker) is a false recovery.
        logger.warning(f"market_data cache PARTIALLY cleared ({cleared} local entries; "
                       "Redis entries survived)")
    else:
        logger.info(f"market_data cache cleared ({cleared} entries)")
    return cleared


_persist_lock = threading.Lock()


def save_cache_to_disk():
    """Write the in-memory cache out so the next process can reuse it.

    Both lanes call this, so the snapshot is taken under the cache lock and
    the file is replaced atomically under a persistence lock -- a reader (or
    a restart) can never observe a half-written file.
    """
    try:
        import json
        import tempfile
        with _cache._lock:
            snapshot = {k: [exp, val] for k, (exp, val) in _cache._local.items()
                        if isinstance(val, dict) and val.get("ok")}
        snapshot["_schema"] = CACHE_SCHEMA_VERSION
        with _persist_lock:
            fd, tmp_path = tempfile.mkstemp(
                dir=os.path.dirname(CACHE_FILE) or ".", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(snapshot, handle, default=str)
                os.replace(tmp_path, CACHE_FILE)
            finally:
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)
    except (OSError, TypeError, ValueError) as e:
        # WARNING, not debug: a full or read-only disk means every restart
        # cold-starts against the limiter, and that must be visible.
        logger.warning(f"cache persist failed: {type(e).__name__}")


_load_cache_from_disk()


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
            # The full detail rides along for classification: a 429 inside a
            # generic HTTPError must hit the rate-limited tier, not be retried
            # every sixty seconds for the life of the limiter event.
            value = {"ok": False, "reason": name, "detail": detail}
            break

    if not isinstance(value, dict):
        # Producers must return dicts; a stray None must not AttributeError
        # into the render path.
        value = {"ok": False, "reason": "producer returned no payload"}
    classify_on = value.get("detail") or value.get("reason") or ""
    if value.get("ok"):
        lifetime = _effective_ttl(ttl)
        _cache.set(f"negcand:{key}", {}, 1)  # success clears any candidate
    elif "cooling down" in (value.get("reason") or ""):
        lifetime = 90  # retry shortly after the shared cooldown lifts
    elif _is_rate_limited(classify_on):
        lifetime = TTL_RATE_LIMITED
    elif _is_structural(value.get("reason", "")):
        # Providers degrade by returning EMPTY 200s wearing the same prose as
        # genuine structural absence ("no listed options" during an outage).
        # A structural negative therefore needs two consecutive confirmations.
        # The candidate marker lives under a SIDE key with its own longer
        # lifetime: by the time this code runs the main key has already
        # expired (that is why produce ran), so reading it back could only
        # ever see None and the long TTL was unreachable (CR, PR 53).
        marker_key = f"negcand:{key}"
        marker = _cache.get(marker_key)
        # Promotion needs a DISTINCT later confirmation: same reason AND the
        # candidate interval fully elapsed. Without the age check, concurrent
        # misses promoted instantly; and the marker is refreshed on every
        # sighting so a stale one cannot promote a much later first failure.
        if (isinstance(marker, dict) and marker.get("reason") == value.get("reason")
                and time.time() - marker.get("at", 0) >= 4 * 60):
            lifetime = _effective_ttl(TTL_NEGATIVE_STRUCTURAL)
        else:
            _cache.set(marker_key, {"reason": value.get("reason"),
                                    "at": time.time()}, 60 * 60)
            lifetime = 5 * 60
    else:
        lifetime = TTL_NEGATIVE_TRANSIENT

    _cache.set(key, value, lifetime)
    return value


def _ticker(symbol: str) -> yf.Ticker:
    return yf.Ticker(symbol.upper())


def _info(symbol: str, allow_fetch: bool = True) -> dict:
    """One `.info` call backs targets, sector, short interest and ownership.

    Deliberately cached as a single blob under the shortest of the lifetimes it
    serves (targets). Splitting sector onto its own 7-day TTL would look tidier
    but would cost a second network round trip per symbol to save nothing --
    the call has to happen for the targets regardless.
    """

    def produce():
        if time.time() < _info_cooldown_until[0]:
            return {"ok": False,
                    "reason": "info lane cooling down after rate limit"}
        _info_throttle()
        try:
            info = _ticker(symbol).info or {}
        except Exception as e:
            detail = f"{type(e).__name__}: {e}"
            if _is_rate_limited(detail):
                _info_lane_refused()
                return {"ok": False, "reason": "info lane cooling down after rate limit"}
            raise
        if not info.get("symbol") and not info.get("regularMarketPrice"):
            return {"ok": False, "reason": "no profile returned"}
        _info_lane_succeeded()
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
            "total_cash": info.get("totalCash"),
            "total_debt": info.get("totalDebt"),
            "free_cashflow": info.get("freeCashflow"),
            "profit_margins": info.get("profitMargins"),
            "current_price": info.get("currentPrice") or info.get("regularMarketPrice"),
            "fifty_two_week_low": info.get("fiftyTwoWeekLow"),
            "fifty_two_week_high": info.get("fiftyTwoWeekHigh"),
        }

    # Wide jitter: entries written in the same evening batch must not expire
    # in the same morning minute -- twenty-five simultaneous quoteSummary
    # calls is exactly the burst that endpoint refuses.
    return _cached(f"info:{symbol.upper()}", int(TTL_TARGETS * random.uniform(0.7, 1.3)),
                   produce, allow_fetch)


def analyst_target(symbol: str, allow_fetch: bool = True) -> Dict[str, Sourced]:
    """Analyst consensus target, with the spread and the count behind it.

    The count is returned alongside the number because "$322.28" and
    "$322.28 across 41 analysts" are different claims.
    """
    info = _info(symbol, allow_fetch)
    source = "yfinance:targetMeanPrice"

    mean = count = None
    if info.get("ok"):
        mean, count = info.get("target_mean"), info.get("analysts")
    if not mean:
        # Backup: FMP's price-target summary carries mean and count, so the
        # consensus-upside factor survives a blocked quoteSummary. The same
        # thin-coverage gates below apply to either provider.
        try:
            import sources
            fallback = sources.price_targets(symbol, allow_fetch)
            if fallback.ok:
                mean = fallback.value["mean"]
                count = fallback.value["count"]
                source = fallback.source
        except Exception as e:
            logger.info(f"targets fallback failed for {symbol}: {type(e).__name__}")

    if not info.get("ok") and not mean:
        reason = info.get("reason", "unavailable")
        return {
            "mean": Sourced.unavailable(source, reason),
            "high": Sourced.unavailable(source, reason),
            "low": Sourced.unavailable(source, reason),
            "analysts": Sourced.unavailable(source, reason),
        }

    if not mean:
        gap = "no analyst coverage published"
        return {
            "mean": Sourced.unavailable(source, gap),
            "high": Sourced.unavailable(source, gap),
            "low": Sourced.unavailable(source, gap),
            "analysts": (Sourced.live(count, source) if count is not None
                         else Sourced.unavailable(source, "analyst count not reported")),
        }
    if not count or count < MIN_ANALYSTS_FOR_CONSENSUS:
        thin = f"only {count or 0} analyst estimate(s)"
        return {
            "mean": Sourced.unavailable(source, thin),
            "high": Sourced.unavailable(source, thin),
            "low": Sourced.unavailable(source, thin),
            "analysts": (Sourced.live(count, source) if count is not None
                         else Sourced.unavailable(source, "analyst count not reported")),
        }

    yahoo_extremes = info.get("ok") and source == "yfinance:targetMeanPrice"
    return {
        "mean": Sourced.live(mean, source),
        "high": (Sourced.live(info.get("target_high"), source)
                 if yahoo_extremes and info.get("target_high")
                 else Sourced.unavailable(source, "no high estimate")),
        "low": (Sourced.live(info.get("target_low"), source)
                if yahoo_extremes and info.get("target_low")
                else Sourced.unavailable(source, "no low estimate")),
        "analysts": Sourced.live(count, source),
    }


def profile(symbol: str, allow_fetch: bool = True) -> Dict[str, Sourced]:
    """Sector, industry, short interest and institutional ownership."""
    info = _info(symbol, allow_fetch)
    source = "yfinance:info"

    def _short_float_fallback():
        # Backup: FINRA settlement short interest over FMP float -- the same
        # twice-monthly data Yahoo repackages. Keeps the score's short-interest
        # factor alive while quoteSummary is blocked. Returns None unless it
        # produced a real value.
        try:
            import sources
            fb = sources.short_percent_float(symbol, allow_fetch)
            return fb if fb.ok else None
        except Exception as e:
            logger.info(f"short-float fallback failed for {symbol}: {type(e).__name__}")
            return None

    if not info.get("ok"):
        reason = info.get("reason", "unavailable")
        result = {k: Sourced.unavailable(source, reason) for k in
                  ("sector", "industry", "short_pct_float", "held_pct_institutions", "avg_volume")}
        fallback = _short_float_fallback()
        if fallback is not None:
            result["short_pct_float"] = fallback
        return result

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
        "short_pct_float": (field("short_pct_float")
                            if info.get("short_pct_float") is not None
                            else (_short_float_fallback()
                                  or field("short_pct_float"))),
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

    from datetime import date as _date, datetime as _dt
    import pytz as _pytz

    today = _dt.now(_pytz.timezone("America/New_York")).date()
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


def _recs_key(symbol: str) -> str:
    return f"recs:v2:{symbol.upper()}"


def _options_key(symbol: str) -> str:
    return f"options:v2:{symbol.upper()}"


def analyst_recommendations(symbol: str, allow_fetch: bool = True) -> Sourced:
    """Current analyst rating spread (strongBuy/buy/hold/sell/strongSell)."""
    source = "yfinance:recommendations"

    def produce():
        yahoo_error = None
        try:
            frame = _ticker(symbol).recommendations
        except Exception as e:
            frame = None
            yahoo_error = f"{type(e).__name__}: {e}"
            logger.info(f"yahoo ratings failed for {symbol}: {type(e).__name__}")
        if frame is None or frame.empty:
            # Backup: Finnhub's trends carry the same spread, so the score's
            # ratings factor survives a quoteSummary outage.
            try:
                import sources
                fallback = sources.ratings_spread(symbol)
                if fallback.ok:
                    return {"ok": True, "spread": fallback.value,
                            "provider": "finnhub"}
            except Exception as e:
                logger.info(f"ratings fallback failed for {symbol}: {type(e).__name__}")
            if yahoo_error is not None:
                # A provider failure is not "no coverage": keep the original
                # detail so _cached classifies it transient or rate-limited,
                # never a six-hour structural absence (CR, PR 60).
                return {"ok": False, "reason": yahoo_error.split(":")[0],
                        "detail": yahoo_error}
            return {"ok": False, "reason": "no ratings published"}
        row = frame.iloc[0].to_dict()
        spread = {k: int(row.get(k, 0) or 0) for k in
                  ("strongBuy", "buy", "hold", "sell", "strongSell")}
        total = sum(spread.values())
        if total == 0:
            return {"ok": False, "reason": "no ratings published"}
        spread["total"] = total
        return {"ok": True, "spread": spread}

    payload = _cached(_recs_key(symbol), TTL_TARGETS, produce, allow_fetch)
    if not payload.get("ok"):
        return Sourced.unavailable(source, payload.get("reason", "unavailable"))
    actual = ("finnhub:recommendation-trends" if payload.get("provider") == "finnhub"
              else source)
    return Sourced.live(payload["spread"], actual)


def options_flow(symbol: str, allow_fetch: bool = True) -> Sourced:
    """Nearest-expiry options activity from the real chain.

    Reports only what the chain actually contains. The previous implementation
    compared today's volume against 80% of itself, which made the "unusual
    activity" ratio a constant 1.25x regardless of input.
    """
    source = "yfinance:option_chain"

    def _alpaca_fallback():
        """Put/call positioning from the indicative feed: keeps the score's
        options factor alive while Yahoo's chain endpoint is limited."""
        try:
            import sources
            fallback = sources.options_putcall(symbol)
            if fallback.ok:
                v = fallback.value
                return {"ok": True, "provider": "alpaca",
                        "expiry": v.get("window"),
                        "call_volume": v["call_volume"],
                        "put_volume": v["put_volume"],
                        "total_volume": v["call_volume"] + v["put_volume"],
                        "put_call_ratio": v.get("put_call_ratio"),
                        "open_interest_put_call": None,
                        "top_calls": [], "top_puts": [],
                        "contracts": v.get("contracts", 0)}
        except Exception as e:
            logger.info(f"options fallback failed for {symbol}: {type(e).__name__}")
        return None

    def _yahoo_flow():
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

    def produce():
        if _options_cooldown_active():
            return _alpaca_fallback() or {
                "ok": False, "reason": "options endpoint cooling down"}
        # Any Yahoo failure -- refusal dict or exception, at any stage -- gets
        # one shot at the independent feed before the refusal caches. The
        # first version only covered two of the four exits, so a chain-stage
        # limiter refusal cached with no fallback attempt (live 2026-08-19).
        try:
            result = _yahoo_flow()
        except Exception as e:
            detail = f"{type(e).__name__}: {e}"
            rate_limited = _is_rate_limited(detail)
            if rate_limited:
                _options_refused()
            fallback = _alpaca_fallback()
            if fallback:
                return fallback
            if rate_limited:
                # Return, don't raise: _cached's quick retry would re-enter
                # under the just-armed cooldown and call Alpaca a second
                # time per symbol per cycle (CR, PR 59).
                return {"ok": False, "reason": type(e).__name__,
                        "detail": detail}
            raise
        if not result.get("ok"):
            return _alpaca_fallback() or result
        return result

    payload = _cached(_options_key(symbol), TTL_OPTIONS, produce, allow_fetch)
    if not payload.get("ok"):
        return Sourced.unavailable(source, payload.get("reason", "unavailable"))
    actual = ("alpaca:options-indicative" if payload.get("provider") == "alpaca"
              else source)
    return Sourced.live(payload, actual)


def implied_move(symbol: str, allow_fetch: bool = True) -> Sourced:
    """The move the options market is pricing in, from the ATM straddle.

    An at-the-money straddle's cost is what the market charges for exposure to
    any move by expiry, so straddle / spot is the market's own forward-looking
    magnitude estimate -- the live counterpart to this app's backward-looking
    hit rates. Priced off mid-quotes; thin small-cap chains get a quality flag
    instead of silent trust, and a chain with no usable quotes is unavailable
    rather than guessed.
    """
    source = "yfinance:option-chain-atm-straddle"

    def produce():
        if _options_cooldown_active():
            return {"ok": False, "reason": "options endpoint cooling down"}
        try:
            ticker = _ticker(symbol)
            expiries = ticker.options
        except Exception as e:
            if _is_rate_limited(f"{type(e).__name__}: {e}"):
                _options_refused()
            raise
        if not expiries:
            return {"ok": False, "reason": "no listed options"}
        from datetime import date as _date
        today = _eastern_now().date()
        expiry = None
        for candidate in expiries:
            try:
                days_out = (_date.fromisoformat(candidate) - today).days
            except ValueError:
                continue
            if days_out >= 5:
                expiry, days_to_expiry = candidate, days_out
                break
        if not expiry:
            return {"ok": False, "reason": "no expiry at least 5 days out"}

        try:
            chain = ticker.option_chain(expiry)
        except Exception as e:
            if _is_rate_limited(f"{type(e).__name__}: {e}"):
                _options_refused()
            raise
        calls, puts = chain.calls, chain.puts
        if calls.empty or puts.empty:
            return {"ok": False, "reason": "one-sided chain"}

        # Spot must be a real underlying price. A strike is not one, and a
        # straddle anchored to a guessed level is a fabricated figure. Read
        # through technicals() so the payload contract has one owner.
        tech = technicals(symbol, allow_fetch=False)
        spot = tech.value.get("close") if tech.ok else None
        if not spot:
            return {"ok": False, "reason": "no cached underlying price to anchor the strike"}

        # One shared ATM strike, present on both sides -- differing legs are
        # not a straddle.
        call_strikes = set(calls["strike"].dropna().tolist())
        put_strikes = set(puts["strike"].dropna().tolist())
        common = call_strikes & put_strikes
        if not common:
            return {"ok": False, "reason": "no strike listed on both sides"}
        strike = min(common, key=lambda k: abs(k - spot))

        def leg_mid(frame):
            row = frame[frame["strike"] == strike].iloc[0]
            bid = float(row.get("bid") or 0)
            ask = float(row.get("ask") or 0)
            if bid > 0 and ask > 0:
                mid = (bid + ask) / 2
                return mid, (ask - bid) / mid if mid else None, "quote"
            last = float(row.get("lastPrice") or 0)
            return (last, None, "last-trade") if last > 0 else (None, None, None)

        call_mid, call_spread, call_basis = leg_mid(calls)
        put_mid, put_spread, put_basis = leg_mid(puts)
        if not call_mid or not put_mid:
            return {"ok": False, "reason": "no usable ATM quotes"}

        # Three distinct quality states: live quotes with a sane spread, live
        # quotes but wide, or no live quotes at all (priced off last trades).
        if "last-trade" in (call_basis, put_basis):
            quality = "last-trade fallback (no live quotes)"
        else:
            worst_spread = max(call_spread, put_spread)
            quality = "ok" if worst_spread < 0.35 else "wide-spread (thin chain)"
        return {
            "ok": True,
            "expiry": expiry,
            "days_to_expiry": days_to_expiry,
            "implied_move_pct": round((call_mid + put_mid) / spot * 100, 1),
            "spot": round(float(spot), 2),
            "strike": float(strike),
            "quality": quality,
            "estimate_basis": "ATM straddle mid-quotes over the last daily close "
                              "(intraday moves scale the figure until the next "
                              "close); not a probability, the magnitude of move "
                              "the market is pricing",
        }

    payload = _cached(f"implied:{symbol.upper()}", TTL_OPTIONS, produce, allow_fetch)
    if not payload.get("ok"):
        return Sourced.unavailable(source, payload.get("reason", "unavailable"))
    return Sourced.derived({k: v for k, v in payload.items() if k != "ok"}, source)


EDGAR_FTS_URL = "https://efts.sec.gov/LATEST/search-index"
GOING_CONCERN_LOOKBACK_DAYS = 400  # covers the latest 10-K plus quarters


def going_concern(symbol: str, allow_fetch: bool = True) -> Sourced:
    """Whether recent 10-K/10-Q filings contain going-concern language.

    Full-text search for the phrase auditors are required to use --
    "substantial doubt" -- in the issuer's filings from the past ~13 months.
    A stock down 15% with that language in its latest quarterly is a
    categorically different bet than one without it. Three honest states:
    flagged (found, with the filing), clear (searched, none found), and
    unavailable (could not check) -- a failed lookup must never render as a
    clean bill of health.
    """
    source = "sec-edgar:full-text-search"

    def produce():
        # CIK resolution lives inside produce so a cache-only call returns
        # from the gc: cache without ever touching the SEC -- resolving the
        # CIK map first would itself fetch when that map is cold. Preflight
        # failures become cached negatives, so the info lane cannot spin on
        # symbols that will never resolve.
        ciks = _edgar_cik_table()
        if not ciks.get("ok"):
            return {"ok": False, "reason": ciks.get("reason", "cik map unavailable")}
        cik = ciks["table"].get(symbol.upper())
        if not cik:
            return {"ok": False, "reason": "ticker not in SEC registry"}
        import requests as _rq
        from datetime import timedelta as _td
        cutoff = (_eastern_now().date() - _td(days=GOING_CONCERN_LOOKBACK_DAYS)).isoformat()
        try:
            _throttle()
            # The date range rides in the request: EFTS returns at most 100
            # relevance-ranked hits, so without it a recent match could be
            # crowded off the page by decades of old ones. The local cutoff
            # below stays as the guard.
            response = _rq.get(
                EDGAR_FTS_URL,
                params={"q": '"substantial doubt"', "forms": "10-K,10-Q",
                        "ciks": str(cik).zfill(10), "dateRange": "custom",
                        "startdt": cutoff,
                        "enddt": _eastern_now().date().isoformat()},
                headers={"User-Agent": EDGAR_UA}, timeout=30)
            response.raise_for_status()
            payload_json = response.json()
            if not isinstance(payload_json, dict) or "hits" not in payload_json:
                # A 200 with a drifted schema is a failed lookup, and a
                # failed lookup must never render as a clean bill of health.
                return {"ok": False, "reason": "edgar full-text response shape changed"}
            hits = (payload_json.get("hits") or {}).get("hits") or []
        except Exception as e:
            return {"ok": False, "reason": f"edgar full-text search unavailable ({type(e).__name__})"}

        recent = []
        for hit in hits:
            src = hit.get("_source") or {}
            file_date = src.get("file_date")
            if file_date and file_date >= cutoff:
                recent.append((file_date, src.get("file_type") or
                               ",".join(src.get("root_forms") or []), src.get("adsh")))
        if not recent:
            return {"ok": True, "flagged": False, "searched_since": cutoff}
        recent.sort(reverse=True)
        file_date, form, adsh = recent[0]
        return {
            "ok": True,
            "flagged": True,
            "latest": file_date,
            "form": form,
            "filings_url": (f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany"
                            f"&CIK={cik}&type=10&dateb=&owner=include&count=10"),
            "note": ("filing contains the phrase 'substantial doubt'; read the filing "
                     "-- management plans sometimes state the doubt is alleviated"),
        }

    payload = _cached(f"gc:{symbol.upper()}", 24 * 60 * 60, produce, allow_fetch)
    if not payload.get("ok"):
        return Sourced.unavailable(source, payload.get("reason", "unavailable"))
    return Sourced.live({k: v for k, v in payload.items() if k != "ok"}, source)


def technicals(symbol: str, allow_fetch: bool = True) -> Sourced:
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
            "fetched_at": time.time(),
            "close": last_close,
            "rsi14": round(float(rsi), 2) if rsi == rsi else None,
            "percent_b": round(float(percent_b), 3) if percent_b is not None and percent_b == percent_b else None,
            "ma20": round(ma20_last, 4) if ma20_last else None,
            "pct_from_ma20": round((last_close - ma20_last) / ma20_last, 4) if ma20_last else None,
            "volume_ratio_20d": round(latest_volume / avg_volume_20, 2) if avg_volume_20 and latest_volume else None,
            "drawdown_from_6mo_peak": round(drawdown, 4) if drawdown is not None else None,
            "bars": int(len(close)),
        }

    payload = _cached(f"tech:{symbol.upper()}", TTL_TECHNICALS, produce, allow_fetch)
    if not payload.get("ok"):
        return Sourced.unavailable(source, payload.get("reason", "unavailable"))
    return Sourced.live(payload, source)


def price_history(symbol: str, period: str = "5y", allow_fetch: bool = True) -> Sourced:
    """Split-adjusted daily closes, for measuring how often targets were hit."""
    source = "yfinance:history"

    def produce():
        hist = _ticker(symbol).history(period=period, interval="1d")
        if hist is None or hist.empty:
            return {"ok": False, "reason": "no price history"}
        closes = [float(c) for c in hist["Close"].dropna().tolist() if c and c > 0]
        if len(closes) < 40:
            return {"ok": False, "reason": f"only {len(closes)} bars of history"}
        return {"ok": True, "closes": closes}

    payload = _cached(f"hist:{symbol.upper()}:{period}", TTL_TECHNICALS, produce, allow_fetch)
    if not payload.get("ok"):
        return Sourced.unavailable(source, payload.get("reason", "unavailable"))
    return Sourced.live(payload["closes"], source)



def ohlcv_history(symbol: str, period: str = "1y") -> Sourced:
    """Full OHLCV frame data for the timeframe predictor, cached and throttled."""
    source = "yfinance:history-ohlcv"

    def produce():
        hist = _ticker(symbol).history(period=period, interval="1d")
        if hist is None or hist.empty:
            return {"ok": False, "reason": "no price history"}
        return {
            "ok": True,
            "index": [d.isoformat() for d in hist.index],
            "open": [None if o != o else float(o) for o in hist["Open"]],
            "high": [None if h != h else float(h) for h in hist["High"]],
            "low": [None if l != l else float(l) for l in hist["Low"]],
            "close": [None if c != c else float(c) for c in hist["Close"]],
            "volume": [0 if v != v else float(v) for v in hist["Volume"]],
        }

    payload = _cached(f"ohlcv:{symbol.upper()}:{period}", TTL_TECHNICALS, produce)
    if not payload.get("ok"):
        return Sourced.unavailable(source, payload.get("reason", "unavailable"))
    return Sourced.live(payload, source)


def ohlcv_frame(symbol: str, period: str = "1y"):
    """The cached OHLCV as a pandas DataFrame, or None."""
    sourced = ohlcv_history(symbol, period)
    if not sourced.ok:
        return None
    import pandas as pd

    data = sourced.value
    try:
        # utc=True: a year of exchange timestamps spans a DST change, so the
        # serialised offsets mix -04:00 and -05:00, which to_datetime refuses
        # to combine into a naive index.
        index = pd.to_datetime(data["index"], utc=True)
        frame = pd.DataFrame(
            {"Open": data["open"], "High": data["high"], "Low": data["low"],
             "Close": data["close"], "Volume": data["volume"]},
            index=index,
        )
        return frame.dropna(subset=["Close"])
    except (ValueError, KeyError, TypeError) as e:
        # A malformed cached payload must degrade to "no preloaded history",
        # not take the whole analysis route down with a 500.
        logger.warning(f"ohlcv frame rebuild failed for {symbol}: {type(e).__name__}: {e}")
        return None



FINRA_SHORT_URL = "https://cdn.finra.org/equity/regsho/daily/CNMSshvol{yyyymmdd}.txt"


def finra_short_volume(symbol: str) -> Sourced:
    """Reported short-sale share of volume, from FINRA's daily RegSHO file.

    One file covers every symbol, so it is fetched once per day and parsed
    into the cache; per-symbol reads never touch the network. Volumes in the
    feed are fractional share counts. T+1 data by nature, and labelled so.
    """
    source = "finra:regsho-daily"

    def produce():
        import requests as _rq
        from datetime import datetime as _dt, timedelta as _td
        import pytz as _pytz

        last_error = "no recent file found"
        # FINRA files are keyed to US trading dates; probing from the UTC date
        # after 8 PM Eastern asks for tomorrow's file first.
        probe = _dt.now(_pytz.timezone("America/New_York")).date()
        for _ in range(6):  # walk back over weekends/holidays
            url = FINRA_SHORT_URL.format(yyyymmdd=probe.strftime("%Y%m%d"))
            try:
                _throttle()
                response = _rq.get(url, timeout=30)
            except _rq.RequestException as e:
                return {"ok": False, "reason": f"finra unreachable ({type(e).__name__})"}
            if response.status_code == 200 and "|" in response.text[:100]:
                table = {}
                for line in response.text.splitlines()[1:]:
                    parts = line.split("|")
                    if len(parts) < 5:
                        continue
                    try:
                        short = float(parts[2])
                        total = float(parts[4])
                    except ValueError:
                        continue
                    if total > 0:
                        table[parts[1]] = {
                            "short_ratio": round(short / total, 4),
                            "short_volume": round(short),
                            "total_volume": round(total),
                        }
                if table:
                    return {"ok": True, "as_of": probe.isoformat(), "table": table}
                last_error = "file parsed empty"
            else:
                last_error = f"HTTP {response.status_code}"
            probe -= _td(days=1)
        return {"ok": False, "reason": last_error}

    payload = _cached("finra:daily", 20 * 60 * 60, produce)
    if not payload.get("ok"):
        return Sourced.unavailable(source, payload.get("reason", "unavailable"))
    row = payload["table"].get(symbol.upper())
    if not row:
        return Sourced.unavailable(source, "symbol not in FINRA consolidated file")
    return Sourced.live({**row, "as_of": payload["as_of"]}, source)



# --- SEC EDGAR: insider filings ---------------------------------------------
EDGAR_UA = os.environ.get("EDGAR_USER_AGENT",
                          "yahoo-losers-webapp/1.0 damien.adams@fcpeuro.com")
EDGAR_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
EDGAR_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"


def _edgar_cik_table():
    """Ticker-to-CIK map from the SEC, one fetch for every symbol, cached."""
    def produce():
        import requests as _rq
        try:
            _throttle()
            response = _rq.get(EDGAR_TICKERS_URL, headers={"User-Agent": EDGAR_UA}, timeout=30)
            response.raise_for_status()
            table = {row["ticker"].upper(): str(row["cik_str"]).zfill(10)
                     for row in response.json().values()}
        except Exception as e:
            return {"ok": False, "reason": f"cik map unavailable ({type(e).__name__})"}
        if not table:
            return {"ok": False, "reason": "cik map empty"}
        return {"ok": True, "table": table}

    return _cached("edgar:ciks", 7 * 24 * 60 * 60, produce)


MAX_FORM4_DOCS = 6  # per symbol per day; EDGAR courtesy plus request latency


def _parse_form4_xml(xml_text: str):
    """Open-market buys and sells from one Form 4's non-derivative table.

    Returns {"buy_value":, "sell_value":, "buys":, "sells":} or None when the
    document has no parseable transactions. Codes: P = open-market purchase,
    S = open-market sale. Grants, exercises and gifts are deliberately not
    counted -- a scheduled RSU vest says nothing about conviction.
    """
    import xml.etree.ElementTree as ET
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return None
    totals = {"buy_value": 0.0, "sell_value": 0.0, "buys": 0, "sells": 0,
              "unpriced": 0}
    found = False
    for txn in root.iter("nonDerivativeTransaction"):
        code = txn.findtext(".//transactionCoding/transactionCode")
        shares = txn.findtext(".//transactionShares/value")
        price = txn.findtext(".//transactionPricePerShare/value")
        if code not in ("P", "S") or not shares:
            continue
        # A priceless transaction cannot contribute a dollar figure; folding
        # it in at zero would understate a total the payload claims is real.
        if not price:
            totals["unpriced"] += 1
            found = True
            continue
        try:
            value = float(shares) * float(price)
        except ValueError:
            continue
        found = True
        if code == "P":
            totals["buy_value"] += value
            totals["buys"] += 1
        else:
            totals["sell_value"] += value
            totals["sells"] += 1
    return totals if found else None


def insider_filings(symbol: str, window_days: int = 90) -> Sourced:
    """Recent insider (Form 4) filing activity from SEC EDGAR, with direction.

    Reports the filing count in the window and, by fetching each filing's XML
    (capped at MAX_FORM4_DOCS), the aggregate open-market buy and sell dollar
    value. Filings that cannot be parsed are counted as unparsed rather than
    guessed at.
    """
    source = "sec-edgar:form4"

    ciks = _edgar_cik_table()
    if not ciks.get("ok"):
        return Sourced.unavailable(source, ciks.get("reason", "cik map unavailable"))
    cik = ciks["table"].get(symbol.upper())
    if not cik:
        return Sourced.unavailable(source, "ticker not in SEC registry")

    def produce():
        import requests as _rq
        from datetime import timedelta as _td
        try:
            _throttle()
            response = _rq.get(EDGAR_SUBMISSIONS_URL.format(cik=cik),
                               headers={"User-Agent": EDGAR_UA}, timeout=30)
            response.raise_for_status()
            payload = response.json()
        except Exception as e:
            return {"ok": False, "reason": f"edgar submissions unavailable ({type(e).__name__})"}

        recent = (payload.get("filings") or {}).get("recent") or {}
        forms = recent.get("form") or []
        dates = recent.get("filingDate") or []
        accessions = recent.get("accessionNumber") or []
        docs = recent.get("primaryDocument") or []
        cutoff = (datetime.now().date() - _td(days=window_days)).isoformat()
        # CR 2: amendments are insider activity too.
        rows = [(dates[i], accessions[i] if i < len(accessions) else None,
                 docs[i] if i < len(docs) else None)
                for i in range(min(len(forms), len(dates)))
                if forms[i] in ("4", "4/A")]
        in_window = [r for r in rows if r[0] >= cutoff]

        # Direction: fetch each filing's XML, newest first, capped. The
        # primaryDocument is sometimes the styled view (xslF345X.../doc.xml);
        # stripping the stylesheet path yields the raw XML EDGAR also serves.
        totals = {"buy_value": 0.0, "sell_value": 0.0, "buys": 0, "sells": 0,
                  "unpriced": 0}
        parsed = unparsed = fetch_failed = 0
        for _fdate, accession, doc in in_window[:MAX_FORM4_DOCS]:
            if not accession or not doc:
                unparsed += 1
                continue
            doc = doc.split("/")[-1]
            url = (f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/"
                   f"{accession.replace('-', '')}/{doc}")
            try:
                _throttle()
                doc_response = _rq.get(url, headers={"User-Agent": EDGAR_UA}, timeout=20)
                doc_response.raise_for_status()
            except Exception as e:
                # A fetch failure is not an unparseable filing; conflating
                # them hid SEC blocks inside a 24h-cached "unparsed" count.
                logger.warning(f"form4 fetch failed for {symbol} {accession}: {type(e).__name__}")
                fetch_failed += 1
                continue
            txns = _parse_form4_xml(doc_response.text)
            if txns is None:
                unparsed += 1
                continue
            parsed += 1
            for key in totals:
                totals[key] += txns[key]

        result = {
            "ok": True,
            "count": len(in_window),
            "latest": rows[0][0] if rows else None,
            "window_days": window_days,
            "filings_url": f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}&type=4&dateb=&owner=include&count=20",
            "parsed": parsed,
            "unparsed": unparsed,
            "fetch_failed": fetch_failed,
            "note": (f"direction from the {parsed} most recent parseable filings; "
                     "open-market P/S transactions only, grants and exercises excluded"),
        }
        if parsed:
            result.update({k: round(v, 2) if "value" in k else v for k, v in totals.items()})
            result["net_value"] = round(totals["buy_value"] - totals["sell_value"], 2)
        return result

    # v2 key: count-only payloads cached under the old key must not serve for
    # a schema that now includes direction fields.
    payload = _cached(f"edgar:form4v2:{symbol.upper()}:{window_days}", 24 * 60 * 60, produce)
    if not payload.get("ok"):
        return Sourced.unavailable(source, payload.get("reason", "unavailable"))
    return Sourced.live({k: v for k, v in payload.items() if k != "ok"}, source)


EDGAR_FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"

# us-gaap concepts, in preference order per field. Issuers tag the same idea
# under different concepts; the first one reported wins.
XBRL_CONCEPTS = {
    "cash": ("CashAndCashEquivalentsAtCarryingValue",
             "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"),
    "debt": ("LongTermDebt", "LongTermDebtNoncurrent", "DebtLongtermAndShorttermCombinedAmount"),
    "assets_current": ("AssetsCurrent",),
    "liabilities_current": ("LiabilitiesCurrent",),
    "operating_cash_flow": ("NetCashProvidedByUsedInOperatingActivities",
                            "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"),
    "capex": ("PaymentsToAcquirePropertyPlantAndEquipment",),
}


def sec_fundamentals(symbol: str) -> Sourced:
    """Balance-sheet and cash-flow figures straight from SEC XBRL filings.

    companyfacts serves every USD fact a company has ever tagged; this keeps
    the latest-dated value per concept. Filed numbers beat yfinance's scraped
    profile fields -- they are the audited source those fields derive from.
    """
    source = "sec-edgar:xbrl-companyfacts"
    ciks = _edgar_cik_table()
    if not ciks.get("ok"):
        return Sourced.unavailable(source, ciks.get("reason", "cik map unavailable"))
    cik = ciks["table"].get(symbol.upper())
    if not cik:
        return Sourced.unavailable(source, "ticker not in SEC registry")

    def produce():
        import requests as _rq
        try:
            _throttle()
            response = _rq.get(EDGAR_FACTS_URL.format(cik=cik),
                               headers={"User-Agent": EDGAR_UA}, timeout=30)
            response.raise_for_status()
            gaap = (response.json().get("facts") or {}).get("us-gaap") or {}
        except Exception as e:
            return {"ok": False, "reason": f"edgar companyfacts unavailable ({type(e).__name__})"}

        def latest(concepts):
            """Most recent USD value across the acceptable concepts.

            Returns (end, value, start). Balance-sheet facts are instants and
            carry start=None; flow facts keep their period start so two flows
            are only ever combined when their periods actually match.
            """
            best = None
            for concept in concepts:
                for item in ((gaap.get(concept) or {}).get("units") or {}).get("USD") or []:
                    end, value = item.get("end"), item.get("val")
                    if end and isinstance(value, (int, float)):
                        if best is None or end > best[0]:
                            best = (end, float(value), item.get("start"))
                if best:
                    break  # preference order: don't mix concepts
            return best

        fields, dates, periods = {}, {}, {}
        for name, concepts in XBRL_CONCEPTS.items():
            hit = latest(concepts)
            if hit:
                dates[name], fields[name], periods[name] = hit[0], hit[1], hit[2]
        if not fields:
            return {"ok": False, "reason": "no USD facts tagged for this issuer"}
        out = {"ok": True, "as_of": max(dates.values())}
        out.update(fields)
        # FCF only from flows covering the same reporting period -- a quarterly
        # OCF minus an annual capex is an accounting fiction, not a figure.
        ocf, capex = fields.get("operating_cash_flow"), fields.get("capex")
        if (ocf is not None and capex is not None
                and dates.get("operating_cash_flow") == dates.get("capex")
                and periods.get("operating_cash_flow") == periods.get("capex")):
            out["free_cash_flow"] = ocf - abs(capex)
        ac, lc = fields.get("assets_current"), fields.get("liabilities_current")
        if ac is not None and lc:
            out["current_ratio"] = round(ac / lc, 2)
        return out

    payload = _cached(f"edgar:facts:{symbol.upper()}", 24 * 60 * 60, produce)
    if not payload.get("ok"):
        return Sourced.unavailable(source, payload.get("reason", "unavailable"))
    return Sourced.live({k: v for k, v in payload.items() if k != "ok"}, source)


def solvency(symbol: str, allow_fetch: bool = True) -> Sourced:
    """Balance-sheet posture: cash, debt, cash flow, current ratio.

    A profitable company knocked down ten percent and a cash-burner knocked
    down ten percent are different bets. Figures come from SEC XBRL filings
    when the issuer has them tagged (the audited source), falling back to
    yfinance's profile fields; the payload names which one supplied them.
    """
    facts = sec_fundamentals(symbol) if allow_fetch else Sourced.unavailable(
        "sec-edgar:xbrl-companyfacts", "fetch not allowed on this path")
    info = _info(symbol, allow_fetch)

    current_ratio = facts_as_of = None
    if facts.ok:
        source = facts.source
        value = facts.value
        cash, debt = value.get("cash"), value.get("debt")
        fcf = value.get("free_cash_flow")
        margins = info.get("profit_margins") if info.get("ok") else None
        current_ratio, facts_as_of = value.get("current_ratio"), value.get("as_of")
    elif info.get("ok"):
        source = "yfinance:balance-sheet-fields"
        cash, debt = info.get("total_cash"), info.get("total_debt")
        fcf, margins = info.get("free_cashflow"), info.get("profit_margins")
    else:
        return Sourced.unavailable("yfinance:balance-sheet-fields",
                                   info.get("reason", "unavailable"))

    # Missing values stay missing. (cash or 0) would report an absent figure
    # as a zero balance, and a posture claimed from absent free cash flow is
    # an assertion nobody measured -- the same fabrication class this project
    # removes everywhere else.
    net_cash = (cash - debt) if (cash is not None and debt is not None) else None

    parts = []
    if net_cash is not None:
        parts.append("Net cash" if net_cash > 0 else "Net debt")
    if fcf is not None:
        parts.append("burning cash" if fcf < 0 else "cash-flow positive")
    if current_ratio is not None:
        parts.append(f"current ratio {current_ratio}")

    if not parts:
        return Sourced.unavailable(source, "cash, debt and free cash flow all unreported")

    label = ", ".join(parts)
    if fcf is not None and fcf < 0 and net_cash is not None and net_cash < 0:
        tone = "risk"
    elif fcf is not None and fcf < 0:
        tone = "caution"
    elif net_cash is not None and net_cash > 0:
        tone = "solid"
    else:
        tone = "neutral"

    known = [name for name, value in
             (("cash", cash), ("debt", debt), ("free cash flow", fcf),
              ("current ratio", current_ratio)) if value is not None]
    basis_origin = ("SEC-filed XBRL" if source == "sec-edgar:xbrl-companyfacts"
                    else "yfinance-reported")
    return Sourced.derived({
        "label": label,
        "tone": tone,
        "total_cash": cash,
        "total_debt": debt,
        "net_cash": net_cash,
        "free_cashflow": fcf,
        "current_ratio": current_ratio,
        "facts_as_of": facts_as_of,
        "profit_margins": margins,
        # CR 4: the documented field name for derived payloads.
        "estimate_basis": f"derived from {basis_origin} {', '.join(known)} only; "
                          "unreported fields are omitted, not zeroed",
    }, source)



# --- Sector context ----------------------------------------------------------
# yfinance sector names -> SPDR sector ETF. The ETF's same-day move separates
# "the whole sector sold off" from "this company specifically fell".
SECTOR_ETFS = {
    "Technology": "XLK",
    "Financial Services": "XLF",
    "Consumer Cyclical": "XLY",
    "Consumer Defensive": "XLP",
    "Healthcare": "XLV",
    "Energy": "XLE",
    "Industrials": "XLI",
    "Utilities": "XLU",
    "Real Estate": "XLRE",
    "Communication Services": "XLC",
    "Basic Materials": "XLB",
}

# Day-move thresholds, in percent. Below -1% the sector itself is clearly
# selling off; above -0.3% it clearly is not. The flat band's ceiling keeps a
# sector rally from matching a "sector flat" condition.
SECTOR_SELLOFF_PCT = -1.0
SECTOR_FLAT_PCT = -0.3
SECTOR_FLAT_UPPER_PCT = 1.5


def _day_move_pct(payload: dict):
    """Percent change between the last two daily closes of a hist: payload."""
    closes = payload.get("closes") or []
    if len(closes) < 2 or not closes[-2]:
        return None
    return (closes[-1] - closes[-2]) / closes[-2] * 100.0


def sector_context(symbol: str) -> Sourced:
    """Was the stock's fall sector-wide or company-specific? Cache-only.

    Reads the symbol's sector from the cached profile and the sector ETF's
    day move from the cached history the fast lane keeps warm. Never fetches:
    a render must not wait on providers, and an unavailable answer is honest.
    """
    source = "derived:sector-etf-day-move"
    info = _info(symbol, allow_fetch=False)
    if not info.get("ok"):
        return Sourced.unavailable(source, "sector not cached yet")
    sector = info.get("sector")
    etf = SECTOR_ETFS.get(sector or "")
    if not etf:
        return Sourced.unavailable(source, f"no sector ETF mapping for {sector!r}")

    etf_hist = _cache.get(f"hist:{etf}:5y")
    etf_move = _day_move_pct(etf_hist) if etf_hist and etf_hist.get("ok") else None
    if etf_move is None:
        return Sourced.unavailable(source, f"{etf} history not warmed yet")

    if etf_move <= SECTOR_SELLOFF_PCT:
        classification, label = "sector_wide", f"sector-wide selloff ({sector} {etf_move:+.1f}%)"
    elif etf_move >= SECTOR_FLAT_PCT:
        classification, label = "company_specific", f"company-specific ({sector} {etf_move:+.1f}%)"
    else:
        classification, label = "mixed", f"mixed ({sector} {etf_move:+.1f}%)"
    return Sourced.derived({
        "sector": sector,
        "etf": etf,
        "etf_day_move_pct": round(etf_move, 2),
        "classification": classification,
        "label": label,
        "estimate_basis": f"{etf} last two daily closes vs thresholds "
                          f"{SECTOR_SELLOFF_PCT}%/{SECTOR_FLAT_PCT}%",
    }, source)


# --- FRED macro series -------------------------------------------------------
FRED_OBS_URL = "https://api.stlouisfed.org/fred/series/observations"
FRED_MACRO_SERIES = {
    "T10Y2Y": "10y-2y Treasury spread",
    "BAMLH0A0HYM2": "High-yield OAS",
}


def fred_latest(series_id: str, allow_fetch: bool = True) -> Sourced:
    """Latest observation of a FRED series, cached for six hours.

    Kept off the render path: the warmer refreshes these alongside the
    indices, and the overview card reads cache-only.
    """
    source = f"fred:{series_id}"
    import secrets_store
    api_key = secrets_store.get("FRED_API_KEY")
    if not api_key:
        return Sourced.unavailable(source, "FRED_API_KEY not configured")

    def produce():
        import requests as _rq
        try:
            _throttle()
            response = _rq.get(FRED_OBS_URL, params={
                "series_id": series_id, "api_key": api_key, "file_type": "json",
                "sort_order": "desc", "limit": 5}, timeout=20)
            response.raise_for_status()
            observations = response.json().get("observations", [])
        except Exception as e:
            return {"ok": False, "reason": f"fred unavailable ({type(e).__name__})"}
        for obs in observations:
            raw = obs.get("value")
            if raw in (None, "", "."):
                continue
            try:
                return {"ok": True, "value": float(raw), "as_of": obs["date"]}
            except (TypeError, ValueError):
                continue  # malformed row; keep walking to older observations
        return {"ok": False, "reason": "no numeric observations"}

    payload = _cached(f"fred:{series_id}", 6 * 60 * 60, produce, allow_fetch)
    if not payload.get("ok"):
        return Sourced.unavailable(source, payload.get("reason", "unavailable"))
    return Sourced.live({"value": payload["value"], "as_of": payload["as_of"],
                         "label": FRED_MACRO_SERIES.get(series_id, series_id)}, source)


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

# Per-cycle ceiling on uncached profile fetches. This was 12 when fetching
# still happened inside page renders, where 25 rapid calls from one datacenter
# IP read as a burst and got the IP banned. Fetching now lives on the
# background thread with a 0.8s gap between calls, and 30 paced calls is a
# steady trickle, not a burst -- so a single cycle covers the whole losers
# list and the cache is complete in one pass instead of dribbling in.
MAX_PROFILES_PER_WARM = int(os.environ.get("MARKET_DATA_MAX_PROFILES", 30))



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
        "fetched_at": time.time(),
        "close": last_close,
        "rsi14": round(float(rsi), 2),
        "percent_b": round(float(percent_b), 3) if percent_b is not None and percent_b == percent_b else None,
        "ma20": round(ma20_last, 4),
        "pct_from_ma20": round((last_close - ma20_last) / ma20_last, 4),
        "volume_ratio_20d": round(latest_volume / avg_volume_20, 2) if avg_volume_20 and latest_volume else None,
        "drawdown_from_6mo_peak": round((last_close - peak) / peak, 4) if peak else None,
        "bars": int(len(close)),
    }


# How often the latest bar is refreshed during a session. This is ONE batched
# chart request for the whole board -- the cheap, batch-friendly endpoint that
# has never tripped the limiter (every observed trip was per-symbol
# quoteSummary). Twelve tiny requests an hour is noise next to that history.
PRICE_REFRESH_SECONDS = int(os.environ.get("MARKET_DATA_PRICE_REFRESH", 300))
_last_price_refresh = [0.0]

# App-injected failover: {symbol: price} for the current moment, from a
# non-Yahoo provider, used when the batched chart refresh returns nothing.
_price_failover_source = [None]


def set_price_failover(fn):
    _price_failover_source[0] = fn


def refresh_last_bar(symbols: List[str]) -> int:
    """Update just the latest daily bar for many symbols in one request.

    The full five-year history and the indicators built from it genuinely
    change on a thirty-minute clock, but the LAST bar -- the price the header
    stamps and the day-move everything conditions on -- is worth keeping
    minutes-fresh. One batched five-day download patches the tail of the
    hist:/tech:/ohlcv: caches in place; alignment is keyed off the ohlcv
    dates so a bar is replaced or appended, never duplicated.
    """
    symbols = [s.upper() for s in symbols if s]
    if not symbols:
        return 0
    _throttle()
    try:
        frame = yf.download(symbols, period="5d", interval="1d",
                            group_by="ticker", auto_adjust=True,
                            progress=False, threads=False)
    except Exception as e:
        detail = f"{type(e).__name__}: {e}"
        logger.warning(f"last-bar refresh failed for {len(symbols)} symbols: {detail}")
        if _is_rate_limited(detail):
            _warm_backoff_until[0] = time.time() + 180
        return 0
    if frame is None or frame.empty:
        # Yahoo gave nothing: patch same-day closes from the failover
        # provider so the board's prices stay live through the outage.
        failover = _price_failover_source[0]
        if failover is None:
            return 0
        try:
            prices = failover(symbols) or {}
        except Exception as e:
            logger.warning(f"price failover failed: {type(e).__name__}")
            return 0
        today_iso = _eastern_now().date().isoformat()
        patched = 0
        for symbol, price in prices.items():
            ohlcv = _cache.get(f"ohlcv:{symbol}:1y")
            hist = _cache.get(f"hist:{symbol}:5y")
            if not (ohlcv and ohlcv.get("ok") and hist and hist.get("ok")):
                continue
            # Same-session bars only: on a fresh session with an empty Yahoo
            # frame, overwriting yesterday's close with today's live price
            # would destroy the prior bar (CR Critical, PR 55). Appending
            # without provider OHLC would be worse; skip until Yahoo returns.
            if (ohlcv.get("index") or [""])[-1][:10] != today_iso:
                continue
            closes = list(hist["closes"])
            closes[-1] = float(price)
            series = list(ohlcv.get("close") or [])
            if series:
                series[-1] = float(price)
                ohlcv["close"] = series
            highs = list(ohlcv.get("high") or [])
            if highs and highs[-1] is not None:
                highs[-1] = max(highs[-1], float(price))
                ohlcv["high"] = highs
            ttl = _effective_ttl(TTL_TECHNICALS)
            _cache.set(f"ohlcv:{symbol}:1y", ohlcv, ttl)
            _cache.set(f"hist:{symbol}:5y",
                       {"ok": True, "closes": closes,
                        "full_fetched_at": hist.get("full_fetched_at")}, ttl)
            patched += 1
        if patched:
            logger.info(f"price failover patched {patched} symbols")
        return patched

    updated = 0
    for symbol in symbols:
        try:
            sub = frame[symbol] if len(symbols) > 1 else frame
            recent = sub.dropna(subset=["Close"])
            if recent.empty:
                continue
            bar = recent.iloc[-1]
            bar_date = recent.index[-1].isoformat()[:10]
            last_close = float(bar["Close"])
            if last_close <= 0:
                continue
        except Exception:
            continue

        ohlcv = _cache.get(f"ohlcv:{symbol}:1y")
        hist = _cache.get(f"hist:{symbol}:5y")
        if not (ohlcv and ohlcv.get("ok") and ohlcv.get("index")
                and hist and hist.get("ok") and hist.get("closes")):
            continue  # cold caches take the full batch_history path instead

        same_day = ohlcv["index"][-1][:10] == bar_date
        closes = list(hist["closes"])
        for field, value in (("open", bar.get("Open")), ("high", bar.get("High")),
                             ("low", bar.get("Low")), ("close", last_close),
                             ("volume", bar.get("Volume"))):
            series = list(ohlcv.get(field) or [])
            if not series:
                # Pad, never skip: a shorter series desynchronizes from the
                # index every consumer zips against (CR, PR 53).
                series = [None] * len(ohlcv["index"])
            cleaned = (None if value is None or value != value else float(value))
            if same_day:
                series[-1] = cleaned
            else:
                series.append(cleaned)
                series = series[-260:]
            ohlcv[field] = series
        if not same_day:
            ohlcv["index"] = (list(ohlcv["index"]) + [bar_date])[-260:]
            closes.append(last_close)
        else:
            ohlcv["index"] = list(ohlcv["index"])
            closes[-1] = last_close

        volumes = [0 if v is None else v for v in (ohlcv.get("volume") or [0] * len(closes))]
        ttl = _effective_ttl(TTL_TECHNICALS)
        _cache.set(f"ohlcv:{symbol}:1y", ohlcv, ttl)
        _cache.set(f"hist:{symbol}:5y",
                   {"ok": True, "closes": closes,
                    "full_fetched_at": hist.get("full_fetched_at")}, ttl)
        tech_payload = _compute_technicals_from_closes(closes[-130:], volumes[-130:])
        _cache.set(f"tech:{symbol}", tech_payload,
                   ttl if tech_payload.get("ok") else TTL_NEGATIVE_TRANSIENT)
        updated += 1
    if updated:
        logger.info(f"last-bar refresh updated {updated}/{len(symbols)} symbols in one request")
    return updated


def batch_history(symbols: List[str], period: str = "5y") -> int:
    """Fetch price history for many symbols in a single request.

    This is the change that keeps the app inside Yahoo's limits. Fetching 25
    symbols individually meant 25 requests per refresh; yf.download retrieves
    them all in one, and the result populates both the technicals and the
    raw-closes caches so no per-symbol call is needed afterwards.
    """
    symbols = [s.upper() for s in symbols]

    def _full_history_stale(s):
        # refresh_last_bar rewrites these keys every few minutes with fresh
        # TTLs, so absence alone would never trigger a re-fetch and the
        # provider's re-adjusted history (splits, dividends) would never
        # replace the tail-patched copy (CR, PR 53). The stamp of the last
        # FULL download breaks that loop.
        hist = _cache.get(f"hist:{s}:{period}")
        if hist is None or _cache.get(f"tech:{s}") is None \
                or _cache.get(f"ohlcv:{s}:1y") is None:
            return True
        fetched = hist.get("full_fetched_at")
        return fetched is None or (time.time() - fetched) > _effective_ttl(TTL_TECHNICALS)

    pending = [s for s in symbols if _full_history_stale(s)]
    if not pending:
        return 0

    _throttle()
    try:
        frame = yf.download(pending, period=period, interval="1d",
                            group_by="ticker", auto_adjust=True,
                            progress=False, threads=False)
    except Exception as e:
        detail = f"{type(e).__name__}: {e}"
        logger.warning(f"batch history failed for {len(pending)} symbols: {detail}")
        if _is_rate_limited(detail):
            _warm_backoff_until[0] = time.time() + 180
            logger.warning("rate limiter engaged; warmer backing off 180s")
        return 0

    if frame is None or frame.empty:
        # An empty 200 is how blocking/limiting actually manifests --
        # yf.download swallows per-ticker errors internally. Silence here
        # meant the limiter was re-tripped every cycle with no trace.
        logger.warning(f"batch history returned an empty frame for {len(pending)} symbols; "
                       "treating as a suspected limit and backing off 90s")
        _warm_backoff_until[0] = max(_warm_backoff_until[0], time.time() + 90)
        return 0

    populated = 0
    skipped = []
    for symbol in pending:
        try:
            # yfinance returns a flat frame for one symbol and a MultiIndex for many.
            sub = frame[symbol] if len(pending) > 1 else frame
            closes = [float(c) for c in sub["Close"].dropna().tolist() if c and c > 0]
            volumes = [float(v) for v in sub["Volume"].fillna(0).tolist()]
        except Exception as e:
            skipped.append(f"{symbol}({type(e).__name__})")
            continue
        if len(closes) < 30:
            skipped.append(f"{symbol}(only {len(closes)} bars)")
            continue

        _cache.set(f"hist:{symbol}:{period}",
                   {"ok": True, "closes": closes, "full_fetched_at": time.time()},
                   _effective_ttl(TTL_TECHNICALS))
        tech_payload = _compute_technicals_from_closes(closes[-130:], volumes[-130:])
        _cache.set(f"tech:{symbol}", tech_payload,
                   _effective_ttl(TTL_TECHNICALS) if tech_payload.get("ok")
                   else TTL_NEGATIVE_TRANSIENT)
        # The same response carries the highs; storing the last year in the
        # ohlcv shape gives intraday-touch odds to every board row without a
        # single extra request.
        try:
            recent = sub.dropna(subset=["Close"]).tail(260)
            _cache.set(f"ohlcv:{symbol}:1y", {
                "ok": True,
                "index": [d.isoformat() for d in recent.index],
                "open": [None if o != o else float(o) for o in recent["Open"]],
                "high": [None if h != h else float(h) for h in recent["High"]],
                "low": [None if low != low else float(low) for low in recent["Low"]],
                "close": [None if c != c else float(c) for c in recent["Close"]],
                "volume": [0 if v != v else float(v) for v in recent["Volume"]],
            }, _effective_ttl(TTL_TECHNICALS))
        except Exception as e:
            logger.debug(f"ohlcv store skipped for {symbol}: {type(e).__name__}")
        populated += 1

    if skipped:
        level = logger.warning if len(skipped) > len(pending) / 2 else logger.info
        level(f"batch history skipped {len(skipped)}: {', '.join(skipped[:8])}"
              + (" ..." if len(skipped) > 8 else ""))
    logger.info(f"batch history populated {populated}/{len(pending)} symbols in one request")
    return populated



# --- Background warming -----------------------------------------------------
#
# Provider calls must never happen inside a page render. Render's platform
# health check issues HEAD / on a schedule, so with fetching in the request path
# every health check triggered a full cold refresh -- the logs showed single
# requests taking 31, 45 and 54 seconds while the limiter engaged. Two gunicorn
# workers without a shared Redis doubled it, and concurrent requests for the
# same symbol produced four and five duplicate fetches.
#
# Fetching now happens on one background thread, paced, in exactly one worker.
# Renders read cache only, so a page load costs nothing upstream and the cache
# fills in over time.

WARM_INTERVAL_SECONDS = int(os.environ.get("MARKET_DATA_WARM_INTERVAL", 45))
WARM_STARTUP_DELAY_SECONDS = int(os.environ.get("MARKET_DATA_WARM_DELAY", 10))
# One worker holds the provider-warming lease at a time (Redis SET NX with a
# rolling expiry). Without Redis every process warms for itself, as before.
WARM_LEASE_KEY = f"md:{CACHE_SCHEMA_VERSION}:warmer-lease"
WARM_LEASE_SECONDS = 180
# Unique per process: two containers can share a PID, and a PID-keyed lease
# would let both believe they hold it (CR, PR 53).
import uuid as _uuid
_WARM_LEASE_TOKEN = _uuid.uuid4().hex


def _holds_warm_lease() -> bool:
    """True when this process should do provider warming this cycle.

    With shared Redis, two gunicorn workers each running the lanes meant
    every provider call happened twice for one cache. The lease costs one
    Redis round-trip per cycle and halves provider load; renewal keeps it,
    and a crashed holder's lease lapses within WARM_LEASE_SECONDS.
    """
    redis_client = _cache._redis
    if redis_client is None:
        return True
    me = _WARM_LEASE_TOKEN
    try:
        if redis_client.set(WARM_LEASE_KEY, me, nx=True, ex=WARM_LEASE_SECONDS):
            return True
        holder = redis_client.get(WARM_LEASE_KEY)
        holder = holder.decode() if isinstance(holder, bytes) else holder
        if holder == me:
            redis_client.expire(WARM_LEASE_KEY, WARM_LEASE_SECONDS)
            return True
        return False
    except Exception:
        return True  # a broken Redis must not stop warming entirely

# The warmer must be able to find work on its own. Queueing only happened from
# the page render, but with a persistent page cache that render stops running --
# so the queue stayed empty and the cache never filled. The warmer now pulls the
# current universe itself on each idle cycle.
_symbol_source = [None]


def set_symbol_source(fn):
    """Register a callable returning the symbols worth keeping warm."""
    _symbol_source[0] = fn


_warm_queue: List[str] = []
# When the provider rate-limits a batch, the warmer sleeps past this moment
# instead of re-tripping the limiter every cycle.
_warm_backoff_until = [0.0]
_warm_queue_lock = threading.Lock()
_warmer_started = False
def request_warm(symbols: List[str]) -> None:
    """Queue symbols for background warming. Never blocks the caller."""
    with _warm_queue_lock:
        known = set(_warm_queue)
        for symbol in symbols:
            upper = symbol.upper()
            if upper not in known and _cache.get(f"info:{upper}") is None:
                _warm_queue.append(upper)


def _symbols_missing_factor_keys(symbols):
    """Universe subsets whose ratings/options factor caches are cold.

    These factors previously warmed only as a side effect of profile expiry
    in the dead pre-lane warm(): with profiles kept warm by this lane, a
    fresh key family or an outage-cleared cache left the whole board at 4/6
    inputs indefinitely (live 2026-08-19)."""
    live = [s for s in symbols if not s.startswith("^")]
    recs = [s for s in live if _cache.get(_recs_key(s)) is None]
    options = [s for s in live if _cache.get(_options_key(s)) is None]
    return recs, options


def _symbols_needing_target_fallback(symbols):
    """Symbols whose consensus factor has no usable Yahoo mean and no cached
    FMP fallback yet. A healthy profile keeps this empty, so FMP budget is
    spent only while quoteSummary is blocked or coverage is absent."""
    out = []
    for s in symbols:
        if s.startswith("^"):
            continue
        if _cache.get(f"src:targets:{s.upper()}") is not None:
            continue
        info = _cache.get(f"info:{s.upper()}")
        if info is None:
            continue  # profile drain owns it first; revisit next tick
        if not info.get("ok") or not info.get("target_mean"):
            out.append(s)
    return out


def _symbols_needing_short_fallback(symbols):
    """Symbols whose short-interest factor has no usable Yahoo value and no
    cached FINRA fallback yet. Healthy profiles keep this empty."""
    out = []
    for s in symbols:
        if s.startswith("^"):
            continue
        if _cache.get(f"src:shortfloat:{s.upper()}") is not None:
            continue
        info = _cache.get(f"info:{s.upper()}")
        if info is None:
            continue  # profile drain owns it first; revisit next tick
        if not info.get("ok") or info.get("short_pct_float") is None:
            out.append(s)
    return out


def _symbols_missing_info(symbols):
    """The subset whose profile is absent from cache -- the info lane's queue."""
    return [s for s in symbols
            if not s.startswith("^") and _cache.get(f"info:{s.upper()}") is None]


def _info_loop():
    """Slow lane: drains missing profiles at the adaptive pace, independently.

    This lane crawling -- ninety seconds between calls under a provider
    penalty -- must never delay the fast lane. When it ran inside the same
    cycle, twenty-five paced info calls blocked the next technicals refresh
    for over half an hour and the whole board's scores decayed to zero while
    the loop was still "working". The monitor caught it: 14, 12, 8, 4, 0.
    """
    time.sleep(WARM_STARTUP_DELAY_SECONDS + 5)
    while True:
        try:
            source = _symbol_source[0]
            universe = [s for s in (source() if source else []) if s and not s.startswith("^")]
            if not _holds_warm_lease():
                time.sleep(30)
                continue
            missing = _symbols_missing_info(universe)
            # EDGAR going-concern context rides this lane too: separate
            # provider, own throttle, 24h cache. The board chip must read
            # warm rather than fetch on a render.
            gc_missing = [s for s in universe if _cache.get(f"gc:{s.upper()}") is None]
            import sources as _src
            grades_missing = [s for s in universe
                              if _cache.get(f"src:grades:{s.upper()}:"
                                            f"{_src.GRADES_WINDOW_DAYS}") is None]
            earnings_missing = [s for s in universe
                                if _cache.get(f"src:earnings:{s.upper()}") is None]
            recs_missing, options_missing = _symbols_missing_factor_keys(universe)
            targets_missing = _symbols_needing_target_fallback(universe)
            short_missing = _symbols_needing_short_fallback(universe)
            calendar_cold = _cache.get("src:trading-days") is None
            if (not missing and not gc_missing and not grades_missing
                    and not earnings_missing and not recs_missing
                    and not options_missing and not targets_missing
                    and not short_missing and not calendar_cold):
                time.sleep(20)
                continue
            for symbol in missing[:5]:
                _info(symbol)   # adaptive lane pacing happens inside
            for symbol in gc_missing[:5]:
                try:
                    going_concern(symbol)
                except Exception as e:
                    logger.warning(f"gc warm skipped for {symbol}: {type(e).__name__}")
            # Analyst revisions + confirmed earnings ride the same slow lane:
            # 24h-cached per symbol, FMP budget enforced inside sources. The
            # missing-lists were computed BEFORE the idle guard, so steady
            # state cannot starve this block (CR, PR 55).
            try:
                if calendar_cold:
                    _src.trading_days_set()
                for symbol in grades_missing[:3]:
                    _src.analyst_grades(symbol)
                for symbol in earnings_missing[:3]:
                    _src.earnings_confirmed(symbol)
            except Exception as e:
                logger.warning(f"sources warm skipped: {type(e).__name__}")
            # The two score factors with independent backups drain here too.
            # produce() owns cooldown and failover, so a limited Yahoo costs
            # one fallback call per symbol, never a blocked lane.
            for symbol in recs_missing[:3]:
                try:
                    analyst_recommendations(symbol)
                except Exception as e:
                    logger.warning(f"recs warm skipped for {symbol}: {type(e).__name__}")
            for symbol in options_missing[:3]:
                try:
                    options_flow(symbol)
                except Exception as e:
                    logger.warning(f"options warm skipped for {symbol}: {type(e).__name__}")
            for symbol in targets_missing[:3]:
                try:
                    analyst_target(symbol)
                except Exception as e:
                    logger.warning(f"targets warm skipped for {symbol}: {type(e).__name__}")
            for symbol in short_missing[:2]:
                try:
                    profile(symbol)
                except Exception as e:
                    logger.warning(f"short-float warm skipped for {symbol}: {type(e).__name__}")
            save_cache_to_disk()
        except Exception as e:
            logger.warning(f"info lane cycle failed: {type(e).__name__}: {e}")
            time.sleep(15)


def _warm_loop():
    # Fast lane. Everything here is batched or cheap, and nothing in it may
    # wait on the info lane.
    time.sleep(WARM_STARTUP_DELAY_SECONDS)
    while True:
      # Whole-body guard, same as the info lane: an unexpected exception in
      # any step (including persistence) must cost one cycle, never the
      # thread -- a dead fast lane decays the entire board with only a
      # stderr traceback to show for it.
      try:
        with _warm_queue_lock:
            empty = not _warm_queue
        if empty and _symbol_source[0] is not None:
            try:
                request_warm(_symbol_source[0]())
            except Exception as e:
                logger.warning(f"symbol source failed: {type(e).__name__}: {e}")

        # The backoff written on a rate-limit refusal is honoured here --
        # previously it was written, logged, and never read, so the loop
        # re-tripped the limiter every cycle while claiming to back off.
        if time.time() < _warm_backoff_until[0]:
            time.sleep(min(30.0, _warm_backoff_until[0] - time.time()))
            continue

        if not _holds_warm_lease():
            time.sleep(WARM_INTERVAL_SECONDS)
            continue

        with _warm_queue_lock:
            batch = _warm_queue[:MAX_PROFILES_PER_WARM]
            del _warm_queue[:len(batch)]
        # The market-overview card reads ^VIX and SPY from this cache. They
        # refresh every cycle regardless of the queue, because a fully-warmed
        # universe leaves the queue empty and their TTL would otherwise lapse
        # with nothing to renew it.
        try:
            # Sector ETFs ride in the same single batched request -- eleven
            # more columns in one call, not eleven calls.
            batch_history(["^VIX", "SPY"] + sorted(set(SECTOR_ETFS.values())))
            for series in FRED_MACRO_SERIES:
                fred_latest(series)
        except Exception as e:
            logger.warning(f"index warm failed: {type(e).__name__}")

        # Persist whatever the index/macro refresh just wrote, even when the
        # symbol queue is empty -- otherwise a restart on a quiet cycle
        # restores stale FRED values from the last busy one.
        save_cache_to_disk()

        try:
            source = _symbol_source[0]
            universe = [s for s in (source() if source else []) if s and not s.startswith("^")]
            if universe:
                # One batched call for the whole board: cheap, and it is what
                # keeps technicals -- and therefore scores -- alive. Cached
                # entries make it a no-op until their TTLs approach.
                batch_history(universe)
                finra_short_volume(universe[0])
                # During any trading session, keep the LAST bar minutes-fresh:
                # one tiny batched request on its own five-minute clock, so
                # "prices as of" tracks the session instead of the 30-minute
                # indicator TTL. Skipped while closed -- nothing moves.
                if (market_phase()["phase"] != "closed"
                        and time.time() - _last_price_refresh[0] >= PRICE_REFRESH_SECONDS):
                    refresh_last_bar(universe + ["SPY", "^VIX"])
                    _last_price_refresh[0] = time.time()
                save_cache_to_disk()
        except Exception as e:
            logger.warning(f"fast-lane warm failed: {type(e).__name__}: {e}")
      except Exception as e:
        logger.warning(f"fast-lane cycle failed whole: {type(e).__name__}: {e}")
        time.sleep(15)
        continue
      time.sleep(WARM_INTERVAL_SECONDS)


def start_background_warmer():
    """Start the warmer once, in a single worker."""
    global _warmer_started
    if _warmer_started or os.environ.get("MARKET_DATA_DISABLE_WARMER"):
        return False
    _warmer_started = True
    threading.Thread(target=_warm_loop, daemon=True, name="market-data-warmer").start()
    threading.Thread(target=_info_loop, daemon=True, name="market-data-info-lane").start()
    logger.info("background warmer started (fast lane + info lane)")
    return True

