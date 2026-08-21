# Review and merge gate

Nothing reaches `main` except a squash-merged PR with five green checks
and every review thread resolved (ruleset "Merge gate", id 21115000).
CLAUDE.md is the doctrine SSOT; this page is the map.

## The five required checks

| Check | What it stops |
|---|---|
| `pytest` | Behavior regressions (full suite, ~400 tests) |
| `gitleaks` | Committed credentials (full history, every PR) |
| `ruff` | Bug-class lint: E9, F, B015, B018 — undefined names, unused code, useless expressions |
| `markdownlint` | Markdown rot (gold config, whole repo) |
| `pip-audit` | Known CVEs in the resolved dependency tree (also weekly cron) |

All jobs are **unconditional** — no `paths:` filters — because a required
check that never reports blocks the merge forever (the name-match trap).

## Review cascade

1. **Local, pre-push**: the pre-push hook runs pytest + gitleaks;
   non-trivial diffs get `coderabbit review --committed --base main -c
   CLAUDE.md` (doctrine attached).
2. **Server, per PR**: CodeRabbit auto-review first; when its meter is
   dry (fair-usage: heavy weeks grind it to 1/hour), request Copilot
   (`copilot-pull-request-reviewer[bot]`, posts in ~2–5 min). Read
   EVERYTHING: review bodies, suppressed/outside-diff sections, inline
   comments. Fix or rebut with evidence; resolve each thread.
3. **Arm auto-merge only AFTER the review lands.** Arming at PR-open let
   CI outrun the reviewer and merged PR #77 mid-review — zero posted
   threads counts as zero unresolved. Sequence: review posts → read all
   → fix → resolve → then `gh pr merge --auto --squash`.

## Reviewer track record (why the cascade earns its cost)

Across the hardening arc: 23+ findings; genuinely real catches included a
live-credentials pairing gap, an API-key-in-error-text leak path, and a
merge-gate sequencing hole. Reviewers also err — findings are evaluated
against the code, and wrong ones are rebutted with probe output on the
thread, not silently ignored.
