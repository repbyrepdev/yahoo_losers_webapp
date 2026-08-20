#!/usr/bin/env bash
# Review-budget dashboard: how hard are we leaning on each reviewer?
# CR has no quota API -- its consumption is inferred from our own activity
# (PRs opened + summons in the trailing 7 days; its own limit notices are the
# ground truth when present). Copilot reads the org's live billing meter.
set -euo pipefail
REPO="repbyrepdev/yahoo_losers_webapp"
ORG="FCP-Euro-Pricing-Team"
SINCE=$(python3 -c "from datetime import date, timedelta; print((date.today()-timedelta(days=7)).isoformat())")
MONTH=$(date -u +%Y-%m)

echo "== CodeRabbit (Pro Plus; allowance shrinks with 7-day attempts) =="
PRS=$(gh pr list --repo "$REPO" --state all --search "created:>=$SINCE" --json number --jq 'length')
echo "  PRs opened (7d, each = 1 CR attempt; summons add more): $PRS"
echo "  latest CR limit notice (authoritative when present):"
gh api "search/issues" -X GET -f q="repo:$REPO commenter:app/coderabbitai created:>=$SINCE" --jq '.items[0].html_url // "none surfaced via search"' 2>/dev/null | sed 's/^/    /'
echo "  rule: ≤1 summon per PR per hour; silent polling otherwise"

echo "== Copilot (Enterprise seat, org-pooled AI credits; \$0.01/credit) =="
gh api "/organizations/$ORG/settings/billing/usage" --jq "
  [.usageItems[] | select(.product == \"copilot\") | select(.date | startswith(\"$MONTH\"))] |
  if length == 0 then \"  no metered copilot usage recorded for $MONTH yet\"
  else (group_by(.sku)[] | \"  \(.[0].sku): qty \(map(.quantity) | add) | gross \$\(map(.grossAmount) | add | .*100 | round / 100) | net \$\(map(.netAmount) | add | .*100 | round / 100)\") end"

echo "== Gemini =="
echo "  CLI blocked (individual tier retired); needs Damien's re-auth with the"
echo "  work Google account (converted Pro seat) or a GEMINI_API_KEY."
echo "  Local invocation count this session: tracked in session logs only."
