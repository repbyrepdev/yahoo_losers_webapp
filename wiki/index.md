# yahoo_losers_webapp — Wiki

The authored doctrine layer, written by Claude Code (the repo's
standing agent) and review-hardened. The deterministic `wiki-facts` CI
check guards these pages' factual claims on every PR; corrections land
through the same gate as code. Authored methodology lives in the app's `/methodology` page;
this wiki documents **how the system is built**.

**Two documentation layers, one source per fact**: these `wiki/` pages
are the authored layer — doctrine, design rationale, the why. The
generated [`openwiki/`](../openwiki/quickstart.md) tree is the evidence
index — per-module behavior grounded in source, maintained by the
weekday `openwiki-update.yml` workflow. Code facts live there; judgment lives here.

## Pages

- [Architecture](architecture.md) — modules, layering, and why the shape is what it is
- [Data pipeline](data-pipeline.md) — every provider chain, fallback order, and cache doctrine
- [Paper trading lifecycle](paper-trading.md) — entries, exits, rails, and the live-money gate
- [Review and merge gate](review-gate.md) — the five checks, review cascade, and doctrine
- [Operations](operations.md) — deploys, crons, monitoring, and secrets
- [Testing](testing.md) — suite layout and the regression-test doctrine

## Ground rules the whole codebase obeys

1. **No fabrication**: a failed fetch renders as unavailable with its reason
   — never a substitute value (`provenance.Sourced` enforces this shape).
2. **Provenance everywhere**: every displayed number carries its source label.
3. **Error identity survives**: provider failures keep `reason` (exception
   name) + `detail` (full text, secrets redacted) so cache TTL classification
   and the UI both see the real cause.
4. **Spend abundant budgets before scarce ones**: FMP's 200/day is the last
   resort in every chain (one documented exception: analyst grade events).
5. **Paper before live**: real-money arming is refused until the recorded
   track record passes `tracking.live_readiness()` — thresholds are code.
