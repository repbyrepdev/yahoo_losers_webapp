"""Multi-provider failover layer: Alpaca, Finnhub, FMP beside Yahoo.

One provider was a single point of failure -- every observed outage, limiter
event, and data gap traced back to Yahoo being the only source. This module
adds the alternates, each behind the same rules as everything else: every
value is Sourced with the provider named, failures surface as unavailable,
budgets are enforced in code, and nothing here runs on a render path.

Verified entitlements (probed live 2026-08-19, free tiers):
- Alpaca: clock, trading calendar (with half-days), corporate-action
  announcements, IEX latest trades, daily bars with explicit adjustment.
- Finnhub: real-time quote, analyst recommendation trends, earnings calendar.
- FMP (/stable/ only -- the legacy v3/v4 API is 403 for post-2025 accounts):
  biggest-losers screener, earnings calendar, delisted companies, per-firm
  analyst grades, splits, quote.

Budgets: Alpaca 200/min and Finnhub 60/min are far above this app's trickle;
FMP is 250/DAY and gets a hard in-code counter that refuses at 200 so an
accident can never exhaust the account.
"""

import logging
import os
import time
from datetime import date, datetime, timedelta
from typing import List, Optional

import requests

import market_data
from provenance import Sourced
from secrets_store import get as get_secret

logger = logging.getLogger(__name__)

# The ONLY Alpaca trading endpoint this app will ever talk to. Paper money by
# construction; _alpaca_trading_base() refuses anything else outright.
ALPACA_PAPER_BASE = "https://paper-api.alpaca.markets"
ALPACA_DATA_BASE = "https://data.alpaca.markets"
FINNHUB_BASE = "https://finnhub.io/api/v1"
FMP_BASE = "https://financialmodelingprep.com/stable"

FMP_DAILY_BUDGET = 200          # hard stop below the 250/day plan limit


def _alpaca_keys():
    return get_secret("ALPACA_API_KEY"), get_secret("ALPACA_API_SECRET")


def _alpaca_headers():
    key, secret = _alpaca_keys()
    if not key or not secret:
        return None
    return {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}


def _alpaca_trading_base() -> str:
    """The trading endpoint, guarded: never anything but paper.

    Real money must be impossible by construction, not by configuration
    discipline. A live URL in the environment is treated as a
    misconfiguration and refused.
    """
    configured = os.environ.get("ALPACA_PAPER_BASE", ALPACA_PAPER_BASE)
    if configured.rstrip("/") != ALPACA_PAPER_BASE:
        raise RuntimeError(
            f"refusing non-paper Alpaca endpoint {configured!r}; this app "
            "only ever trades simulated money")
    return ALPACA_PAPER_BASE


def _fmp_budget_ok() -> bool:
    """In-code daily budget, atomic where it can be.

    Shared Redis gets a true INCR (both workers count against one budget);
    without Redis each process keeps its own counter, so the cap is halved
    per worker -- two isolated counters must still sum under the plan limit
    (CR, PR 55).
    """
    day_key = f"fmpbudget:{date.today().isoformat()}"
    redis_client = market_data._cache._redis
    if redis_client is not None:
        try:
            used = redis_client.incr(f"md:{market_data.CACHE_SCHEMA_VERSION}:{day_key}")
            if used == 1:
                redis_client.expire(f"md:{market_data.CACHE_SCHEMA_VERSION}:{day_key}",
                                    24 * 60 * 60)
            return used <= FMP_DAILY_BUDGET
        except Exception:
            pass
    used = market_data._cache.get(day_key) or 0
    if used >= FMP_DAILY_BUDGET // 2:
        return False
    market_data._cache.set(day_key, used + 1, 24 * 60 * 60)
    return True


def _fmp_get(path: str, params: dict):
    api_key = get_secret("FMP_API_KEY")
    if not api_key:
        return None, "FMP_API_KEY not configured"
    if not _fmp_budget_ok():
        return None, f"FMP daily budget ({FMP_DAILY_BUDGET}) exhausted"
    market_data._throttle()
    response = requests.get(f"{FMP_BASE}/{path}",
                            params={**params, "apikey": api_key}, timeout=20)
    response.raise_for_status()
    return response.json(), None


