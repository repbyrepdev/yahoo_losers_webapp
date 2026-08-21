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
