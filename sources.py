"""Multi-provider failover layer: Alpaca, Finnhub, FMP and FINRA beside Yahoo.

Ordering principles (probe-verified per provider, 2026-08-20):
1. Spend abundant budgets before scarce ones -- Finnhub (60/min) and
   Alpaca (200/min) are effectively unlimited at this volume; FMP
   (250/day, hard in-code stop at 200) sits LAST in every chain.
2. Official APIs over scraping when data is equal; Yahoo keeps first
   place only for the product-defining screener, bundled quoteSummary
   fields, and strictly richer data (consolidated volume, options OI).

Finnhub-first: rating trends, company news, earnings calendar, and the
profile2 name/industry backup. Alpaca: movers screener (universe backup),
bars, IEX trades, indicative option snapshots (put/call + ATM straddle),
trading calendar, corporate actions, and the PAPER trading account --
entry limits at ref+2% extended-hours, GTC take-profits, expiry/stop
exits, loss-halt and cooldown rails (constants shared with any future
live mode). FMP: losers, targets, grades, earnings, splits, float, EOD
history. FINRA: consolidated short interest over FMP float. Keyed
providers carry atomic per-symbol-per-day request claims with same-day
answer replay, so budget can never be spent twice for one question.

Not free (probed, documented dead ends): Finnhub candles/price targets/
short interest; FMP stock news. Every value is Sourced with its provider
named; failures surface as unavailable with their error identity intact.
"""

import logging
import math
import os
import time
from datetime import date, datetime, timedelta, timezone
from typing import List, Optional

import requests

import market_data
from provenance import Sourced, redact_secrets
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


def _alpaca_trading_base(account: str = "paper") -> str:
    """The trading endpoint. Paper is the default and only self-serve path.

    Real money stays impossible by construction: the LIVE branch exists so
    the same rules engine can one day run both books, but it cannot arm
    itself. Arming requires ALL of, deliberately:
    1. LIVE_TRADING_ARMED set to the exact phrase "yes-i-accept-losses" --
       a human typed it; no code path sets it;
    2. LIVE_ALPACA_API_KEY / LIVE_ALPACA_API_SECRET configured;
    3. tracking.live_readiness() passing -- the recorded track record is
       the only authority that can promote this system to real dollars,
       and its thresholds are code, not midnight judgment.
    """
    if account == "live":
        if os.environ.get("LIVE_TRADING_ARMED") != "yes-i-accept-losses":
            raise RuntimeError("live trading is not armed (LIVE_TRADING_ARMED)")
        if not (get_secret("LIVE_ALPACA_API_KEY") and get_secret("LIVE_ALPACA_API_SECRET")):
            raise RuntimeError("live trading keys are not configured")
        import tracking as _tracking
        readiness = _tracking.live_readiness()
        if not readiness["ready"]:
            unmet = ", ".join(f"{c['name']} {c['actual']}/{c['required']}"
                              for c in readiness["criteria"] if not c["met"])
            raise RuntimeError(
                f"track record has not earned live money yet: {unmet}")
        return "https://api.alpaca.markets"
    configured = os.environ.get("ALPACA_PAPER_BASE", ALPACA_PAPER_BASE)
    if configured.rstrip("/") != ALPACA_PAPER_BASE:
        raise RuntimeError(
            f"refusing non-paper Alpaca endpoint {configured!r}; this app "
            "only ever trades simulated money")
    return ALPACA_PAPER_BASE


def _alpaca_trading_context(account: str = "paper"):
    """(base, headers) for the requested account -- the ONLY correct way to
    make an authenticated trading call. The live branch runs the full
    arming gate AND uses the LIVE credentials it verified; pairing the
    live base with paper headers (or vice versa) is impossible through
    this helper (local review, security).
    """
    base = _alpaca_trading_base(account)
    if account == "live":
        return base, {"APCA-API-KEY-ID": get_secret("LIVE_ALPACA_API_KEY"),
                      "APCA-API-SECRET-KEY": get_secret("LIVE_ALPACA_API_SECRET")}
    return base, _alpaca_headers()


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
    try:
        response = requests.get(f"{FMP_BASE}/{path}",
                                params={**params, "apikey": api_key}, timeout=20)
        response.raise_for_status()
    except requests.RequestException as e:
        # HTTPError text embeds the full URL -- including apikey. Re-raise
        # the same type with redacted text and no chained original, so no
        # caller's detail string or traceback can leak the key.
        raise type(e)(redact_secrets(e)) from None
    return response.json(), None


