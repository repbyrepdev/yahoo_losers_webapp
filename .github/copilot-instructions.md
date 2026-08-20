# Copilot review instructions — yahoo_losers_webapp

Canonical build doctrine: `CLAUDE.md` at the repo root. Review against it.

Priorities, highest first:
1. **Silent failures** — swallowed exceptions, dropped rows without logs,
   fallbacks that run at only some failure exits of a producer.
2. **Provenance violations** — any displayed value without a `Sourced`
   provider label, any substitute/guessed number. Refusal over fabrication
   is the house rule.
3. **Error-identity loss** — provider failures must return the exception
   name as `reason` and full text as `detail`; a transient outage caching
   as structural absence ("no X published") is a Major.
4. **Budget safety** — FMP is capped at 200 requests/day; flag any path
   that can spend a scarce budget twice for one question, or fetch during
   page rendering.
5. **Trading-logic correctness** — paper-trading rails (entry band,
   take-profit, stops, session expiry, halts) are constants with exact
   semantics; flag off-by-one sessions, calendar-vs-trading-day confusion,
   order lifecycles that can double-fill or leave the account short.
6. **Untested fixes** — a bug fix without a regression test is incomplete.

Style: match surrounding code; comments state constraints, not narration.
Tests must pin wall-clock state (market phase, calendar, dates).
