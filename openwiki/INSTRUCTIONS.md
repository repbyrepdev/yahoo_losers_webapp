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
6. Security posture: `/inspect/<symbol>` sanitizes its path segment to
   ticker characters; provider error text passes through
   `provenance.redact_secrets` before storage or display. Keep these
   facts in the routes/operations pages.