def _finnhub_get(path: str, params: dict):
    token = get_secret("FINNHUB_API_KEY")
    if not token:
        return None, "FINNHUB_API_KEY not configured"
    market_data._throttle()
    response = requests.get(f"{FINNHUB_BASE}/{path}",
                            params={**params, "token": token}, timeout=20)
    response.raise_for_status()
    return response.json(), None


def _alpaca_get(base: str, path: str, params: Optional[dict] = None):
    headers = _alpaca_headers()
    if headers is None:
        return None, "Alpaca keys not configured"
    market_data._throttle()
    response = requests.get(f"{base}{path}", params=params or {},
                            headers=headers, timeout=20)
    response.raise_for_status()
    return response.json(), None


# --- Trading calendar --------------------------------------------------------

def trading_calendar(start: date, end: date) -> Sourced:
    """Real exchange sessions from Alpaca, half-days included, cached daily."""
    source = "alpaca:calendar"
    key = f"src:calendar:{start.isoformat()}:{end.isoformat()}"

    def produce():
        payload, err = _alpaca_get(_alpaca_trading_base(), "/v2/calendar",
                                   {"start": start.isoformat(), "end": end.isoformat()})
        if err:
            return {"ok": False, "reason": err}
        return {"ok": True, "days": [d["date"] for d in payload],
                "sessions": {d["date"]: {"open": d["open"], "close": d["close"]}
                             for d in payload}}

    payload = market_data._cached(key, 24 * 60 * 60, produce)
    if not payload.get("ok"):
        return Sourced.unavailable(source, payload.get("reason", "unavailable"))
    return Sourced.live({k: v for k, v in payload.items() if k != "ok"}, source)


def trading_days_set(lookahead_days: int = 400,
                     cache_only: bool = False) -> Optional[set]:
    """Set of upcoming trading-day ISO dates, or None when unavailable.

    cache_only serves hot paths (market_phase consults this on every call):
    it reads what warm_calendar() stored and NEVER fetches -- a render or a
    test must not block on a provider.
    """
    if cache_only:
        cached = market_data._cache.get("src:trading-days")
        return set(cached["days"]) if cached and cached.get("days") else None
    today = date.today()
    calendar = trading_calendar(today - timedelta(days=7),
                                today + timedelta(days=lookahead_days))
    if not calendar.ok:
        return None
    days = calendar.value["days"]
    market_data._cache.set("src:trading-days", {"days": days}, 24 * 60 * 60)
    return set(days)


def add_trading_days(start: date, bars: int) -> date:
    """Walk N real trading days forward; weekend-walk fallback without data."""
    days = trading_days_set()
    current, steps = start, 0
    while steps < bars:
        current = current + timedelta(days=1)
        if days is not None:
            if current.isoformat() in days:
                steps += 1
        elif current.weekday() < 5:
            steps += 1
        if (current - start).days > bars * 4 + 10:
            break  # pathological calendar; refuse to loop forever
    return current


# --- Corporate actions -------------------------------------------------------

def splits_for(symbol: str, since_days: int = 400) -> Sourced:
    """Split events: Alpaca announcements first, FMP as the fallback.

    The lookback rides in the cache key: a 400-day answer must not serve a
    caller who asked about an older interval (CR, PR 55).
    """
    symbol = symbol.upper()
    key = f"src:splits:{symbol}:{since_days}"

    def produce():
        since = (date.today() - timedelta(days=since_days)).isoformat()
        try:
            payload, err = _alpaca_get(
                _alpaca_trading_base(), "/v2/corporate_actions/announcements",
                {"ca_types": "split", "since": since,
                 "until": date.today().isoformat(), "symbol": symbol})
            if not err:
                events = [{"date": a.get("ex_date") or a.get("effective_date"),
                           "ratio": (float(a["new_rate"]) / float(a["old_rate"]))
                           if a.get("new_rate") and a.get("old_rate") else None,
                           "provider": "alpaca"}
                          for a in payload
                          if (a.get("initiating_symbol") or a.get("target_symbol")) == symbol]
                return {"ok": True, "events": [e for e in events if e["date"]]}
        except Exception as e:
            logger.info(f"alpaca splits unavailable for {symbol}: {type(e).__name__}")
        try:
            payload, err = _fmp_get("splits", {"symbol": symbol})
            if err:
                return {"ok": False, "reason": err}
            events = [{"date": s.get("date"),
                       "ratio": (float(s["numerator"]) / float(s["denominator"]))
                       if s.get("numerator") and s.get("denominator") else None,
                       "provider": "fmp"}
                      for s in payload if s.get("date") and s["date"] >= since]
            return {"ok": True, "events": events}
        except Exception as e:
            return {"ok": False, "reason": f"both split providers failed ({type(e).__name__})"}

    payload = market_data._cached(key, 24 * 60 * 60, produce)
    if not payload.get("ok"):
        return Sourced.unavailable("alpaca+fmp:splits", payload.get("reason", "unavailable"))
    return Sourced.live(payload["events"], "alpaca+fmp:splits")


