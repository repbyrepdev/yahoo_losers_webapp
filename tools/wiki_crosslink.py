#!/usr/bin/env python3
"""PR -> wiki cross-linker: map the PR's changed files to the wiki pages
that document them, using openwiki/source-map.md as the reverse index.
Deterministic; posts/updates ONE marker-tagged comment; silent when no
pages match."""
import json
import re
import subprocess
import sys

MARKER = "<!-- wiki-crosslink -->"


def run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True, check=True).stdout


def main(pr):
    changed = run(["gh", "pr", "diff", pr, "--name-only"]).split()
    smap = open("openwiki/source-map.md", encoding="utf-8").read()
    pages = {}
    for line in smap.splitlines():
        for f in changed:
            base = f.split("/")[-1]
            if base and f"`{base}`" in line or f"`{f}`" in line:
                for title, target in re.findall(r"\[([^\]]+)\]\(([^)]+\.md)\)",
                                                line):
                    pages.setdefault(title, target.lstrip("./"))
    if not pages:
        print("no documented pages touched")
        return 0
    repo = json.loads(run(["gh", "repo", "view", "--json",
                           "nameWithOwner"]))["nameWithOwner"]
    links = "\n".join(
        f"- [{t}](https://github.com/{repo}/blob/main/openwiki/{p})"
        for t, p in sorted(pages.items()))
    body = (f"{MARKER}\n📚 **This PR touches documented territory** — "
            f"the wiki pages covering these files:\n\n{links}\n\n"
            f"<sub>Deterministic lookup via openwiki/source-map.md; the "
            f"weekday docs robot reconciles content after merge.</sub>")
    existing = json.loads(run(["gh", "pr", "view", pr, "--json", "comments"]))
    for c in existing.get("comments", []):
        if MARKER in (c.get("body") or ""):
            print("marker comment already present; leaving as-is")
            return 0
    run(["gh", "pr", "comment", pr, "--body", body])
    print(f"linked {len(pages)} pages")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
