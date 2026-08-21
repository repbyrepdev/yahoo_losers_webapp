# Doctrine

The judgment layer: why the system is built the way it is. Code facts
live in the generated [`openwiki/`](../openwiki/quickstart.md) tree,
which a weekday robot keeps pinned to source evidence. This page holds
what no generator can decide.

## Ground rules the whole codebase obeys

1. **No fabrication**: a failed fetch renders as unavailable with its
   reason — never a substitute, never a guess (`provenance.Sourced`).
2. **Provenance everywhere**: every displayed number carries its source.
3. **Error identity survives**: failures keep `reason` (exception name)
   and `detail` (full text, secrets redacted) end to end.
4. **Spend abundant budgets before scarce ones**: FMP's 200/day is the
   last resort in every chain (documented exception: analyst grade
   events, where FMP is the only per-firm source).
5. **Paper before live**: real money is refused until the recorded
   track record earns it.

## Why the graduation thresholds

The live gate demands ≥100 resolved predictions, Brier ≤ 0.20, ≥20
graded fills, and a 28-day unbroken snapshot streak — not because those
numbers are magic, but because each kills a specific self-deception:
100 resolutions beats small-sample luck; the Brier bound demands the
odds MEAN something; 20 fills proves execution (not just prediction);
the streak proves the pipeline runs unattended through real weeks. The
thresholds are code (`tracking.live_readiness`), so the webapp, the
arming path, and this page can never disagree.

## Testing philosophy

- Every bug fix gets a regression test pinning it — the suite is the
  changelog of everything that ever broke.
- Tests stub credentials; the suite must pass on a keyless machine.
- Price fixtures oscillate — monotonic series degenerate Wilder RSI to
  NaN and hide bugs.
- Disputed behavior gets BOTH directions pinned (the OCO payload test
  asserts the working shape passes AND the rejected shape fails).
