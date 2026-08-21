# Operations

## Deploy path

Push to `main` → Render builds and deploys the web service
(`yahoo-losers-webapp.onrender.com`, srv-d2sreo63jp1c73b5th80). Python is
pinned by `.python-version` (3.13.x); CI matches. Deploys are watched by
SHA via the Render API, then live pages spot-verified (at most two spaced
GETs — never refresh loops).

## Scheduled work (GitHub Actions)

| Workflow | Schedule | Job |
|---|---|---|
| `snapshot.yml` | 01:15 UTC Tue–Sat (Mon–Fri evenings ET; snapshot ids use the Eastern trading date) | Nightly snapshot + paper lifecycle + Google Chat digest (entries, fills, exits, holdings) |
| `audit.yml` | Mondays 12:00 UTC + every PR | pip-audit CVE gate |
| `healthwatch.yml` | periodic | Uptime / health probe |
| `tests.yml`, `lint.yml`, `gitleaks.yml` | every PR + main | The merge gate |

The snapshot job runs the Python directly (not through the site), so the
site being slow/cold never corrupts the record.

## Secrets

macOS Keychain locally (`security find-generic-password -s <SVC> -w`),
Render env vars in production, GitHub Actions secrets for CI. Never
`.env` files. Alpaca is pinned to the paper API base unless the live
gate passes (see [paper-trading](paper-trading.md)).

## Monitoring surfaces

- `/health` (open), `/health/sources` (provider probe board)
- `/metrics` — cache hit rates, provider budgets
- Morning digest in Google Chat = the daily blotter
- The weekly pip-audit cron run fails red when a new CVE publishes
