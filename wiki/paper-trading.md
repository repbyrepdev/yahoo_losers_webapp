# Paper trading lifecycle

The paper account is a **dress rehearsal for live money**, so it mirrors
how a person actually trades — extended-hours limit entries, broker-
resident protective exits — and its record is what must EARN the switch.

## Nightly flow (snapshot cron, 01:15 UTC ≈ 9:15 PM ET, Tue–Sat)

1. **Entries**: whole-share LIMIT orders at reference × 1.02,
   `extended_hours=true`, day TIF — submitted after the extended session
   closes so they queue for the next pre-market. Idempotent client ids
   (`snap-{date}-{SYM}`) make re-runs safe.
2. **Protective pair on fills**: an OCO order (take-profit limit at
   ref × 1.05, stop at ref × 0.85, GTC) — **broker-resident**, so exits
   act continuously without the app being awake. (Probe-verified shape:
   `order_class=oco`, nested prices only, no top-level `limit_price`.)
3. **Rule-based exits** (the 9:15 sweep *queues*; the broker acts):
   close-basis stop at ref − 8%, window expiry after 7 trading sessions.
   Cancels are verified (200/204/404/410 all acceptable) before market
   sells; unexpected shorts are surfaced loudly, never skipped.
4. **Halts**: daily-loss halt at 2% of equity; re-entry cooldown 5
   sessions per symbol; phase guard blocks submits while the market is open.

## The rails (constants, shared with any future live mode)

`PAPER_TP_PCT = 5`, `PAPER_STOP_PCT = 8`, `PAPER_CATASTROPHE_STOP_PCT = 15`,
`PAPER_MAX_SESSIONS = 7`, plus the halt/cooldown above. The `/inspect`
page evaluates all four rails against any basis.

## Graduation to live money (`tracking.live_readiness`)

Live arming refuses unless ALL of:

- ≥ 100 resolved predictions, Brier ≤ 0.20
- ≥ 20 graded paper fills
- 28-day continuity **streak** of snapshots (gaps > 4 days break it)
- live keys configured AND `LIVE_TRADING_ARMED` set to an exact phrase no
  code path writes — a human-only act.

`_alpaca_trading_context` pairs the live base URL with live credentials
atomically, so live can never run on paper keys or vice versa. The
track-record page shows the scoreboard. Design when armed:
paper-alongside-live, with fill divergence as an execution-quality metric.