# --- Prices ------------------------------------------------------------------

def latest_trades(symbols: List[str]) -> Sourced:
    """Real-time last trades for many symbols in ONE Alpaca IEX request."""
    source = "alpaca:iex-latest-trade"
    if not symbols:
        return Sourced.unavailable(source, "no symbols")
    try:
        payload, err = _alpaca_get(ALPACA_DATA_BASE, "/v2/stocks/trades/latest",
                                   {"symbols": ",".join(s.upper() for s in symbols),
                                    "feed": "iex"})
        if err:
            return Sourced.unavailable(source, err)
        trades = {sym: {"price": t["p"], "at": t["t"]}
                  for sym, t in (payload.get("trades") or {}).items()}
        if not trades:
            return Sourced.unavailable(source, "no trades returned")
        return Sourced.live(trades, source)
    except Exception as e:
        return Sourced.unavailable(source, f"alpaca latest trades failed ({type(e).__name__})")


def quote_failover(symbol: str) -> Sourced:
    """One symbol's latest price: Alpaca, then Finnhub, then FMP."""
    trades = latest_trades([symbol])
    if trades.ok and symbol.upper() in trades.value:
        t = trades.value[symbol.upper()]
        return Sourced.live({"price": t["price"], "at": t["at"]}, trades.source)
    try:
        payload, err = _finnhub_get("quote", {"symbol": symbol.upper()})
        if not err and payload.get("c"):
            return Sourced.live({"price": payload["c"], "at": payload.get("t")},
                                "finnhub:quote")
    except Exception as e:
        logger.info(f"finnhub quote failed for {symbol}: {type(e).__name__}")
    try:
        payload, err = _fmp_get("quote", {"symbol": symbol.upper()})
        if not err and payload and payload[0].get("price"):
            return Sourced.live({"price": payload[0]["price"], "at": None}, "fmp:quote")
    except Exception as e:
        logger.info(f"fmp quote failed for {symbol}: {type(e).__name__}")
    return Sourced.unavailable("alpaca+finnhub+fmp:quote", "every price provider failed")


# --- Losers screener failover ------------------------------------------------

def fmp_losers() -> Sourced:
    """The day's biggest losers from FMP, shaped like the Yahoo screener rows.

    The one capability that previously had no backup at all: when Yahoo's
    screener fails, the board can still exist.
    """
    source = "fmp:biggest-losers"

    def produce():
        try:
            payload, err = _fmp_get("biggest-losers", {})
            if err:
                return {"ok": False, "reason": err}
            rows = []
            for r in payload:
                pct = r.get("changesPercentage")
                if not r.get("symbol") or not isinstance(pct, (int, float)):
                    continue  # one malformed row must not sink the last-resort source
                rows.append({"Symbol": r.get("symbol"),
                             "Name": r.get("name"),
                             "Change": str(r.get("change")),
                             "Percent Change": f"{pct:.2f}%",
                             "Volume": "n/a", "Market Cap": "n/a"})
            if not rows:
                return {"ok": False, "reason": "empty losers list"}
            return {"ok": True, "rows": rows[:25]}
        except Exception as e:
            return {"ok": False, "reason": f"fmp losers failed ({type(e).__name__})"}

    payload = market_data._cached("src:fmp-losers", 10 * 60, produce)
    if not payload.get("ok"):
        return Sourced.unavailable(source, payload.get("reason", "unavailable"))
    return Sourced.live(payload["rows"], source)


# --- Analyst revisions -------------------------------------------------------

GRADES_WINDOW_DAYS = 30


