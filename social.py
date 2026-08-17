"""Social sentiment from sources that actually publish it.

What this replaces:

- Twitter mention counts computed as `max(reddit * 2, stocktwits * 3)` and
  reported under a Twitter label. There is no free source for them, so they are
  gone rather than approximated.
- A "panic level" scaled as `total_mentions / 500`. Reddit search caps at 100
  results and a StockTwits stream page holds about 30 messages, so the scale
  topped out near 0.26 and the "high social volume" threshold of 2000 was
  unreachable. Every stock read calm.
- "Trending phrases" selected from two hard-coded lists of stock slang. Nothing
  was ever read from a message body.

StockTwits tags each message with a bullish/bearish sentiment that the previous
code ignored while calling len() on the same payload. That tag is the real
signal, and it comes with a denominator, so the result can be stated as
"68% bearish of 22 tagged messages" rather than an unanchored 1-10 score.
"""

import logging
import os
import re
import time
from collections import Counter
from typing import Dict, List, Optional

import requests

from provenance import Sourced
import secrets_store

logger = logging.getLogger(__name__)

STOCKTWITS_URL = "https://api.stocktwits.com/api/2/streams/symbol/{symbol}.json"
REDDIT_TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
REDDIT_SEARCH_URL = "https://oauth.reddit.com/r/{subs}/search"
REDDIT_SUBS = "wallstreetbets+stocks+investing+StockMarket"

USER_AGENT = "python:yahoo-losers-webapp:v2 (by /u/repbyrepdev)"

# Below this many tagged messages, a ratio is noise dressed up as a statistic.
MIN_TAGGED_MESSAGES = 5

TTL_SOCIAL = 15 * 60
TTL_FAILED = 2 * 60

_cache: Dict[str, tuple] = {}
_token: Dict[str, object] = {}

# Words too common to be interesting in a phrase count.
_STOPWORDS = {
    "the", "and", "for", "are", "but", "not", "you", "all", "any", "can", "her",
    "was", "one", "our", "out", "day", "get", "has", "him", "his", "how", "its",
    "new", "now", "old", "see", "two", "way", "who", "boy", "did", "she", "use",
    "your", "this", "that", "with", "from", "have", "will", "just", "like",
    "what", "when", "them", "then", "they", "been", "more", "some", "than",
    "into", "over", "only", "also", "were", "well", "much", "very", "back",
    "here", "there", "about", "would", "could", "should", "still", "going",
    "think", "know", "even", "make", "made", "want", "need", "long", "short",
    "https", "http", "com", "www", "amp",
}


def _cached(key: str, producer):
    hit = _cache.get(key)
    if hit and hit[0] > time.time():
        return hit[1]
    value = producer()
    ttl = TTL_SOCIAL if value.get("ok") else TTL_FAILED
    _cache[key] = (time.time() + ttl, value)
    return value


def _clean_tokens(text: str) -> List[str]:
    text = re.sub(r"http\S+", " ", text or "")
    text = re.sub(r"[$@#][A-Za-z]\w*", " ", text)  # cashtags and handles
    words = re.findall(r"[a-z']{3,}", text.lower())
    return [w for w in words if w not in _STOPWORDS]


# Legal-entity words. A phrase built only from these plus the company's own
# name is just the company restating itself, not a market theme.
_CORPORATE_WORDS = {
    "inc", "corp", "corporation", "incorporated", "holdings", "holding",
    "ltd", "limited", "group", "plc", "company", "companies", "sa", "nv",
    "ag", "spa", "ab", "asa", "class", "common", "stock", "shares", "adr",
}


