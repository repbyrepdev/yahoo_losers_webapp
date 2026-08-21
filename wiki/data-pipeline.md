# Data pipeline

Every fact on a page comes through a **provider chain**: an ordered list
of sources tried until one answers. Chains follow two principles —
**official API beats scraping when data quality is equal**, and **spend
abundant budgets before scarce ones** (FMP's 200 requests/day is the hard
stop, so it is last everywhere, with one exception noted below).

## The chains

| Feed | 1st | 2nd | 3rd |
|---|---|---|---|
| Losers universe | Yahoo screener | Alpaca movers (junk-filtered) | FMP |
| Price history | Yahoo batch chart | Alpaca IEX bars | FMP EOD light |
| Quotes | Alpaca | Finnhub | FMP |
| Analyst targets | Yahoo | FMP summary | — |
| Ratings | Finnhub | Yahoo | — |
| Options (put/call, straddle) | Yahoo (has OI) | Alpaca indicative | — |
| Short float | Yahoo | FINRA short volume ÷ FMP float | — |
| Earnings (confirmed) — `earnings_confirmed` | Finnhub (symbol-filtered) | FMP | — |
| Earnings (estimate) — `market_data.earnings_date` | Yahoo | — | — |
| News | Finnhub | Yahoo | — |
| Company profile | Yahoo | Finnhub profile2 | — |
| Calendar / splits | Alpaca | FMP | — |
| Analyst grade EVENTS | **FMP first** (only per-firm events source) | Finnhub | — |

Fallbacks run at **every** producer exit — refusal *and* exception alike.
A dual failure reports every provider's reason in `detail`
(`"…; fallbacks: alpaca: …; fmp: …"`).

## Cache doctrine (`market_data._cached`)

- **TTL classification** happens on `detail or reason`: rate-limit text →
  `TTL_RATE_LIMITED` (15 min); structural negatives ("no listed options")
  need **two consecutive confirmations** before the 6h negative TTL, since
  degraded providers return empty 200s wearing structural prose.
- **Day-claims** (`claim_once`): scarce daily calls (FMP) are claimed
  atomically (Redis SET NX / lock-held local); losers of the claim poll
  for the winner's stored answer (bounded wait) instead of caching a miss.
- **TTL stretch**: `_effective_ttl` stretches upward under pressure,
  never below base; jitter is upward-only.
- **Cooldown divert**: Yahoo 429/401 puts the whole lane on cooldown and
  diverts to fallbacks rather than hammering.

## Secret hygiene

Provider keys ride in query params for FMP/Finnhub, and HTTP errors embed
the full URL. `provenance.redact_secrets` strips `apikey`/`token` values
**at the provider boundary** (the helpers re-raise sanitized exceptions,
`from None`), so no detail string, cached payload, page, or traceback can
carry a key. Regression-tested down to `traceback.format_exception`.
