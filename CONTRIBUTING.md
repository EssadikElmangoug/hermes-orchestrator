# Contributing

Thanks for your interest! Hermes Orchestrator is a small, focused codebase —
most of it lives in four files — so getting productive takes minutes, not
days.

## Getting started

```bash
git clone https://github.com/EssadikElmangoug/hermes-orchestrator
cd hermes-orchestrator
python3 -m venv .venv && .venv/bin/pip install fastapi uvicorn pydantic pyyaml httpx websockets pytest
.venv/bin/python workspace/server.py     # UI on http://127.0.0.1:9100
.venv/bin/pytest workspace/tests -q      # run the tests
```

You'll want [Hermes Agent](https://hermes-agent.nousresearch.com) installed to
exercise real agents, but the test suite runs without it.

## Code map

| File | What it is |
|---|---|
| `workspace/orchestrator.py` | All fleet logic: registry, agent lifecycle, the shared-resources sync, watchdog, incidents/fixer dispatch |
| `workspace/server.py` | FastAPI app: REST API + static UI + bootstrap |
| `workspace/proxy.py` | Dashboard reverse proxy: subdomain routing, HTML injection, model-options cache |
| `workspace/auth.py` | Cookie SSO gate (one login for workspace + all dashboards) |
| `workspace/static/` | The UI (vanilla JS) and `dash_inject.js` (dashboard enhancements) |
| `workspace/seed_skills/` | Skills seeded into the shared layer so agents understand the workspace |
| `install.sh` | The curl-able installer |

Read [docs/architecture.md](docs/architecture.md) for how the pieces fit.

## The rules that matter

1. **Never write into an adopted install.** Pre-existing Hermes homes,
   configs, profiles, and systemd units are read-only to the workspace. Every
   feature must preserve this — it is the project's core promise.
2. **Merges must match resource granularity.** Skills merge per skill folder
   (recursively), config sections per entry, webhooks per route. A
   coarser-grained merge silently loses someone's work; there are tests for
   these — extend them if you touch the sync.
3. **Dashboard changes go through the proxy injection layer**
   (`dash_inject.js` + `/workspace-api/*` endpoints), never by patching the
   installed Hermes package.
4. **No new heavyweight dependencies** without discussion — the whole server
   is six pip packages.

## Pull requests

- Keep PRs focused; describe the behavior change, not just the code change.
- Add or extend a test when you touch merge/sync logic.
- `bash -n install.sh` (or shellcheck) must pass if you touch the installer.