def analyst_grades(symbol: str, days: int = GRADES_WINDOW_DAYS) -> Sourced:
    """Per-firm upgrade/downgrade events since the window opened.

    THE post-drop signal: whether analysts cut or defended after the fall.
    FMP's per-event feed first; Finnhub's monthly recommendation trend as
    the coarser fallback.
    """
    symbol = symbol.upper()
    # The window rides in the key: a 30-day answer must not serve a caller
    # who asked about a different span (CR, PR 55).
    key = f"src:grades:{symbol}:{days}"

    def produce():
        cutoff = (date.today() - timedelta(days=days)).isoformat()
        try:
            payload, err = _fmp_get("grades", {"symbol": symbol})
            if not err and isinstance(payload, list):
                events = [{"date": g.get("date"), "firm": g.get("gradingCompany"),
                           "action": g.get("action"),
                           "from": g.get("previousGrade"), "to": g.get("newGrade")}
                          for g in payload
                          if g.get("date") and g["date"] >= cutoff]
                upgrades = sum(1 for e in events if (e["action"] or "").lower() == "upgrade")
                downgrades = sum(1 for e in events
                                 if (e["action"] or "").lower() == "downgrade")
                return {"ok": True, "provider": "fmp", "events": events[:10],
                        "upgrades": upgrades, "downgrades": downgrades,
                        "window_days": days}
        except Exception as e:
            logger.info(f"fmp grades unavailable for {symbol}: {type(e).__name__}")
        try:
            payload, err = _finnhub_get("stock/recommendation", {"symbol": symbol})
            if err:
                return {"ok": False, "reason": err}
            if not payload:
                return {"ok": False, "reason": "no recommendation history"}
            latest, prior = payload[0], (payload[1] if len(payload) > 1 else None)
            trend = None
            if prior:
                now_bull = latest.get("strongBuy", 0) + latest.get("buy", 0)
                was_bull = prior.get("strongBuy", 0) + prior.get("buy", 0)
                trend = now_bull - was_bull
            up = max(0, trend) if trend is not None else 0
            down = max(0, -trend) if trend is not None else 0
            return {"ok": True, "provider": "finnhub", "events": [],
                    "upgrades": up, "downgrades": down,
                    "monthly_trend": trend, "latest_period": latest.get("period"),
                    "window_days": days}
        except Exception as e:
            return {"ok": False, "reason": f"both grade providers failed ({type(e).__name__})"}

    payload = market_data._cached(key, 24 * 60 * 60, produce)
    if not payload.get("ok"):
        return Sourced.unavailable("fmp+finnhub:grades", payload.get("reason", "unavailable"))
    return Sourced.live({k: v for k, v in payload.items() if k != "ok"},
                        f"{payload.get('provider', 'fmp')}:grades")


# --- Earnings ----------------------------------------------------------------

def earnings_confirmed(symbol: str) -> Sourced:
    """Confirmed upcoming earnings date: FMP first, Finnhub fallback."""
    symbol = symbol.upper()
    key = f"src:earnings:{symbol}"

    def produce():
        span_from = date.today().isoformat()
        span_to = (date.today() + timedelta(days=90)).isoformat()
        try:
            payload, err = _fmp_get("earnings-calendar",
                                    {"from": span_from, "to": span_to})
            if not err and isinstance(payload, list):
                mine = sorted(r["date"] for r in payload
                              if r.get("symbol") == symbol and r.get("date"))
                if mine:
                    return {"ok": True, "date": mine[0], "provider": "fmp"}
        except Exception as e:
            logger.info(f"fmp earnings unavailable: {type(e).__name__}")
        try:
            payload, err = _finnhub_get("calendar/earnings",
                                        {"from": span_from, "to": span_to,
                                         "symbol": symbol})
            if err:
                return {"ok": False, "reason": err}
            rows = (payload.get("earningsCalendar") or [])
            mine = sorted(r["date"] for r in rows if r.get("date"))
            if mine:
                return {"ok": True, "date": mine[0], "provider": "finnhub"}
            return {"ok": False, "reason": "no confirmed earnings in the next 90 days"}
        except Exception as e:
            return {"ok": False, "reason": f"both earnings providers failed ({type(e).__name__})"}

    payload = market_data._cached(key, 24 * 60 * 60, produce)
    if not payload.get("ok"):
        return Sourced.unavailable("fmp+finnhub:earnings", payload.get("reason", "unavailable"))
    return Sourced.live({"date": payload["date"]}, f"{payload['provider']}:earnings-calendar")


# --- Survivorship ------------------------------------------------------------

