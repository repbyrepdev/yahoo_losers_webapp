# yahoo_losers_webapp — build doctrine (SSOT)

> **Agent context**: two documentation layers. `openwiki/quickstart.md`
> is the generated evidence index (per-module behavior grounded in
> source; maintained by the `openwiki-update.yml` weekday workflow —
> exempt from markdownlint per `.markdownlint-cli2.yaml`, the generator
> owns its formatting). `wiki/index.md` is the authored doctrine layer
> (review gate, graduation rationale, design judgment; guarded by the
> deterministic `wiki-facts` check). Corrections go through PRs like code.

Flask app on Render analyzing Yahoo's daily losers with empirical odds and a
paper-trading rehearsal account. This file is the canonical process; the
same doctrine is mirrored for reviewers in `.coderabbit.yaml` and
`.github/copilot-instructions.md`. If they drift, this file wins.

## Non-negotiable code rules

- **Every displayed value carries provenance** (`Sourced`, provider named).
  A value that cannot be sourced renders as unavailable with its reason —
  never a substitute, never a guess.
- **Refusal over fabrication, always.**
- **Error identity is preserved**: provider failures return
  `reason` (exception name) + `detail` (full text) so cache TTL
  classification and the UI both see the real cause. A transient outage
  must never cache as structural absence ("no X published").
- **No silent failures**: skipped rows, deferred actions, and blocked
  operations are counted, logged, and recorded with reasons.
- **Provider ordering principles**: spend abundant budgets before scarce
  ones (FMP's 200/day is the scarcest — last in every chain it appears in);
  official APIs over scraping when data is equal. Scarce-budget calls carry
  atomic per-symbol-per-day claims with same-day answer replay.
- **Fallbacks run at every failure exit** of a producer, not just the first.
- **Nothing fetches on a render path** — lanes and caches feed the page.
- **Paper-trading rails are constants** shared with any future live mode;
  changing them is a deliberate, reviewed act. Live arming is Damien-only.

## Process per change

1. Branch from fresh `origin/main` (`git fetch` first — stale-base PRs run
   zero workflows when CONFLICTING).
2. Build with tests: every bug fix gets a regression test pinning it;
   suites must be green locally (`../venv/bin/python -m pytest tests/ -q`)
   before any push. Never pipe pytest through `tail` without capturing the
   exit code.
3. Markdown changes must pass `npx markdownlint-cli2` before commit.
4. **Local review before push on non-trivial diffs**: `coderabbit review
   --plain` and/or `copilot -p "review …"` on the diff. Evaluate findings —
   reviewers err; fix what is real; post the local summary as a PR comment
   for the audit trail.
5. PR with a clear body. Merges into `main` are MECHANICALLY gated by a
   repo ruleset: the `pytest` check must be green and every review thread
   resolved (ruleset "Merge gate", id 21115000 — if a CI job is ever
   renamed, update the ruleset's required checks or every merge blocks).
   Required checks: `pytest`, `gitleaks`, `ruff`, `markdownlint`, and
   `pip-audit` (gitleaks = full-history secret scan;
   the runtime analogue is `provenance.redact_secrets` at provider
   boundaries — gitleaks covers what reaches git, redaction covers what
   reaches pages and logs).
   **The server gate**: CI green AND at least one
   server-side review (CodeRabbit primary; request Copilot
   `copilot-pull-request-reviewer[bot]` when CR is rate-limited or silent
   15+ min past CI-green) — read COMPLETELY: review bodies (outside-diff
   and nitpick sections included), inline comments, thread states. Address
   or rebut every finding; resolve threads with evidence. Never merge red
   or unread. CI green ≠ reviewed — they are independent systems.
   **Arm auto-merge only AFTER the review lands.** `gh pr merge --auto`
   at PR-open time lets a fast CI outrun the reviewer and merge the PR
   mid-review (incident, PR #77: merged 30s after CI-green; CodeRabbit
   aborted with "the pull request is closed"). Thread-resolution rules
   cannot catch it — zero posted threads counts as zero unresolved.
   Sequence: open PR → review posts → read all → fix/rebut → resolve →
   then arm. If it ever slips, run the local reviewer over the exact
   unreviewed commit range and ship real findings as a follow-up PR.
**The whole machine, end to end** (what actually stops a bad change):
   local pre-push hook (full pytest + gitleaks history scan) → PR (CI:
   `pytest`, `gitleaks`, `ruff` bug-classes, `markdownlint`, `pip-audit`
   on the resolved environment; all unconditional so required checks
   always report) → server review cascade (CodeRabbit → Copilot when CR
   is metered) read completely → threads resolved → auto-merge armed
   LAST → squash to `main` → Render deploys (Python pinned in
   `.python-version`; CI matches) → deploy watched by SHA → live pages
   spot-verified → nightly snapshot cron exercises the paper lifecycle.
   Repo settings: auto-delete merged branches; squash-only. A weekly
   `pip-audit` cron reds the repo when a new CVE lands with no code
   change. Local venv note: the Mac venv runs 3.9, so security floors
   that need ≥3.10 resolve in CI/prod but not locally — trust the CI
   audit, not a local one.
6. **Reviewer budgets**: ≤1 CodeRabbit summon per PR per hour, silent
   polling otherwise; batch related changes (30 PRs/week ground CR's meter
   to ~1/hour once). `bash tools/review_budget_status.sh` shows live
   pressure. Comments re-anchor to new commits — judge newness by
   timestamp, not commit id.
7. Merge (squash) → Render auto-deploys `main`. Verify by deploy status for
   the exact SHA, then at most two spaced page GETs (never refresh loops);
   `/refresh` forces a view rebuild when needed.
8. If behavior or providers changed: update README (it IS the /methodology
   page), and keep the provider-chain table truthful — the code must honor
   every documented claim.

## Operational map

- Providers, chains, budgets: README "Provider chains" section.
- Paper lifecycle + rails: README "Paper trading" section.
- Snapshots (`data/snapshots/`) are the tamper-evident record; the nightly
  digest issue is the alert channel.
- Secrets: macOS Keychain locally, Render env in production. Never dotfiles.

<!-- OPENWIKI:START -->

## OpenWiki

See [AGENTS.md](AGENTS.md) for OpenWiki agent instructions.

<!-- OPENWIKI:END -->
