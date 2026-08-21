# Testing

~400 tests, one suite (`tests/`), run in full by the pre-push hook, CI,
and before any completion claim (fresh evidence, never stale).

## Layout

| File | Covers |
|---|---|
| `test_sources.py` | Provider chains, fallbacks, paper lifecycle, redaction, FMP hygiene |
| `test_live_gate.py` | Graduation criteria, live-arming refusal matrix, inspector page states |
| `test_no_fabrication.py` | The Sourced contract: unavailable never renders as a number |
| `test_market_context.py`, `test_gold_standard.py`, `test_freshness.py`, `test_rank.py` | Scoring, calibration, cache TTLs, ranking |

## Doctrine

- **Every bug fix gets a regression test pinning it** — the suite is the
  changelog of everything that ever broke.
- Tests stub credentials (`get_secret` monkeypatch) — they must pass on a
  machine with no keys.
- Price fixtures oscillate (`×1.004 / ×0.998`) — monotonic series
  degenerate Wilder RSI to NaN and hide bugs.
- Both directions of a disputed behavior get pinned (e.g. the OCO payload
  shape test pins that the working shape passes AND the rejected
  alternative fails), so a future "fix" cannot silently swap them.