def delisted_recent(days: int = 365) -> Sourced:
    """Recently delisted US listings, for the survivorship disclosure."""
    source = "fmp:delisted-companies"

    def produce():
        cutoff = (date.today() - timedelta(days=days)).isoformat()
        names = []
        try:
            for page in range(3):
                payload, err = _fmp_get("delisted-companies", {"page": page})
                if err:
                    return {"ok": False, "reason": err}
                if not payload:
                    break
                for row in payload:
                    if (row.get("delistedDate") or "") >= cutoff:
                        names.append({"symbol": row.get("symbol"),
                                      "date": row.get("delistedDate")})
                if payload and (payload[-1].get("delistedDate") or "") < cutoff:
                    break
            return {"ok": True, "count": len(names), "recent": names[:200],
                    "window_days": days}
        except Exception as e:
            return {"ok": False, "reason": f"fmp delisted failed ({type(e).__name__})"}

    payload = market_data._cached("src:delisted", 7 * 24 * 60 * 60, produce)
    if not payload.get("ok"):
        return Sourced.unavailable(source, payload.get("reason", "unavailable"))
    return Sourced.live({k: v for k, v in payload.items() if k != "ok"}, source)


# --- Paper execution ---------------------------------------------------------

PAPER_NOTIONAL_PER_PICK = 1000.0
PAPER_MAX_PICKS = 3


def paper_execute_picks(symbols: List[str]) -> Sourced:
    """Submit market-on-open PAPER orders for the day's top picks.

    Simulated money only: the endpoint is pinned to paper-api and anything
    else raises before a request is made. Orders queue for the next open
    (time_in_force=opg), which matches how the snapshot's entry would really
    have been traded. Fills land in later snapshots as the measured-slippage
    track record.
    """
    source = "alpaca:paper-orders"
    base = _alpaca_trading_base()   # raises on any non-paper endpoint
    headers = _alpaca_headers()
    if headers is None:
        return Sourced.unavailable(source, "Alpaca keys not configured")
    submitted, failed = [], []
    for symbol in [s.upper() for s in symbols][:PAPER_MAX_PICKS]:
        client_order_id = f"snap-{date.today().isoformat()}-{symbol}"
        try:
            market_data._throttle()
            response = requests.post(
                f"{base}/v2/orders", headers=headers, timeout=20,
                json={"symbol": symbol, "notional": PAPER_NOTIONAL_PER_PICK,
                      "side": "buy", "type": "market", "time_in_force": "opg",
                      # Deterministic per (day, symbol): a snapshot retry
                      # cannot double-submit -- Alpaca rejects the duplicate
                      # id, which we treat as already-submitted.
                      "client_order_id": client_order_id})
            if response.status_code == 422 and "client_order_id" in response.text:
                submitted.append({"symbol": symbol, "order_id": client_order_id,
                                  "status": "already-submitted"})
                continue
            response.raise_for_status()
            order = response.json()
            submitted.append({"symbol": symbol, "order_id": order.get("id"),
                              "status": order.get("status")})
        except Exception as e:
            failed.append({"symbol": symbol, "reason": type(e).__name__})
            logger.warning(f"paper order failed for {symbol}: {type(e).__name__}")
    if not submitted and failed:
        return Sourced.unavailable(source, f"all paper orders failed: {failed}")
    return Sourced.live({"submitted": submitted, "failed": failed,
                         "notional_each": PAPER_NOTIONAL_PER_PICK,
                         "basis": "paper account, market-on-open, simulated money"},
                        source)


def paper_recent_fills(days: int = 7) -> Sourced:
    """Recent paper fills, for the slippage record."""
    source = "alpaca:paper-fills"
    try:
        after = (datetime.utcnow() - timedelta(days=days)).isoformat() + "Z"
        payload, err = _alpaca_get(_alpaca_trading_base(), "/v2/orders",
                                   {"status": "closed", "after": after,
                                    "limit": 100})
        if err:
            return Sourced.unavailable(source, err)
        fills = [{"symbol": o["symbol"], "filled_at": o.get("filled_at"),
                  "filled_avg_price": o.get("filled_avg_price"),
                  "filled_qty": o.get("filled_qty")}
                 for o in payload if o.get("filled_at")]
        return Sourced.live(fills, source)
    except Exception as e:
        return Sourced.unavailable(source, f"paper fills failed ({type(e).__name__})")