def _finnhub_get(path: str, params: dict):
    token = get_secret("FINNHUB_API_KEY")
    if not token:
        return None, "FINNHUB_API_KEY not configured"
    market_data._throttle()
    try:
        response = requests.get(f"{FINNHUB_BASE}/{path}",
                                params={**params, "token": token}, timeout=20)
        response.raise_for_status()
    except requests.RequestException as e:
        raise type(e)(redact_secrets(e)) from None
    return response.json(), None


def _compose_failure(payload: dict) -> str:
    """reason names the cause, detail carries the chain; a caller-facing
    message must lose neither (review: 'detail or reason' dropped the
    primary cause whenever both existed)."""
    reason = payload.get("reason")
    detail = payload.get("detail")
    if detail and reason and reason not in detail:
        return f"{reason}; {detail}"
    return detail or reason or "unavailable"


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

def _normalize_loser_rows(raw, cap=25):
    """Yahoo-shaped rows from a raw movers list, junk filtered.

    Raw screeners include sub-dollar paper and warrant/unit tickers
    (five-letter symbols ending W/U, or dotted classes like FTRA.WS) that
    the Yahoo cohort -- the population every hit rate was computed on --
    never contains. A failover day's universe must resemble that cohort.
    """
    rows = []
    for r in raw:
        sym = (r.get("symbol") or "").upper()
        pct = r.get("pct")
        price = r.get("price")
        if not sym or not isinstance(pct, (int, float)):
            continue
        if "." in sym or (len(sym) >= 5 and sym[-1] in ("W", "U")):
            continue
        if isinstance(price, (int, float)) and price < 1.0:
            continue
        rows.append({"Symbol": sym,
                     "Name": r.get("name") or sym,
                     "Change": str(r.get("change")),
                     "Percent Change": f"{pct:.2f}%",
                     "Volume": "n/a", "Market Cap": "n/a"})
        if len(rows) >= cap:
            break
    return rows


def alpaca_losers() -> Sourced:
    """The day's biggest losers from Alpaca's official movers screener.

    Second in the universe chain: an API with a per-minute budget beats
    FMP's 200/day when the Yahoo screener is down, and its list is
    minute-fresh (last_updated rides the payload).
    """
    source = "alpaca:movers-losers"

    def produce():
        try:
            payload, err = _alpaca_get(ALPACA_DATA_BASE,
                                       "/v1beta1/screener/stocks/movers",
                                       {"top": 50})
            if err:
                return {"ok": False, "reason": err}
        except Exception as e:
            return {"ok": False, "reason": f"alpaca movers failed ({type(e).__name__})"}
        raw = [{"symbol": r.get("symbol"), "pct": r.get("percent_change"),
                "change": r.get("change"), "price": r.get("price"), "name": None}
               for r in (payload.get("losers") or [])]
        rows = _normalize_loser_rows(raw)
        if not rows:
            return {"ok": False, "reason": "empty losers list"}
        return {"ok": True, "rows": rows}

    payload = market_data._cached("src:alpaca-losers", 10 * 60, produce)
    if not payload.get("ok"):
        return Sourced.unavailable(source, payload.get("reason", "unavailable"))
    return Sourced.live(payload["rows"], source)


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
            raw = [{"symbol": r.get("symbol"), "pct": r.get("changesPercentage"),
                    "change": r.get("change"), "price": r.get("price"),
                    "name": r.get("name")} for r in payload]
            rows = _normalize_loser_rows(raw)
            if not rows:
                return {"ok": False, "reason": "empty losers list"}
            return {"ok": True, "rows": rows}
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

def earnings_cache_key(symbol: str) -> str:
    # v2: v1 entries were poisoned when Finnhub's calendar ignored the symbol
    # param and every ticker inherited the market's next earnings date
    # (live 2026-08-20: STLD and MTSI wore SMTC's Aug 25).
    return f"src:earnings:v2:{symbol.upper()}"


