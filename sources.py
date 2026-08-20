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
import math
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


def _eastern_today() -> date:
    """The trading-day date. Render's clock is UTC: at the 8 PM ET submission
    window that is already tomorrow, which would mislabel order ids."""
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo("America/New_York")).date()


def paper_execute_picks(picks: List[dict]) -> Sourced:
    """Submit market-on-open PAPER orders for the day's top picks.

    Simulated money only: the endpoint is pinned to paper-api and anything
    else raises before a request is made. Orders queue for the next open
    (time_in_force=opg), which matches how the snapshot's entry would really
    have been traded. Fills land in later snapshots as the measured-slippage
    track record.

    Picks are {"symbol", "price"} dicts: Alpaca requires whole shares for
    opg (notional means fractional, and "fractional orders must be DAY
    orders" -- live rejection 2026-08-19), so each order is sized to the
    nearest whole-share count under the target notional, minimum one share.
    """
    source = "alpaca:paper-orders"
    base = _alpaca_trading_base()   # raises on any non-paper endpoint
    headers = _alpaca_headers()
    if headers is None:
        return Sourced.unavailable(source, "Alpaca keys not configured")
    submitted, failed = [], []
    for pick in picks:
        # Validate BEFORE consuming a slot: an unpriceable high-ranked pick
        # must not crowd out a valid lower-ranked one (CR, PR 67).
        if len(submitted) >= PAPER_MAX_PICKS:
            break
        symbol = str(pick.get("symbol", "")).upper()
        price = pick.get("price")
        if not symbol:
            continue
        if not isinstance(price, (int, float)) or not math.isfinite(price) or price <= 0:
            failed.append({"symbol": symbol,
                           "reason": "no price to size the order"})
            continue
        qty = max(1, int(PAPER_NOTIONAL_PER_PICK // price))
        client_order_id = f"snap-{_eastern_today().isoformat()}-{symbol}"
        try:
            market_data._throttle()
            response = requests.post(
                f"{base}/v2/orders", headers=headers, timeout=20,
                json={"symbol": symbol, "qty": str(qty),
                      "side": "buy", "type": "market", "time_in_force": "opg",
                      # Deterministic per (day, symbol): a snapshot retry
                      # cannot double-submit -- Alpaca rejects the duplicate
                      # id, which we treat as already-submitted.
                      "client_order_id": client_order_id})
            if response.status_code == 422 and "client_order_id" in response.text:
                submitted.append({"symbol": symbol, "order_id": client_order_id,
                                  "status": "already-submitted", "qty": qty,
                                  "ref_price": price})
                continue
            response.raise_for_status()
            order = response.json()
            submitted.append({"symbol": symbol, "order_id": order.get("id"),
                              "status": order.get("status"), "qty": qty,
                              "ref_price": price})
        except Exception as e:
            # Keep the provider's words: "HTTPError" alone cost a debugging
            # round trip when every order bounced off the opg window rule.
            body = getattr(getattr(e, "response", None), "text", "") or str(e)
            failed.append({"symbol": symbol,
                           "reason": f"{type(e).__name__}: {body[:160]}"})
            logger.warning(f"paper order failed for {symbol}: {type(e).__name__}: {body[:160]}")
    if not submitted and failed:
        return Sourced.unavailable(source, f"all paper orders failed: {failed}")
    return Sourced.live({"submitted": submitted, "failed": failed,
                         "target_notional_each": PAPER_NOTIONAL_PER_PICK,
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


# --- Factor backups ----------------------------------------------------------

def ratings_spread(symbol: str) -> Sourced:
    """Analyst buy/hold/sell spread from Finnhub's recommendation trends.

    The same shape Yahoo's spread factor consumes, so the score's ratings
    input survives a quoteSummary outage (live incident 2026-08-19: the
    factor went missing board-wide with no backup).
    """
    source = "finnhub:recommendation-trends"
    key = f"src:ratings:{symbol.upper()}"

    def produce():
        try:
            payload, err = _finnhub_get("stock/recommendation",
                                        {"symbol": symbol.upper()})
            if err:
                return {"ok": False, "reason": err}
            if not payload:
                return {"ok": False, "reason": "no ratings published"}
            latest = payload[0]
            spread = {k: int(latest.get(k, 0) or 0) for k in
                      ("strongBuy", "buy", "hold", "sell", "strongSell")}
            total = sum(spread.values())
            if total == 0:
                return {"ok": False, "reason": "no ratings published"}
            spread["total"] = total
            return {"ok": True, "spread": spread, "period": latest.get("period")}
        except Exception as e:
            return {"ok": False, "reason": f"finnhub ratings failed ({type(e).__name__})"}

    payload = market_data._cached(key, 24 * 60 * 60, produce)
    if not payload.get("ok"):
        return Sourced.unavailable(source, payload.get("reason", "unavailable"))
    return Sourced.live(payload["spread"], source)


def options_putcall(symbol: str) -> Sourced:
    """Put/call volume positioning from Alpaca's indicative options feed.

    One chain-snapshot request; contract symbols encode call/put, and the
    daily bars carry per-contract volume. This keeps the options factor
    alive while Yahoo's chain endpoint is limited -- the factor that went
    dark board-wide in the 2026-08-19 incident.
    """
    source = "alpaca:options-indicative"
    key = f"src:putcall:{symbol.upper()}"

    def produce():
        import re as _re
        until = (date.today() + timedelta(days=45)).isoformat()
        # The endpoint paginates (max 1000/page). Contracts sort C-before-P
        # within each expiry, so a truncated chain would overweight calls --
        # merge every page or refuse.
        snapshots = {}
        token = None
        for _page in range(5):
            params = {"feed": "indicative", "limit": 1000,
                      "expiration_date_lte": until}
            if token:
                params["page_token"] = token
            try:
                payload, err = _alpaca_get(
                    ALPACA_DATA_BASE,
                    f"/v1beta1/options/snapshots/{symbol.upper()}", params)
                if err:
                    return {"ok": False, "reason": err}
            except Exception as e:
                return {"ok": False, "reason": f"alpaca options failed ({type(e).__name__})"}
            snapshots.update(payload.get("snapshots") or {})
            token = payload.get("next_page_token")
            if not token:
                break
        else:
            return {"ok": False, "reason": "options chain exceeds page budget"}
        if not snapshots:
            return {"ok": False, "reason": "no listed options"}
        # OCC symbology is fixed from the right (8-digit strike, C/P,
        # 6-digit date); anchor there so roots with digits still parse.
        pattern = _re.compile(r"\d{6}([CP])\d{8}$")
        call_volume = put_volume = 0
        for contract, snap in snapshots.items():
            match = pattern.search(contract)
            if not match:
                continue
            volume = int((snap.get("dailyBar") or {}).get("v") or 0)
            if match.group(1) == "C":
                call_volume += volume
            else:
                put_volume += volume
        if call_volume + put_volume == 0:
            return {"ok": False, "reason": "no option volume today"}
        return {"ok": True, "call_volume": call_volume, "put_volume": put_volume,
                "put_call_ratio": (round(put_volume / call_volume, 3)
                                   if call_volume else None),
                "contracts": len(snapshots), "window": f"expiries to {until}"}

    payload = market_data._cached(key, market_data.TTL_OPTIONS, produce)
    if not payload.get("ok"):
        return Sourced.unavailable(source, payload.get("reason", "unavailable"))
    return Sourced.live({k: v for k, v in payload.items() if k != "ok"}, source)


def price_targets(symbol: str, allow_fetch: bool = True) -> Sourced:
    """Analyst target mean and count from FMP's price-target summary.

    Backup for the score's consensus-upside factor when Yahoo's quoteSummary
    is blocked (verified free on the /stable/ endpoints 2026-08-19). One
    request per symbol per day, inside the FMP daily budget.
    """
    source = "fmp:price-target-summary"
    key = f"src:targets:{symbol.upper()}"

    def produce():
        # One provider request per symbol per day, atomically claimed (CR,
        # PRs 62 and 66): the response cache alone cannot hold that contract.
        # The day's answer is kept beside the claim and replayed if the
        # response cache expires early, so the factor never goes missing
        # just because the request budget is already spent.
        day = date.today().isoformat()
        answer_key = f"src:targets:answer:{symbol.upper()}:{day}"
        if not market_data._cache.claim_once(
                f"src:targets:{symbol.upper()}:{day}", 24 * 60 * 60):
            # The claim loser is racing an in-flight winner: wait briefly for
            # the day's answer, and refuse transiently (never day-scoped) if
            # it has not landed yet (CR, PR 66 follow-up).
            for _ in range(6):
                replay = market_data._cache.get(answer_key)
                if replay is not None:
                    return replay
                time.sleep(0.5)
            return {"ok": False,
                    "reason": "fmp target request in flight"}
        def _remember(result):
            market_data._cache.set(answer_key, result, 24 * 60 * 60)
            return result
        try:
            payload, err = _fmp_get("price-target-summary",
                                    {"symbol": symbol.upper()})
            if err:
                return _remember({"ok": False, "reason": err})
            if not payload:
                return _remember({"ok": False, "reason": "no analyst coverage published"})
            row = payload[0]
            # Prefer the fresher quarter window; a quiet quarter falls back
            # to the trailing year.
            for count_key, mean_key, window in (
                    ("lastQuarterCount", "lastQuarterAvgPriceTarget", "3mo"),
                    ("lastYearCount", "lastYearAvgPriceTarget", "12mo")):
                count = int(row.get(count_key) or 0)
                mean = row.get(mean_key)
                if count > 0 and mean:
                    return _remember({"ok": True, "mean": float(mean),
                                      "count": count, "window": window})
            return _remember({"ok": False, "reason": "no analyst coverage published"})
        except Exception as e:
            return _remember({"ok": False, "reason": f"fmp targets failed ({type(e).__name__})"})

    payload = market_data._cached(key, 24 * 60 * 60, produce, allow_fetch)
    if not payload.get("ok"):
        return Sourced.unavailable(source, payload.get("reason", "unavailable"))
    return Sourced.live({"mean": payload["mean"], "count": payload["count"],
                         "window": payload["window"]}, source)


def _finra_latest_settlement() -> Optional[str]:
    """Newest consolidated-short-interest settlement date. One request per
    day, shared by every symbol -- the dataset is partitioned by date and
    only sortable within a named partition."""
    key = "src:finra:si-settlement"

    def produce():
        try:
            resp = requests.get(
                "https://api.finra.org/partitions/group/otcMarket"
                "/name/consolidatedShortInterest",
                headers={"Accept": "application/json"}, timeout=20)
            resp.raise_for_status()
            parts = [p["partitions"][0]
                     for p in (resp.json().get("availablePartitions") or [])
                     if p.get("partitions")]
            if not parts:
                return {"ok": False, "reason": "no partitions listed"}
            return {"ok": True, "settlement": max(parts)}
        except Exception as e:
            return {"ok": False, "reason": f"finra partitions failed ({type(e).__name__})"}

    payload = market_data._cached(key, 24 * 60 * 60, produce)
    return payload.get("settlement") if payload.get("ok") else None


def shares_float(symbol: str, allow_fetch: bool = True) -> Sourced:
    """Free-float share count from FMP (free tier, verified 2026-08-19).

    Float moves slowly: cached a week, and a day stamp caps HTTP at one
    request per symbol per day whatever the response cache does."""
    source = "fmp:shares-float"
    key = f"src:float:{symbol.upper()}"

    def produce():
        day = date.today().isoformat()
        answer_key = f"src:float:answer:{symbol.upper()}:{day}"
        if not market_data._cache.claim_once(
                f"src:float:{symbol.upper()}:{day}", 24 * 60 * 60):
            # The claim loser is racing an in-flight winner: wait briefly for
            # the day's answer, and refuse transiently (never day-scoped) if
            # it has not landed yet (CR, PR 66 follow-up).
            for _ in range(6):
                replay = market_data._cache.get(answer_key)
                if replay is not None:
                    return replay
                time.sleep(0.5)
            return {"ok": False, "reason": "fmp float request in flight"}
        def _remember(result):
            market_data._cache.set(answer_key, result, 24 * 60 * 60)
            return result
        try:
            payload, err = _fmp_get("shares-float", {"symbol": symbol.upper()})
            if err:
                return _remember({"ok": False, "reason": err})
            if not payload or not payload[0].get("floatShares"):
                return _remember({"ok": False, "reason": "float not reported"})
            shares = float(payload[0]["floatShares"])
            # json.loads accepts NaN/Infinity literals, and NaN <= 0 is
            # False -- an explicit finiteness check or garbage caches as truth.
            if not math.isfinite(shares) or shares <= 0:
                return _remember({"ok": False, "reason": "float not reported"})
            return _remember({"ok": True, "floatShares": shares,
                              "as_of": (payload[0].get("date") or "")[:10]})
        except Exception as e:
            return _remember({"ok": False, "reason": f"fmp float failed ({type(e).__name__})"})

    payload = market_data._cached(key, 7 * 24 * 60 * 60, produce, allow_fetch)
    if not payload.get("ok"):
        return Sourced.unavailable(source, payload.get("reason", "unavailable"))
    return Sourced.live({k: v for k, v in payload.items() if k != "ok"}, source)


def short_percent_float(symbol: str, allow_fetch: bool = True) -> Sourced:
    """Short interest as a fraction of float: FINRA's consolidated short
    interest (the same twice-monthly settlement data Yahoo repackages) over
    FMP's float. Backup for the score's short-interest factor -- the last
    input that was Yahoo-only. Labeled with its settlement date."""
    source = "finra:consolidated-short-interest"
    key = f"src:shortfloat:{symbol.upper()}"

    def produce():
        day = date.today().isoformat()
        answer_key = f"src:shortfloat:answer:{symbol.upper()}:{day}"
        if not market_data._cache.claim_once(
                f"src:shortfloat:{symbol.upper()}:{day}", 24 * 60 * 60):
            # The claim loser is racing an in-flight winner: wait briefly for
            # the day's answer, and refuse transiently (never day-scoped) if
            # it has not landed yet (CR, PR 66 follow-up).
            for _ in range(6):
                replay = market_data._cache.get(answer_key)
                if replay is not None:
                    return replay
                time.sleep(0.5)
            return {"ok": False, "reason": "short-interest request in flight"}
        def _remember(result):
            market_data._cache.set(answer_key, result, 24 * 60 * 60)
            return result
        settlement = _finra_latest_settlement()
        if not settlement:
            return _remember({"ok": False, "reason": "finra settlement calendar unavailable"})
        try:
            resp = requests.post(
                "https://api.finra.org/data/group/otcMarket"
                "/name/consolidatedShortInterest",
                headers={"Accept": "application/json",
                         "Content-Type": "application/json"},
                json={"limit": 1, "compareFilters": [
                    {"compareType": "EQUAL", "fieldName": "symbolCode",
                     "fieldValue": symbol.upper()},
                    {"compareType": "EQUAL", "fieldName": "settlementDate",
                     "fieldValue": settlement}]},
                timeout=20)
            resp.raise_for_status()
            rows = resp.json()
        except Exception as e:
            return _remember({"ok": False, "reason": f"finra short interest failed ({type(e).__name__})"})
        if not rows or not int(rows[0].get("currentShortPositionQuantity") or 0):
            return _remember({"ok": False, "reason": "no short interest reported"})
        shares_short = int(rows[0]["currentShortPositionQuantity"])
        flt = shares_float(symbol, allow_fetch=True)
        if not flt.ok:
            return _remember({"ok": False, "reason": f"float unavailable ({flt.reason})"})
        pct = shares_short / flt.value["floatShares"]
        # 150% of float is the valid ceiling (GME 2021 hit ~140%); above it
        # the composition is presumed broken, not the market exotic.
        if not 0 < pct <= 1.5:
            return _remember({"ok": False,
                              "reason": f"implausible short/float ratio {pct:.2f}"})
        return _remember({"ok": True, "pct": round(pct, 4),
                          "shares_short": shares_short, "as_of": settlement})

    payload = market_data._cached(key, 3 * 24 * 60 * 60, produce, allow_fetch)
    if not payload.get("ok"):
        return Sourced.unavailable(source, payload.get("reason", "unavailable"))
    return Sourced.derived(payload["pct"],
                           f"{source} (settlement {payload['as_of']}) / fmp:shares-float")


def company_news(symbol: str, limit: int = 5) -> Sourced:
    """Recent headlines from Finnhub's company-news feed (free tier,
    verified live 2026-08-20). Same item shape the page renders, so the
    news chip survives a Yahoo outage."""
    source = "finnhub:company-news"
    key = f"src:news:{symbol.upper()}:{limit}"

    def produce():
        try:
            frm = (date.today() - timedelta(days=7)).isoformat()
            payload, err = _finnhub_get("company-news",
                                        {"symbol": symbol.upper(),
                                         "from": frm, "to": date.today().isoformat()})
            if err:
                return {"ok": False, "reason": err}
        except Exception as e:
            return {"ok": False, "reason": f"finnhub news failed ({type(e).__name__})"}
        out = []
        for item in payload or []:
            title = item.get("headline")
            if not title:
                continue
            published = item.get("datetime")
            out.append({
                "title": title,
                "publisher": item.get("source"),
                "published": (datetime.utcfromtimestamp(published).isoformat() + "Z"
                              if isinstance(published, (int, float)) and published > 0
                              else None),
                "url": item.get("url"),
            })
            if len(out) >= limit:
                break
        if not out:
            return {"ok": False, "reason": "no recent headlines"}
        return {"ok": True, "items": out}

    payload = market_data._cached(key, market_data.TTL_NEWS, produce)
    if not payload.get("ok"):
        return Sourced.unavailable(source, payload.get("reason", "unavailable"))
    return Sourced.live(payload["items"], source)


def implied_straddle_move(symbol: str, spot: float) -> Sourced:
    """ATM straddle implied move from Alpaca's indicative options feed.

    Mirrors the Yahoo computation: nearest expiry at least 5 days out, one
    strike listed on both sides nearest spot, legs priced off live
    bid/ask mids with dailyBar close as the honest last-trade fallback.
    Keeps the drill-in's implied-move figure alive while Yahoo's chain is
    limited."""
    source = "alpaca:options-indicative-straddle"

    def produce():
        import re as _re
        until = (date.today() + timedelta(days=45)).isoformat()
        snapshots = {}
        token = None
        for _page in range(5):
            params = {"feed": "indicative", "limit": 1000,
                      "expiration_date_lte": until}
            if token:
                params["page_token"] = token
            try:
                payload, err = _alpaca_get(
                    ALPACA_DATA_BASE,
                    f"/v1beta1/options/snapshots/{symbol.upper()}", params)
                if err:
                    return {"ok": False, "reason": err}
            except Exception as e:
                return {"ok": False, "reason": f"alpaca options failed ({type(e).__name__})"}
            snapshots.update(payload.get("snapshots") or {})
            token = payload.get("next_page_token")
            if not token:
                break
        else:
            return {"ok": False, "reason": "options chain exceeds page budget"}
        if not snapshots:
            return {"ok": False, "reason": "no listed options"}

        pattern = _re.compile(r"(\d{6})([CP])(\d{8})$")
        by_expiry = {}
        for contract, snap in snapshots.items():
            m = pattern.search(contract)
            if not m:
                continue
            raw_date, side, raw_strike = m.groups()
            expiry = f"20{raw_date[0:2]}-{raw_date[2:4]}-{raw_date[4:6]}"
            strike = int(raw_strike) / 1000.0
            by_expiry.setdefault(expiry, {}).setdefault(strike, {})[side] = snap

        today = date.today()
        expiry = days_to_expiry = None
        for candidate in sorted(by_expiry):
            try:
                days_out = (date.fromisoformat(candidate) - today).days
            except ValueError:
                continue
            if days_out >= 5:
                expiry, days_to_expiry = candidate, days_out
                break
        if not expiry:
            return {"ok": False, "reason": "no expiry at least 5 days out"}

        both_sided = {k: v for k, v in by_expiry[expiry].items()
                      if "C" in v and "P" in v}
        if not both_sided:
            return {"ok": False, "reason": "no strike listed on both sides"}
        strike = min(both_sided, key=lambda k: abs(k - spot))

        def leg_mid(snap):
            quote = snap.get("latestQuote") or {}
            bid, ask = float(quote.get("bp") or 0), float(quote.get("ap") or 0)
            if bid > 0 and ask > 0:
                mid = (bid + ask) / 2
                return mid, (ask - bid) / mid if mid else None, "quote"
            last = float((snap.get("dailyBar") or {}).get("c") or 0)
            return (last, None, "last-trade") if last > 0 else (None, None, None)

        call_mid, call_spread, call_basis = leg_mid(both_sided[strike]["C"])
        put_mid, put_spread, put_basis = leg_mid(both_sided[strike]["P"])
        if not call_mid or not put_mid:
            return {"ok": False, "reason": "no usable ATM quotes"}
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
            "estimate_basis": "ATM straddle mid-quotes (indicative feed) over "
                              "the last daily close; not a probability, the "
                              "magnitude of move the market is pricing",
        }

    payload = produce()
    if not payload.get("ok"):
        return Sourced.unavailable(source, payload.get("reason", "unavailable"))
    return Sourced.live({k: v for k, v in payload.items() if k != "ok"}, source)
