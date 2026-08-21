---
type: operations guide
title: Deployment and Observability
description: Runtime deployment, scaling, health, metrics, CI, dependency audit, snapshot automation, source health monitoring, and operational caveats for the Flask service.
tags: [deployment, observability, ci, operations]
---

# Deployment and Observability

The app deploys as a Python Flask service behind Gunicorn. Redis is optional but important for shared cache and coordination across workers. NGINX, Docker Compose, and Kubernetes manifests provide scaling examples, while GitHub Actions enforce tests, audits, source-health monitoring, and nightly snapshot capture.

Configuration and credentials are documented in [Configuration and Secrets](configuration-and-secrets.md).

## Runtime entrypoints

| File | Role |
| --- | --- |
| `Procfile` | Platform shorthand: `web: gunicorn app:app`. |
| `Dockerfile` | Builds Python 3.9 slim image, installs dependencies, copies repo, switches to non-root `appuser`, exposes `8080`, and starts `gunicorn -c gunicorn.conf.py app:app`. |
| `gunicorn.conf.py` | Production process settings: `gthread`, 2 workers, 4 threads, 120-second timeout, `preload_app = True`, stdout logs, `/dev/shm` worker temp directory. |
| `app.py` | Flask app object and all route registration. |

Because Gunicorn preloads the app, background threads are intentionally started from `app._ensure_warmer_running()` after fork, not during import.

## Local and container topology

`docker-compose.yml` defines:

- `redis`: `redis:7-alpine`, persistent volume, healthcheck.
- `app`: built from the local Dockerfile, `REDIS_URL=redis://redis:6379/0`, `PORT=8080`, healthcheck against `/health`, exposes 8080 only inside the compose network.
- `nginx`: `nginx:alpine`, ports 80/443, mounts `nginx.conf`, proxies to app.
- `prometheus`: optional metrics collection service, but the referenced `prometheus.yml` is not present in the repository inventory, so treat it as a deployment-local requirement.

`docker-compose.scale.yml` adds an example 3-replica app deployment and cAdvisor. It references `nginx.scale.conf`, which is not present in this repository, so use it as a guide rather than an immediately runnable compose overlay unless that file is supplied.

## NGINX behavior

`nginx.conf` defines:

- `least_conn` upstream to `app:8080`.
- `/health` bypasses rate limiting.
- `/api/` is limited by `limit_req zone=api burst=20 nodelay` and has 60-second proxy read/send timeouts.
- Static assets receive one-year immutable caching.
- `/` uses the `general` limit zone with `burst=50`.
- `/metrics` is restricted to loopback and private network ranges.
- Security headers mirror core browser hardening in Flask.

## Kubernetes manifest

`k8s-deployment.yaml` contains:

- `Deployment` named `yahoo-losers-webapp`, 2 initial replicas, container port 8080, resource requests `100Mi`/`100m`, limits `200Mi`/`500m`.
- HTTP readiness probe on `/health` and liveness probe on `/health`.
- `Service` named `yahoo-losers-service` of type `LoadBalancer`.
- `HorizontalPodAutoscaler` from 2 to 10 replicas, targeting 70 percent CPU and 80 percent memory.
- Redis Deployment and Redis Service named `redis-service`.

The app reports cache backend and memory in `/health`, so Kubernetes liveness/readiness can observe resource stress without requiring providers to be warm.

## Health and metrics endpoints

| Endpoint | Purpose | Operational interpretation |
| --- | --- | --- |
| `/health` | Liveness and basic resource status. | Empty cache is normal and not unhealthy. Memory percent above 90 changes status to unhealthy. Includes page cache backend, market-data backend, and entry count. |
| `/health/sources` | Provider reachability and yfinance rate-limit signal. | Returns `207` when one or more sources are degraded. Used by scheduled health-watch. |
| `/metrics` | Memory and page-cache state. | Restricted by NGINX to private network ranges. |
| `/refresh` | Manual page and market-data cache clear. | Clears Redis/file rendered cache and `market_data` cache; next request rebuilds. |
| `/api/client-error` | Browser error telemetry. | Stores last 50 entries in-process only. |
| `/api/client-errors` | Read browser error buffer. | Non-durable, per-worker view. |
| `/api/tasks/start/<symbol>` and `/api/tasks/status/<task_id>` | Celery task scaffolding. | Requires Redis broker to work usefully; current task bodies return placeholder pending payloads. |

## GitHub Actions

| Workflow | Purpose |
| --- | --- |
| `.github/workflows/tests.yml` | Installs runtime and dev requirements, runs `python -m pytest tests/ -q`, and greps for known fabricated fallback patterns and bare `except:` in `app.py`. |
| `.github/workflows/audit.yml` | Runs `pip-audit` on the resolved runtime environment on PRs, pushes to main, and weekly. |
| `.github/workflows/gitleaks.yml` | Secret scanning gate over full git history; checkout uses `fetch-depth: 0`, and gitleaks uses the GitHub token. |
| `.github/workflows/lint.yml` | Lint/format gate. |
| `.github/workflows/snapshot.yml` | Calls `/api/snapshot`, validates nonempty scored snapshot, commits `data/snapshots/<date>.json`, and posts a daily digest issue with top scores and paper events. |
| `.github/workflows/healthwatch.yml` | Probes `/health/sources` every 30 minutes, opens a provider-degraded issue, and closes it when healthy. |
| `.github/workflows/openwiki-update.yml` | Wiki maintenance automation. |

## Operational failure modes

- Redis unavailable: page cache falls back to file cache, market-data cache falls back to memory, worker coordination is weaker, and provider calls may be duplicated across workers.
- Provider rate limits: Yahoo `.info` backs off through the adaptive info lane; options endpoint uses a shared cooldown; FMP calls are hard-budgeted.
- Warmers disabled: tests set `MARKET_DATA_DISABLE_WARMER=1`; production without warmers will serve more cold `—` values and slower detail fetches.
- Missing provider keys: optional features return unavailable states instead of guessed data.
- Source health degradation: `/health/sources` and the health-watch workflow surface degraded upstreams before the dashboard silently loses factors.

## Validation commands

Typical local checks:

```bash
python -m pytest tests/ -q
python -m pytest tests/test_sources.py tests/test_freshness.py -q
python -m pytest tests/test_live_gate.py -q
```

Container smoke check:

```bash
docker build -t yahoo-losers-webapp .
container_id=$(docker run -d --rm -p 8080:8080 yahoo-losers-webapp)
trap 'docker stop "$container_id" >/dev/null' EXIT
curl --fail --retry 10 --retry-delay 1 http://localhost:8080/health
```
