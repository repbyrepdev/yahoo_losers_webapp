"""Real economic release dates.

The previous implementation guessed: CPI was assumed to land on the 13th of the
month and FOMC on the 20th of eight assumed months. Those dates were then shown
to the user with a specific day and time, so they read as a published schedule
while frequently being wrong.

FOMC dates are scraped from federalreserve.gov, which is the authoritative
source and needs no credentials. Other releases come from the FRED release
calendar when FRED_API_KEY is set; without a key they are reported unavailable
rather than guessed.
"""

import logging
import os
import re
from datetime import date, datetime, timedelta
from typing import List, Optional

import requests

from provenance import Sourced

logger = logging.getLogger(__name__)

FOMC_URL = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"
FRED_RELEASES_URL = "https://api.stlouisfed.org/fred/release/dates"

# FRED release IDs for the releases that move broad equity markets.
FRED_RELEASES = {
    10: "CPI",
    50: "Employment Situation",
    53: "GDP",
    9: "Retail Sales",
}

_MONTHS = {m: i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"], start=1)}

_cache: dict = {}

# The Fed publishes its meeting calendar a year or more ahead, so this is close
# to static data -- refetching it hourly would be pointless traffic. FRED's
# release calendar is revised more often but still measured in days.
TTL_FOMC = 7 * 24 * 60 * 60
TTL_FRED = 24 * 60 * 60
TTL_FAILED = 15 * 60


def _cached(key, producer, ttl):
    import time
    hit = _cache.get(key)
    if hit and hit[0] > time.time():
        return hit[1]
    value = producer()
    # Don't hold a failed scrape for a week; retry it sooner.
    lifetime = ttl if value.get("ok") else TTL_FAILED
    _cache[key] = (time.time() + lifetime, value)
    return value


def fomc_meetings() -> Sourced:
    """Scheduled FOMC meeting dates, scraped from the Federal Reserve."""
    source = "federalreserve.gov:fomccalendars"

    def produce():
        try:
            response = requests.get(
                FOMC_URL, timeout=15,
                headers={"User-Agent": "Mozilla/5.0 (compatible; StockAnalyzer/1.0)"},
            )
            response.raise_for_status()
        except requests.RequestException as e:
            return {"ok": False, "reason": f"fed calendar unreachable ({type(e).__name__})"}

        html = response.text
        # Each meeting renders as a month block followed by a day or day range,
        # e.g. "January" + "27-28" (a trailing * marks a projections meeting).
        panels = re.findall(
            r'fomc-meeting__month[^>]*>\s*<strong>([^<]+)</strong>.*?'
            r'fomc-meeting__date[^>]*>\s*([^<]{1,40}?)\s*<',
            html, re.S,
        )
        years = re.findall(r'>(\d{4})\s+FOMC Meetings<', html)

        if not panels:
            return {"ok": False, "reason": "fed calendar layout changed; no meetings parsed"}

        # The page lists years in order; infer each meeting's year by watching
        # for the month sequence to wrap around.
        meetings, year_index, previous_month = [], 0, 0
        base_year = int(years[0]) if years else date.today().year

        for month_name, day_text in panels:
            month_name = month_name.strip().split("/")[0].strip()
            month = _MONTHS.get(month_name)
            if not month:
                continue
            if month < previous_month:
                year_index += 1
            previous_month = month

            projections = "*" in day_text
            days = re.findall(r"\d+", day_text)
            if not days:
                continue
            try:
                meeting_day = int(days[-1])
                meeting_date = date(base_year + year_index, month, meeting_day)
            except ValueError:
                continue

            meetings.append({
                "date": meeting_date.isoformat(),
                "name": "FOMC rate decision",
                "projections": projections,
                "impact": "high",
            })

        if not meetings:
            return {"ok": False, "reason": "no meeting dates parsed"}

        # Sanity check: the Fed holds eight meetings a year. A wildly different
        # count means the page structure changed and the parse is unreliable.
        if len(meetings) < 8:
            return {"ok": False, "reason": f"implausible meeting count ({len(meetings)})"}

        return {"ok": True, "meetings": sorted(meetings, key=lambda m: m["date"])}

    payload = _cached("fomc", produce, TTL_FOMC)
    if not payload.get("ok"):
        return Sourced.unavailable(source, payload.get("reason", "unavailable"))
    return Sourced.live(payload["meetings"], source)


def fred_releases(days_ahead: int = 45) -> Sourced:
    """Upcoming CPI / jobs / GDP / retail sales dates from the FRED calendar."""
    source = "fred:release/dates"
    api_key = os.environ.get("FRED_API_KEY")
    if not api_key:
        return Sourced.unavailable(source, "FRED_API_KEY not configured")

    def produce():
        today = date.today()
        events = []
        for release_id, name in FRED_RELEASES.items():
            try:
                response = requests.get(
                    FRED_RELEASES_URL,
                    params={
                        "release_id": release_id,
                        "api_key": api_key,
                        "file_type": "json",
                        "realtime_start": today.isoformat(),
                        "realtime_end": (today + timedelta(days=days_ahead)).isoformat(),
                        "include_release_dates_with_no_data": "true",
                    },
                    timeout=15,
                )
                response.raise_for_status()
                for entry in response.json().get("release_dates", []):
                    events.append({
                        "date": entry["date"],
                        "name": name,
                        "impact": "high",
                    })
            except (requests.RequestException, ValueError, KeyError) as e:
                logger.warning(f"FRED release {release_id} failed: {type(e).__name__}")
        if not events:
            return {"ok": False, "reason": "no release dates returned"}
        return {"ok": True, "events": sorted(events, key=lambda e: e["date"])}

    payload = _cached(f"fred:{days_ahead}", produce, TTL_FRED)
    if not payload.get("ok"):
        return Sourced.unavailable(source, payload.get("reason", "unavailable"))
    return Sourced.live(payload["events"], source)


def upcoming_events(days_ahead: int = 30) -> dict:
    """Merge the real calendars into one forward-looking list."""
    today = date.today()
    horizon = today + timedelta(days=days_ahead)

    events: List[dict] = []
    sources, unavailable = [], []

    fomc = fomc_meetings()
    if fomc.ok:
        sources.append(fomc.source)
        for meeting in fomc.value:
            meeting_date = date.fromisoformat(meeting["date"])
            if today <= meeting_date <= horizon:
                events.append({
                    **meeting,
                    "days_away": (meeting_date - today).days,
                    "source": fomc.source,
                })
    else:
        unavailable.append({"source": fomc.source, "reason": fomc.reason})

    fred = fred_releases(days_ahead)
    if fred.ok:
        sources.append(fred.source)
        for entry in fred.value:
            entry_date = date.fromisoformat(entry["date"])
            if today <= entry_date <= horizon:
                events.append({
                    **entry,
                    "days_away": (entry_date - today).days,
                    "source": fred.source,
                })
    else:
        unavailable.append({"source": fred.source, "reason": fred.reason})

    events.sort(key=lambda e: e["days_away"])

    return {
        "events": events,
        "sources": sources,
        "unavailable": unavailable,
        "horizon_days": days_ahead,
        "available": bool(events),
        "as_of": datetime.now().isoformat(),
    }
