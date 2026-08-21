<!-- OPENWIKI:START -->

## OpenWiki

This repository has a generated `openwiki/` evidence index. It is optional just-in-time context, not required startup reading.

- Treat source code and tests as authoritative. A brief's unknowns and review items are verification gaps, not automatic requirements.
- Prefer the narrowest quiet validation that proves the changed behavior. Preserve complete failure output.

The scheduled OpenWiki GitHub Actions workflow refreshes the repository wiki. Do not hand-edit generated OpenWiki pages unless explicitly asked; prefer updating source code/docs and letting OpenWiki regenerate.

<!-- OPENWIKI:END -->

## Documentation layers (repo-owned note, outside the OpenWiki block)

`openwiki-update.yml` (weekday cron) refreshes the generated `openwiki/`
tree and the OPENWIKI-marked blocks in `AGENTS.md`/`CLAUDE.md` via gated
PRs, guided by the standing brief in `openwiki/INSTRUCTIONS.md`. The
hand-authored `docs/` pages (doctrine.md, review-gate.md) are the
judgment layer; the deterministic `wiki-facts` check gates factual
claims across openwiki/, docs/, and README. Never hand-edit generated pages —
put standing corrections in `openwiki/INSTRUCTIONS.md` instead (hand
edits get reverted by the generator's claims reconciliation; PR #84
proved it).
### Chat-lane runbook (in-session OpenWiki runs)

When running the OpenWiki lifecycle in a session, the wiki work is only
half the job. After `openwiki_finish` returns `complete`, ALWAYS:

1. Branch, commit ONLY `openwiki/` + `AGENTS.md`/`CLAUDE.md` marked
   blocks (never `git add -A`), push, open the PR.
2. Arm it: `gh pr merge --auto --squash` (the generated-docs lane; the
   ruleset still blocks on red checks or open threads).
3. Confirm the hub dispatch after merge: the `notify-wiki-hub` run goes
   green and repbyrep-wiki's deploy fires within ~2 minutes.

Skipping any step is non-destructive (gates stall, cron self-heals next
weekday) — but do not rely on that; follow the list.