def earnings_confirmed(symbol: str) -> Sourced:
    """Confirmed upcoming earnings date: Finnhub first, FMP fallback."""
    symbol = symbol.upper()
    key = earnings_cache_key(symbol)

    def produce():
        # Finnhub first: its per-minute allowance is effectively unlimited at
        # our volume, so FMP's 200/day stays in reserve (provider principle:
        # spend abundant budgets before scarce ones).
        span_from = date.today().isoformat()
        span_to = (date.today() + timedelta(days=90)).isoformat()
        finnhub_said_none = False
        try:
            payload, err = _finnhub_get("calendar/earnings",
                                        {"from": span_from, "to": span_to,
                                         "symbol": symbol})
            if not err:
                finnhub_said_none = True
                rows = (payload.get("earningsCalendar") or [])
                # Never trust the API-side filter: when the free tier ignores
                # the symbol param it returns the whole market's calendar, and
                # taking the earliest date stamps EVERY ticker with the same
                # day (live incident 2026-08-20).
                mine = sorted(r["date"] for r in rows
                              if r.get("date")
                              and str(r.get("symbol", "")).upper() == symbol)
                if mine:
                    return {"ok": True, "date": mine[0], "provider": "finnhub"}
        except Exception as e:
            logger.info(f"finnhub earnings unavailable: {type(e).__name__}")
        try:
            payload, err = _fmp_get("earnings-calendar",
                                    {"from": span_from, "to": span_to})
            if err:
                # Finnhub answered cleanly with no rows: that IS the answer;
                # FMP's transport error must not relabel it.
                if finnhub_said_none:
                    return {"ok": False,
                            "reason": "no confirmed earnings in the next 90 days"}
                return {"ok": False, "reason": err}
            if isinstance(payload, list):
                mine = sorted(r["date"] for r in payload
                              if r.get("symbol") == symbol and r.get("date"))
                if mine:
                    return {"ok": True, "date": mine[0], "provider": "fmp"}
            return {"ok": False, "reason": "no confirmed earnings in the next 90 days"}
        except Exception as e:
            if finnhub_said_none:
                return {"ok": False,
                        "reason": "no confirmed earnings in the next 90 days"}
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
# Entry band above the recorded reference: the buy limit. Caps chasing (a
# gap past the band is a recorded miss, not a worse fill) and makes the
# order eligible in extended hours, which market orders never are.
PAPER_ENTRY_BAND_PCT = 2.0
# Lifecycle rails -- identical constants for the eventual live mode; paper
# exists to rehearse exactly these numbers.
PAPER_TP_PCT = 5.0                    # take-profit at the recorded claim level
PAPER_STOP_PCT = 8.0                  # close-basis stop below ref (no wick-outs)
PAPER_MAX_SESSIONS = 7                # thesis expiry: the measured window
PAPER_REENTRY_COOLDOWN_SESSIONS = 5   # no revenge-buying a stopped name
PAPER_DAILY_LOSS_HALT_PCT = 2.0       # equity down 2% on the day: no new entries
PAPER_CATASTROPHE_STOP_PCT = 15.0     # broker-resident intraday floor (OCO leg)


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

    Picks are {"symbol", "price"} dicts sized to whole shares under the
    target notional (fractional orders were rejected live 2026-08-19).

    Orders are LIMIT at ref_price plus the entry band, extended-hours
    eligible, day TIF -- the workflow Damien actually trades: decide after
    the close, let the order work from pre-market, fill anywhere inside
    the band, and record a miss when the stock gaps beyond it (a capped
    entry, never a chased one). Market orders were the wrong instrument
    twice: opg expired unfilled (no IEX auction print for mid-caps,
    2026-08-20) and plain market cannot work extended hours at all.
    """
    source = "alpaca:paper-orders"
    # DAY market orders fill IMMEDIATELY while the market is open -- the
    # public snapshot route or a manual dispatch during regular hours would
    # buy intraday instead of at the next open (CR, PR 73). Entries submit
    # only outside the regular session.
    try:
        if market_data.market_phase().get("phase") == "open":
            return Sourced.unavailable(
                source, "market is open; paper entries submit only outside "
                        "regular hours so fills happen at the next open")
    except Exception:
        pass
    base = _alpaca_trading_base()   # raises on any non-paper endpoint
    headers = _alpaca_headers()
    if headers is None:
        return Sourced.unavailable(source, "Alpaca keys not configured")
    # Daily-loss halt: equity down PAPER_DAILY_LOSS_HALT_PCT on the day means
    # no NEW risk -- existing positions and exits keep managing themselves.
    try:
        account = _paper_get("/v2/account")
        equity = float(account.get("equity") or 0)
        last_equity = float(account.get("last_equity") or 0)
        if last_equity > 0 and equity <= last_equity * (1 - PAPER_DAILY_LOSS_HALT_PCT / 100.0):
            return Sourced.unavailable(
                source, f"daily loss halt: equity {equity:.2f} is more than "
                        f"{PAPER_DAILY_LOSS_HALT_PCT:.0f}% below yesterday's "
                        f"{last_equity:.2f}; no new entries today")
    except Exception as e:
        logger.info(f"account check unavailable ({type(e).__name__}); proceeding")
    # Re-entry cooldown: a name we exited recently is not a fresh setup.
    recently_exited = set()
    try:
        closed = _paper_get("/v2/orders", {"status": "closed", "limit": 200,
                                           "after": (datetime.now(timezone.utc) - timedelta(days=14)).isoformat()})
        today = _eastern_today()
        for o in closed:
            cid = o.get("client_order_id") or ""
            if cid.startswith(("snap-exit-", "snap-tp-")) and o.get("filled_at"):
                if _sessions_between(str(o["filled_at"])[:10], today) < PAPER_REENTRY_COOLDOWN_SESSIONS:
                    recently_exited.add(o.get("symbol"))
    except Exception as e:
        logger.info(f"cooldown check unavailable ({type(e).__name__}); proceeding")
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
        if symbol in recently_exited:
            failed.append({"symbol": symbol,
                           "reason": f"re-entry cooldown ({PAPER_REENTRY_COOLDOWN_SESSIONS} sessions after an exit)"})
            continue
        qty = max(1, int(PAPER_NOTIONAL_PER_PICK // price))
        limit_price = round(price * (1 + PAPER_ENTRY_BAND_PCT / 100.0), 2)
        client_order_id = f"snap-{_eastern_today().isoformat()}-{symbol}"
        try:
            market_data._throttle()
            response = requests.post(
                f"{base}/v2/orders", headers=headers, timeout=20,
                json={"symbol": symbol, "qty": str(qty),
                      "side": "buy", "type": "limit",
                      "limit_price": str(limit_price),
                      "extended_hours": True, "time_in_force": "day",
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
                              "ref_price": price, "limit_price": limit_price})
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
                         "entry_band_pct": PAPER_ENTRY_BAND_PCT,
                         "basis": "paper account, buy limit at ref plus "
                                  f"{PAPER_ENTRY_BAND_PCT:.0f}% band, extended-hours "
                                  "eligible, day TIF, simulated money"},
                        source)


def _paper_get(path, params=None):
    payload, err = _alpaca_get(_alpaca_trading_base(), path, params or {})
    if err:
        raise RuntimeError(err)
    return payload


def _entry_ref_price(order) -> Optional[float]:
    """Recover the recorded reference from an entry order's limit price.

    Entries are always placed at ref * (1 + band), so the ref is
    deterministic -- no side store to drift out of sync with the broker.
    """
    try:
        limit = float(order.get("limit_price") or 0)
        if limit <= 0:
            return None
        return round(limit / (1 + PAPER_ENTRY_BAND_PCT / 100.0), 4)
    except (TypeError, ValueError):
        return None


def _sessions_between(start_iso: str, end: date) -> int:
    from datetime import date as _date, timedelta as _td
    try:
        start = _date.fromisoformat(str(start_iso)[:10])
    except ValueError:
        return 0
    try:
        cal = trading_days_set(cache_only=True) or set()
    except Exception:
        cal = set()
    # The cached calendar spans roughly [today-7d, today+400d]; dates before
    # its earliest member must fall back to weekday counting or every older
    # position undercounts and the expiry stop never fires (CR, PR 74).
    min_cal = min(cal) if cal else None
    sessions, cursor = 0, start
    while cursor < end:
        cursor += _td(days=1)
        iso = cursor.isoformat()
        if cal and min_cal is not None and iso >= min_cal:
            if iso in cal:
                sessions += 1
        elif cursor.weekday() < 5:
            sessions += 1
    return sessions


def paper_manage_positions() -> Sourced:
    """The nightly lifecycle sweep: exits derive from the recorded claims.

    For every open position: ensure a GTC take-profit sell at
    ref * (1 + PAPER_TP_PCT); exit at the next open when the thesis
    expires (PAPER_MAX_SESSIONS elapsed -- the window the odds were
    measured over) or breaks (close <= ref * (1 - PAPER_STOP_PCT),
    close-basis so intraday wicks cannot shake positions out). Every
    action is idempotent via deterministic client ids and recorded with
    its reason.
    """
    source = "alpaca:paper-lifecycle"
    try:
        if market_data.market_phase().get("phase") == "open":
            return Sourced.unavailable(
                source, "market is open; lifecycle sweeps run off-hours so "
                        "exits queue for the next open")
    except Exception:
        pass
    headers = _alpaca_headers()
    if headers is None:
        return Sourced.unavailable(source, "Alpaca keys not configured")
    base = _alpaca_trading_base()
    today = _eastern_today()
    actions, held = [], []
    try:
        positions = _paper_get("/v2/positions")
        open_orders = _paper_get("/v2/orders", {"status": "open", "limit": 100})
        closed = _paper_get("/v2/orders", {"status": "closed", "limit": 200,
                                           "after": (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()})
    except Exception as e:
        return Sourced.unavailable(source, f"lifecycle fetch failed ({type(e).__name__})")

    entries_by_symbol = {}
    for o in closed:
        cid = o.get("client_order_id") or ""
        is_entry = (cid.startswith("snap-")
                    and not cid.startswith(("snap-tp-", "snap-exit-")))
        if is_entry and o.get("side") == "buy" and o.get("filled_at"):
            entries_by_symbol.setdefault(o["symbol"], o)

    open_sells = {}
    for o in open_orders:
        if o.get("side") == "sell":
            open_sells.setdefault(o["symbol"], []).append(o)

    for pos in positions:
        symbol = pos.get("symbol")
        qty = int(float(pos.get("qty") or 0))
        if not symbol:
            continue
        if qty < 0:
            # A short position should be impossible under these rails (OCO
            # legs are one-cancels-other), but a broker race or manual action
            # could create one -- and silently skipping it would leave an
            # unmanaged short invisible forever (CR CLI, local review).
            actions.append({"symbol": symbol, "action": "unexpected-short",
                            "qty": qty,
                            "reason": "short position outside the rails; "
                                      "needs manual attention -- not auto-managed"})
            continue
        if qty == 0:
            continue
        entry = entries_by_symbol.get(symbol)
        ref = _entry_ref_price(entry) if entry else None
        if ref is None:
            held.append({"symbol": symbol, "note": "no snap entry order found; unmanaged"})
            continue
        entry_date = str(entry.get("filled_at") or entry.get("submitted_at"))[:10]
        sessions = _sessions_between(entry_date, today)
        last_close = None
        try:
            bars = _paper_get_data_bar(symbol)
            last_close = bars
        except Exception:
            pass
        expired = sessions >= PAPER_MAX_SESSIONS
        stopped = (isinstance(last_close, (int, float))
                   and last_close <= ref * (1 - PAPER_STOP_PCT / 100.0))
        if expired or stopped:
            reason = "window expired" if expired else "stop: close below band"
            # Every resting sell must be CONFIRMED cancelled before the market
            # exit goes in: a failed cancel plus a market sell can fill twice
            # and leave the account short (CR, PR 74). A blocked cancel defers
            # the exit to the next sweep.
            cancel_blocked = False
            for o in open_sells.get(symbol, []):
                try:
                    resp = requests.delete(f"{base}/v2/orders/{o['id']}",
                                           headers=headers, timeout=20)
                    # 404/410: already gone (an OCO sibling cancel removes
                    # both legs) -- that IS a successful cancel.
                    if resp.status_code not in (200, 204, 404, 410):
                        cancel_blocked = True
                except Exception:
                    cancel_blocked = True
            if cancel_blocked:
                actions.append({"symbol": symbol, "action": "exit-blocked",
                                "reason": f"{reason}; resting sell cancel failed, retrying next sweep"})
                continue
            cid = f"snap-exit-{today.isoformat()}-{symbol}"
            try:
                market_data._throttle()
                resp = requests.post(f"{base}/v2/orders", headers=headers, timeout=20,
                                     json={"symbol": symbol, "qty": str(qty),
                                           "side": "sell", "type": "market",
                                           "time_in_force": "day",
                                           "client_order_id": cid})
                if resp.status_code == 422 and "client_order_id" in resp.text:
                    actions.append({"symbol": symbol, "action": "exit",
                                    "status": "already-queued", "reason": reason})
                else:
                    resp.raise_for_status()
                    actions.append({"symbol": symbol, "action": "exit",
                                    "status": resp.json().get("status"),
                                    "reason": reason, "sessions_held": sessions})
            except Exception as e:
                body = getattr(getattr(e, "response", None), "text", "") or str(e)
                actions.append({"symbol": symbol, "action": "exit-failed",
                                "reason": f"{type(e).__name__}: {body[:120]}"})
            continue
        if not open_sells.get(symbol):
            # OCO pair, broker-resident and continuous: the take-profit at the
            # claim's level and an intraday catastrophe floor; one cancels the
            # other, so the double-fill short CR flagged is structurally
            # impossible. The tighter close-basis stop above remains the
            # decision rule; this leg only catches intraday disasters.
            tp_price = round(ref * (1 + PAPER_TP_PCT / 100.0), 2)
            cat_price = round(ref * (1 - PAPER_CATASTROPHE_STOP_PCT / 100.0), 2)
            cid = f"snap-tp-{entry_date}-{symbol}"
            try:
                market_data._throttle()
                resp = requests.post(f"{base}/v2/orders", headers=headers, timeout=20,
                                     json={"symbol": symbol, "qty": str(qty),
                                           "side": "sell", "type": "limit",
                                           "order_class": "oco",
                                           "take_profit": {"limit_price": str(tp_price)},
                                           "stop_loss": {"stop_price": str(cat_price)},
                                           "time_in_force": "gtc",
                                           "client_order_id": cid})
                if resp.status_code == 422 and "client_order_id" in resp.text:
                    actions.append({"symbol": symbol, "action": "protective-pair",
                                    "status": "already-placed", "tp": tp_price,
                                    "catastrophe_stop": cat_price})
                else:
                    resp.raise_for_status()
                    actions.append({"symbol": symbol, "action": "protective-pair",
                                    "status": resp.json().get("status"),
                                    "tp": tp_price, "catastrophe_stop": cat_price})
            except Exception as e:
                body = getattr(getattr(e, "response", None), "text", "") or str(e)
                actions.append({"symbol": symbol, "action": "tp-failed",
                                "reason": f"{type(e).__name__}: {body[:120]}"})
        held.append({"symbol": symbol, "qty": qty, "ref": ref,
                     "sessions_held": sessions, "last_close": last_close})
    return Sourced.live({"actions": actions, "positions": held,
                         "rails": {"tp_pct": PAPER_TP_PCT,
                                   "stop_pct": PAPER_STOP_PCT,
                                   "max_sessions": PAPER_MAX_SESSIONS}}, source)


def _paper_get_data_bar(symbol: str):
    """Latest daily close for the close-basis stop.

    The bars endpoint sorts ASCENDING by default, so limit=1 returns the
    OLDEST bar in range (CR, PR 74); the latest-bar endpoint returns the
    single canonical newest one.
    """
    payload, err = _alpaca_get(ALPACA_DATA_BASE, "/v2/stocks/bars/latest",
                               {"symbols": symbol.upper(), "feed": "iex"})
    if err:
        raise RuntimeError(err)
    bar = (payload.get("bars") or {}).get(symbol.upper())
    return float(bar["c"]) if bar else None


def paper_account_overview() -> Sourced:
    """Equity, positions and working orders for the page's paper section.

    Read-only and cached five minutes: three requests per refresh against
    a 200/min allowance. Positions carry Alpaca's own marks (entry, current
    price, unrealized P/L), so the page repeats the broker's numbers rather
    than recomputing them.
    """
    source = "alpaca:paper-account"
    key = "src:paper-account"

    def produce():
        try:
            account = _paper_get("/v2/account")
            positions = _paper_get("/v2/positions")
            open_orders = _paper_get("/v2/orders", {"status": "open", "limit": 50})
        except Exception as e:
            # Keep the underlying words: the UI shows the real cause and
            # _cached classifies on detail (transient vs rate-limited),
            # same identity rule as every other producer (Copilot, PR 75).
            return {"ok": False, "reason": type(e).__name__,
                    "detail": f"{type(e).__name__}: {e}"}
        equity = float(account.get("equity") or 0)
        last_equity = float(account.get("last_equity") or 0)
        rows = []
        skipped = 0
        for p in positions:
            try:
                # Broker-reported quantity verbatim: int() would floor a
                # fractional position (0.25 -> 0) and contradict Alpaca's
                # own market_value beside it (Copilot, PR 75).
                raw_qty = float(p.get("qty") or 0)
                qty = int(raw_qty) if raw_qty == int(raw_qty) else round(raw_qty, 4)
                rows.append({"symbol": p.get("symbol"),
                             "qty": qty,
                             "entry": round(float(p.get("avg_entry_price") or 0), 2),
                             "current": round(float(p.get("current_price") or 0), 2),
                             "upl_pct": round(float(p.get("unrealized_plpc") or 0) * 100, 2),
                             "market_value": round(float(p.get("market_value") or 0), 2)})
            except (TypeError, ValueError):
                skipped += 1
                continue
        if skipped:
            logger.info(f"paper overview skipped {skipped} malformed position row(s)")
        working = [{"symbol": o.get("symbol"), "side": o.get("side"),
                    "type": o.get("type"), "limit_price": o.get("limit_price"),
                    "client_order_id": o.get("client_order_id")}
                   for o in open_orders]
        return {"ok": True, "equity": round(equity, 2),
                "cash": round(float(account.get("cash") or 0), 2),
                "day_change_pct": (round((equity / last_equity - 1) * 100, 2)
                                   if last_equity > 0 else None),
                "as_of": _eastern_today().isoformat(),
                "positions": rows, "working_orders": working}

    payload = market_data._cached(key, 5 * 60, produce)
    if not payload.get("ok"):
        return Sourced.unavailable(
            source, payload.get("detail") or payload.get("reason", "unavailable"))
    return Sourced.live({k: v for k, v in payload.items() if k != "ok"}, source)


def paper_recent_fills(days: int = 7) -> Sourced:
    """Recent paper fills, for the slippage record."""
    source = "alpaca:paper-fills"
    try:
        after = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
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


def _alpaca_option_snapshots(symbol: str, until: str):
    """Fetch and merge every page of the indicative option-snapshot chain.

    Returns (snapshots, error_reason): partial chains bias anything computed
    from them (contracts sort C-before-P), so past the five-page budget the
    caller gets a refusal, never a truncation.
    """
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
                return None, err
        except Exception as e:
            return None, f"alpaca options failed ({type(e).__name__})"
        snapshots.update(payload.get("snapshots") or {})
        token = payload.get("next_page_token")
        if not token:
            break
    else:
        return None, "options chain exceeds page budget"
    if not snapshots:
        return None, "no listed options"
    return snapshots, None


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
        snapshots, err = _alpaca_option_snapshots(symbol, until)
        if err:
            return {"ok": False, "reason": err}
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
        snapshots, err = _alpaca_option_snapshots(symbol, until)
        if err:
            return {"ok": False, "reason": err}

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

        # Eastern, matching the Yahoo path: near midnight UTC the server
        # date could select a different expiry than the primary figure.
        today = market_data._eastern_now().date()
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


def company_profile(symbol: str) -> Sourced:
    """Name and industry from Finnhub's profile2 (free, verified 2026-08-20).

    Partial by design: profile2 carries industry but not the GICS sector
    Yahoo reports, so sector stays honestly absent in fallback mode.
    """
    source = "finnhub:profile2"
    key = f"src:profile:{symbol.upper()}"

    def produce():
        try:
            payload, err = _finnhub_get("stock/profile2",
                                        {"symbol": symbol.upper()})
            if err:
                return {"ok": False, "reason": err}
            if not payload or not payload.get("name"):
                return {"ok": False, "reason": "no profile published"}
            return {"ok": True, "name": payload.get("name"),
                    "industry": payload.get("finnhubIndustry")}
        except Exception as e:
            return {"ok": False, "reason": f"finnhub profile failed ({type(e).__name__})"}

    payload = market_data._cached(key, 7 * 24 * 60 * 60, produce)
    if not payload.get("ok"):
        return Sourced.unavailable(source, payload.get("reason", "unavailable"))
    return Sourced.live({k: v for k, v in payload.items() if k != "ok"}, source)


def daily_bars(symbol: str, days: int = 180) -> Sourced:
    """Daily OHLCV bars from Alpaca, shaped for the technicals computation.

    The history fallback the README promises: when Yahoo's chart endpoint
    fails, the mean-reversion factors compute from IEX bars instead of going
    dark. IEX volume is a subset of consolidated volume -- the source label
    says so, and the honest alternative is no technicals at all.
    """
    source = "alpaca:bars(iex)"

    def produce():
        start = (date.today() - timedelta(days=int(days * 1.6))).isoformat()
        try:
            payload, err = _alpaca_get(ALPACA_DATA_BASE,
                                       f"/v2/stocks/{symbol.upper()}/bars",
                                       {"timeframe": "1Day", "start": start,
                                        "limit": 400, "feed": "iex",
                                        "adjustment": "split"})
            if err:
                return {"ok": False, "reason": err}
        except Exception as e:
            return {"ok": False, "reason": f"alpaca bars failed ({type(e).__name__})"}
        bars = payload.get("bars") or []
        if len(bars) < 30:
            return {"ok": False, "reason": f"insufficient history ({len(bars)} bars)"}
        return {"ok": True, "bars": [{"t": b.get("t"), "o": b.get("o"),
                                      "h": b.get("h"), "l": b.get("l"),
                                      "c": b.get("c"), "v": b.get("v")}
                                     for b in bars]}

    payload = market_data._cached(f"src:bars:{symbol.upper()}", 15 * 60, produce)
    if not payload.get("ok"):
        return Sourced.unavailable(source, payload.get("reason", "unavailable"))
    return Sourced.live(payload["bars"], source)


def fmp_eod_bars(symbol: str, days: int = 180) -> Sourced:
    """Close+volume EOD history from FMP -- third string for technicals.

    The light endpoint carries consolidated closes and volume (no
    high/low), which is exactly what the technicals computation consumes.
    Day-stamped like every scarce-budget call.
    """
    source = "fmp:eod-history"
    key = f"src:eod:{symbol.upper()}"

    def produce():
        day = date.today().isoformat()
        answer_key = f"src:eod:answer:{symbol.upper()}:{day}"
        if not market_data._cache.claim_once(
                f"src:eod:{symbol.upper()}:{day}", 24 * 60 * 60):
            # Same bounded replay wait as the other day-stamped FMP
            # producers: the winner is probably mid-request, so poll for
            # its stored answer before conceding unavailable.
            for _ in range(6):
                replay = market_data._cache.get(answer_key)
                if replay is not None:
                    return replay
                time.sleep(0.5)
            return {"ok": False, "reason": "fmp eod request in flight"}
        def _remember(result):
            market_data._cache.set(answer_key, result, 24 * 60 * 60)
            return result
        try:
            payload, err = _fmp_get("historical-price-eod/light",
                                    {"symbol": symbol.upper()})
            if err:
                return _remember({"ok": False, "reason": err})
            rows, skip_reasons = [], {}
            def _skip(why):
                skip_reasons[why] = skip_reasons.get(why, 0) + 1
            for r in (payload if isinstance(payload, list) else []):
                if not isinstance(r, dict):
                    _skip("non-object")
                    continue
                try:
                    date.fromisoformat(r.get("date") or "")
                except (TypeError, ValueError):
                    _skip("invalid date")
                    continue
                if r.get("price") is None:
                    _skip("missing field")
                    continue
                # Convert inside the loop: one malformed value skips ONE
                # row instead of raising and rejecting the whole response.
                try:
                    close = float(r["price"])
                    vol = float(r.get("volume") or 0)
                except (TypeError, ValueError):
                    _skip("non-numeric")
                    continue
                if not (math.isfinite(close) and math.isfinite(vol)):
                    _skip("non-finite")
                    continue
                rows.append({"t": r["date"], "c": close, "v": vol})
            skipped = sum(skip_reasons.values())
            if skipped:
                logger.info(f"fmp eod skipped {skipped} malformed row(s) "
                            f"for {symbol}: {skip_reasons}")
            if len(rows) < 30:
                return _remember({"ok": False,
                                  "reason": f"insufficient history ({len(rows)} rows, "
                                            f"skipped {skip_reasons or 0})"})
            rows.sort(key=lambda r: r["t"])
            return _remember({"ok": True, "skipped_rows": skipped,
                              "bars": rows[-days:]})
        except Exception as e:
            return _remember({"ok": False, "reason": type(e).__name__,
                              "detail": redact_secrets(
                                  f"fmp eod failed: {type(e).__name__}: {e}")})

    payload = market_data._cached(key, 24 * 60 * 60, produce)
    if not payload.get("ok"):
        return Sourced.unavailable(source, _compose_failure(payload))
    return Sourced.live(payload["bars"], source)
