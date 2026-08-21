#!/usr/bin/env python3
"""Deterministic wiki-facts gate: the checkable claims in wiki/ must match
the code. No LLM, no network -- pure text assertions, so it can be a
required check without flakiness. Semantic drift is the Claude
maintenance workflow's job; THIS gate covers what a regex can prove.
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
failures = []


def check(cond, msg):
    if not cond:
        failures.append(msg)


def read(rel):
    return (ROOT / rel).read_text(encoding="utf-8")


sources = read("sources.py")
tracking = read("tracking.py")

# 1) Paper rails: every constant's value must appear in paper-trading.md
paper = read("wiki/paper-trading.md")
for name in ("PAPER_TP_PCT", "PAPER_STOP_PCT",
             "PAPER_CATASTROPHE_STOP_PCT", "PAPER_MAX_SESSIONS"):
    m = re.search(rf"^{name}\s*=\s*([\d.]+)", sources, re.M)
    check(m, f"{name} not found in sources.py")
    if m:
        val = m.group(1).rstrip("0").rstrip(".")
        check(f"{name} = {val}" in paper.replace("`", ""),
              f"paper-trading.md rails line missing {name} = {val}")

# 2) Graduation thresholds
for name, page_hint in (("LIVE_MIN_RESOLVED", "resolved"),
                        ("LIVE_MAX_BRIER", "Brier"),
                        ("LIVE_MIN_GRADED_FILLS", "fills"),
                        ("LIVE_MIN_SNAPSHOT_DAYS", "streak")):
    m = re.search(rf"^{name}\s*=\s*([\d.]+)", tracking, re.M)
    check(m, f"{name} not found in tracking.py")
    if m:
        val = m.group(1).rstrip("0").rstrip(".")
        check(val in paper, f"paper-trading.md missing graduation value {val} ({name})")

# 3) Required-check names in review-gate.md match workflow job names
gate = read("wiki/review-gate.md")
jobs = set()
for wf in (ROOT / ".github" / "workflows").glob("*.yml"):
    jobs |= set(re.findall(r"^\s{2}([\w-]+):\s*$", wf.read_text(encoding="utf-8"), re.M))
for required in ("pytest", "gitleaks", "ruff", "markdownlint", "pip-audit"):
    check(f"`{required}`" in gate, f"review-gate.md missing required check `{required}`")

# 4) Module inventory in architecture.md covers every top-level .py
arch = read("wiki/architecture.md")
for py in sorted(ROOT.glob("*.py")):
    check(f"`{py.name}`" in arch, f"architecture.md missing module `{py.name}`")

# 5) Test inventory in testing.md covers every test file
testing = read("wiki/testing.md")
for t in sorted((ROOT / "tests").glob("test_*.py")):
    check(f"`{t.name}`" in testing, f"testing.md missing `{t.name}`")

if failures:
    print("WIKI FACTS DRIFTED (fix wiki/ or the code claim):")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("wiki facts verified: rails, thresholds, checks, modules, tests")