def _phrases(messages: List[str], top: int = 3,
             exclude_terms: Optional[set] = None) -> List[dict]:
    """Most repeated two- and three-word phrases across real message bodies.

    Phrases made up entirely of the company's own name and legal-entity words
    are dropped. StockTwits messages routinely repeat the full company name, so
    without this the shortlist fills with "tenable holdings" and "holdings inc"
    instead of anything about the market.
    """
    blocked = {t.lower() for t in (exclude_terms or set())} | _CORPORATE_WORDS
    counter: Counter = Counter()
    for body in messages:
        tokens = _clean_tokens(body)
        for size in (2, 3):
            for i in range(len(tokens) - size + 1):
                counter[" ".join(tokens[i:i + size])] += 1

    # A phrase seen once is not trending, and one made only of blocked words
    # carries no information about why the stock moved.
    ranked = [
        (phrase, count) for phrase, count in counter.most_common(60)
        if count >= 2 and not all(word in blocked for word in phrase.split())
    ]

    # Drop overlapping phrases so a trigram and its component bigram do not both
    # occupy the shortlist. Containment is checked in both directions: ranking by
    # frequency can surface either one first, and only testing one direction let
    # "guidance cut" and "guidance cut again" through together.
    chosen: List[dict] = []
    for phrase, count in ranked:
        if any(phrase in existing["phrase"] or existing["phrase"] in phrase
               for existing in chosen):
            continue
        chosen.append({"phrase": phrase, "count": count})
        if len(chosen) >= top:
            break
    return chosen


def stocktwits(symbol: str, company_name: Optional[str] = None) -> Sourced:
    """Message volume, real bull/bear split, and phrases from actual message text."""
    source = "stocktwits:streams"
    exclude = {symbol.lower()} | set(re.findall(r"[a-z]{3,}", (company_name or "").lower()))

    def produce():
        try:
            response = requests.get(
                STOCKTWITS_URL.format(symbol=symbol.upper()),
                headers={"User-Agent": USER_AGENT},
                timeout=10,
            )
            response.raise_for_status()
            payload = response.json()
        except requests.RequestException as e:
            return {"ok": False, "reason": f"stocktwits unreachable ({type(e).__name__})"}
        except ValueError:
            return {"ok": False, "reason": "stocktwits returned invalid JSON"}

        messages = payload.get("messages") or []
        if not messages:
            return {"ok": False, "reason": "no messages on this symbol's stream"}

        bullish = bearish = 0
        bodies = []
        for message in messages:
            bodies.append(message.get("body") or "")
            tag = ((message.get("entities") or {}).get("sentiment") or {}).get("basic")
            if tag == "Bullish":
                bullish += 1
            elif tag == "Bearish":
                bearish += 1

        tagged = bullish + bearish
        return {
            "ok": True,
            "messages": len(messages),
            "tagged": tagged,
            "bullish": bullish,
            "bearish": bearish,
            # Only meaningful with enough tagged messages to be a ratio at all.
            "bearish_ratio": round(bearish / tagged, 3) if tagged >= MIN_TAGGED_MESSAGES else None,
            "phrases": _phrases(bodies, exclude_terms=exclude),
        }

    payload = _cached(f"st:{symbol.upper()}", produce)
    if not payload.get("ok"):
        return Sourced.unavailable(source, payload.get("reason", "unavailable"))
    return Sourced.live(payload, source)


def _reddit_token() -> Optional[str]:
    """Fetch and cache an app-only Reddit token.

    Reddit began returning 403 to unauthenticated search calls, which is why
    mention counts had been silently zero. Zero is dangerous here: it rendered
    as a calm, bullish-looking reading produced entirely by an auth failure.
    """
    client_id = secrets_store.get("REDDIT_CLIENT_ID")
    client_secret = secrets_store.get("REDDIT_CLIENT_SECRET")
    if not (client_id and client_secret):
        return None

    if _token.get("value") and _token.get("expires", 0) > time.time():
        return _token["value"]

    try:
        response = requests.post(
            REDDIT_TOKEN_URL,
            auth=(client_id, client_secret),
            data={"grant_type": "client_credentials"},
            headers={"User-Agent": USER_AGENT},
            timeout=10,
        )
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError) as e:
        logger.warning(f"Reddit token request failed: {type(e).__name__}")
        return None

    token = payload.get("access_token")
    if not token:
        return None
    _token["value"] = token
    _token["expires"] = time.time() + int(payload.get("expires_in", 3600)) - 60
    return token


