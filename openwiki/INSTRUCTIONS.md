# OpenWiki standing brief (user-authored; read every run, never rewritten)

Scope: document this repository's behavior grounded in source. The
hand-authored `wiki/` tree is a SEPARATE doctrine layer — do not
duplicate its judgment content; link to it.

Standing corrections and priorities (from the PR #83 human review —
keep these true in every regeneration):

1. `openwiki/index.md` MUST link to the authored doctrine layer:
   `[Authored Wiki](../wiki/index.md)`.
2. `TTLCache.claim_once()` atomicity: cross-process ONLY when Redis
   backs the cache (`SET NX`); the no-Redis fallback lock and answer
   replay are process-local. Never describe the fallback as a
   cross-process guarantee.
3. Backtest limitations: `Ticker.history()` defaults to
   `auto_adjust=True`, so returns are computed on dividend-ADJUSTED
   closes. Describe this as an adjusted-price basis, not "dividend
   ignorance".
4. Docker smoke-check examples must run the container detached
   (`docker run -d`) before curling `/health` — a foreground run never
   reaches the curl.
5. The retired `wiki-maintenance.yml` workflow must not be mentioned;
   the docs automation is `openwiki-update.yml` (weekday cron).
6. Trading-rail constants must state their VALUES alongside their names
   in the paper-trading page (a deterministic CI gate,
   `tools/check_wiki_facts.py`, asserts name AND value presence — a
   formula reference alone fails the gate).
7. The authored judgment layer moved to `docs/` (doctrine.md,
   review-gate.md) — the old `wiki/` tree is gone. Link the authored
   layer as `[Authored docs](../docs/doctrine.md)`.
8. Security posture: `/inspect/<symbol>` sanitizes its path segment to
   ticker characters. Provider failure payloads are scrubbed by
   `provenance.redact_secrets` at the cache boundary
   (`market_data._cached` redacts `reason`/`detail` on every not-ok
   payload before storage), with additional boundary redaction inside
   the FMP/Finnhub helpers. Keep these facts in the routes/operations
   pages.

## Diagram hygiene (standing)

- Sequence diagrams for request/data flows; state diagrams for
  lifecycles; flowcharts organized with `subgraph` clusters.
- Prefer the ELK renderer on flowcharts:
  start fences with `%%{init: {"flowchart": {"defaultRenderer": "elk"}}}%%`.
- Decompose, never truncate: a multi-concern diagram becomes one
  COMPLETE diagram per concern; every component must appear in at least
  one diagram; never omit nodes for aesthetics.
- Full-topology "everything" views: link the project's Interactive
  graph page instead of drawing a mega-flowchart.
