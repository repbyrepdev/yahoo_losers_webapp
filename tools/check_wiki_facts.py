#!/usr/bin/env python3
"""Deterministic docs-facts gate: the checkable claims in the documentation
must match the code. No LLM, no network -- pure text assertions, so it can
be a required check without flakiness.

Re-anchored (one-home-per-fact consolidation): fact assertions now point
at the GENERATED openwiki/ pages -- so this gate also polices the
generator's output -- plus the authored docs/ judgment pages and the
README front door. Containment checks are loose (name AND value appear
in the page) so innocent regeneration phrasing cannot red the gate.
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
    p = ROOT / rel
    return p.read_text(encoding="utf-8") if p.exists() else ""


sources = read("sources.py")
tracking = read("tracking.py")
trading_page = read("openwiki/trading/paper-trading-and-live-gate.md")
doctrine = read("docs/doctrine.md")
review = read("docs/review-gate.md")
readme = read("README.md")

check(trading_page, "openwiki trading page missing")
check(doctrine and review, "authored docs pages missing")

# 1) Paper rails: constant name AND its current value both appear in the
#    generated trading page (format-independent containment).
for name in ("PAPER_TP_PCT", "PAPER_STOP_PCT",
             "PAPER_CATASTROPHE_STOP_PCT", "PAPER_MAX_SESSIONS"):
    m = re.search(rf"^{name}\s*=\s*([\d.]+)", sources, re.M)
    check(m, f"{name} not found in sources.py")
    if m:
        val = m.group(1).rstrip("0").rstrip(".")
        check(name in trading_page,
              f"openwiki trading page missing constant name {name}")
        check(val in trading_page,
              f"openwiki trading page missing value {val} for {name}")

# 2) Graduation thresholds: values present in the generated trading page
#    AND the authored doctrine rationale.
for name in ("LIVE_MIN_RESOLVED", "LIVE_MAX_BRIER",
             "LIVE_MIN_GRADED_FILLS", "LIVE_MIN_SNAPSHOT_DAYS"):
    m = re.search(rf"^{name}\s*=\s*([\d.]+)", tracking, re.M)
    check(m, f"{name} not found in tracking.py")
    if m:
        val = m.group(1).rstrip("0").rstrip(".")
        check(val in trading_page,
              f"openwiki trading page missing graduation value {val} ({name})")
        check(val in doctrine,
              f"docs/doctrine.md missing graduation value {val} ({name})")

# 3) Required checks: every workflow-defined gate job is named in
#    CLAUDE.md's list (workflows are the ground truth for names).
claude_md = read("CLAUDE.md")
for required in ("pytest", "gitleaks", "ruff", "markdownlint", "pip-audit",
                 "wiki-facts"):
    check(f"`{required}`" in claude_md,
          f"CLAUDE.md required-checks list missing `{required}`")

# 4) The README stays a front door: no volatile provider-chain tables.
check("| Feed |" not in readme and "1st |" not in readme,
      "README has grown a provider-chain table again -- chains live in openwiki/")
check(len(readme.splitlines()) < 120,
      f"README is {len(readme.splitlines())} lines -- front doors stay under 120")

if failures:
    print("DOCS FACTS DRIFTED (fix the docs or the code claim):")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("docs facts verified: rails, thresholds, checks, README discipline")
