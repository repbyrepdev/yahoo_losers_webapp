<!-- OPENWIKI:START -->

## OpenWiki

This repository has a generated `openwiki/` evidence index. It is optional just-in-time context, not required startup reading.

- Treat source code and tests as authoritative. A brief's unknowns and review items are verification gaps, not automatic requirements.
- Prefer the narrowest quiet validation that proves the changed behavior. Preserve complete failure output.

The scheduled OpenWiki GitHub Actions workflow (`openwiki-update.yml`)
refreshes this generated `openwiki/` tree plus the OPENWIKI-marked
guidance blocks in `AGENTS.md` and `CLAUDE.md`, via a gated PR. The
hand-authored `wiki/` pages are a separate layer (doctrine and design
rationale), guarded by the deterministic `wiki-facts` CI check. Do not
hand-edit generated OpenWiki pages unless explicitly asked; prefer
updating source code/docs and letting OpenWiki regenerate.

<!-- OPENWIKI:END -->

## Documentation layers (repo-owned note, outside the OpenWiki block)

`openwiki-update.yml` (weekday cron) refreshes the generated `openwiki/`
tree and the OPENWIKI-marked blocks in `AGENTS.md`/`CLAUDE.md` via gated
PRs, guided by the standing brief in `openwiki/INSTRUCTIONS.md`. The
hand-authored `wiki/` pages are the doctrine layer, guarded by the
deterministic `wiki-facts` CI check. Never hand-edit generated pages —
put standing corrections in `openwiki/INSTRUCTIONS.md` instead (hand
edits get reverted by the generator's claims reconciliation; PR #84
proved it).