def reddit(symbol: str, company_name: Optional[str] = None) -> Sourced:
    """Recent Reddit posts mentioning the symbol, via OAuth."""
    source = "reddit:oauth-search"
    exclude = {symbol.lower()} | set(re.findall(r"[a-z]{3,}", (company_name or "").lower()))

    token = _reddit_token()
    if not token:
        return Sourced.unavailable(
            source, "REDDIT_CLIENT_ID/REDDIT_CLIENT_SECRET not configured"
        )

    def produce():
        try:
            response = requests.get(
                REDDIT_SEARCH_URL.format(subs=REDDIT_SUBS),
                headers={"Authorization": f"Bearer {token}", "User-Agent": USER_AGENT},
                params={"q": symbol, "restrict_sr": "true", "sort": "new",
                        "t": "week", "limit": 100},
                timeout=10,
            )
            response.raise_for_status()
            payload = response.json()
        except requests.RequestException as e:
            return {"ok": False, "reason": f"reddit search failed ({type(e).__name__})"}
        except ValueError:
            return {"ok": False, "reason": "reddit returned invalid JSON"}

        posts = (payload.get("data") or {}).get("children") or []
        titles = [(p.get("data") or {}).get("title", "") for p in posts]
        return {
            "ok": True,
            "mentions": len(posts),
            # The endpoint caps at 100, so report saturation honestly rather
            # than implying the count is the true volume.
            "capped": len(posts) >= 100,
            "phrases": _phrases(titles, exclude_terms=exclude),
        }

    payload = _cached(f"rd:{symbol.upper()}", produce)
    if not payload.get("ok"):
        return Sourced.unavailable(source, payload.get("reason", "unavailable"))
    return Sourced.live(payload, source)


def sentiment(symbol: str, company_name: Optional[str] = None) -> dict:
    """Combined social read, with each source's availability stated."""
    st = stocktwits(symbol, company_name)
    rd = reddit(symbol, company_name)

    result = {
        "symbol": symbol,
        "stocktwits": {
            "available": st.ok,
            "source": st.source,
            "reason": None if st.ok else st.reason,
            **(st.value if st.ok else {}),
        },
        "reddit": {
            "available": rd.ok,
            "source": rd.source,
            "reason": None if rd.ok else rd.reason,
            **(rd.value if rd.ok else {}),
        },
    }

    # Phrases come from whichever sources produced any, merged by frequency.
    merged: Counter = Counter()
    for block in (st.value if st.ok else None, rd.value if rd.ok else None):
        for entry in (block or {}).get("phrases", []):
            merged[entry["phrase"]] += entry["count"]
    result["trending_phrases"] = [
        {"phrase": phrase, "count": count} for phrase, count in merged.most_common(3)
    ]

    ratio = (st.value or {}).get("bearish_ratio") if st.ok else None
    if ratio is None:
        result["overall"] = {
            "available": False,
            "reason": "not enough tagged messages to compute a ratio",
        }
        result["summary"] = "Social sentiment unavailable"
        return result

    tagged = st.value["tagged"]
    if ratio >= 0.7:
        label, color = "Strongly bearish", "#dc3545"
    elif ratio >= 0.55:
        label, color = "Bearish", "#fd7e14"
    elif ratio > 0.45:
        label, color = "Mixed", "#ffc107"
    else:
        label, color = "Bullish", "#28a745"

    result["overall"] = {
        "available": True,
        "bearish_ratio": ratio,
        "tagged_messages": tagged,
        "label": label,
        "color": color,
    }
    result["summary"] = f"{ratio:.0%} bearish of {tagged} tagged StockTwits messages"
    return result
