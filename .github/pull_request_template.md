## Summary

-

## Test plan

- [ ] `pytest tests/` green locally (pre-push hook enforces)
- [ ] Local review run for non-trivial diffs (`coderabbit review --committed --base main -c CLAUDE.md`)

## Gate (mechanical -- for reference)

- Required checks: pytest, gitleaks, ruff, markdownlint, pip-audit
- Server review read fully, threads resolved, THEN `gh pr merge --auto --squash`
