# Security

Hermes Orchestrator manages AI agents that hold real credentials and can run
real commands, so its security posture is deliberately conservative. This page
describes what the workspace does — and does not — expose.

## Network exposure

- Everything binds to **loopback** (`127.0.0.1`) by default: the workspace UI
  (`:9100`), every agent's API server, and every agent's dashboard. Nothing is
  reachable from the network unless you put a reverse proxy in front.
- In domain mode, the only public entry point is your reverse proxy → `:9100`.
  Agent dashboards are reached **through** the workspace proxy, never
  directly.

## Authentication

- Setting `WORKSPACE_PASSWORD` (or `WORKSPACE_PASSWORD_FILE`) enables a
  single sign-in that covers the workspace **and** every agent dashboard: an
  HMAC-signed, expiring session cookie issued for the parent domain. Scripts
  authenticate with `Authorization: Bearer <password>` or HTTP Basic.
- Each agent's dashboard additionally uses Hermes's own per-process session
  token, injected only into the served SPA.

## What the proxy blocks

- The agent dashboards' **Files** section (a web file manager over the
  agent's filesystem) is disabled when served through the workspace: its API
  returns 403 server-side and the tab is removed client-side.
- `Origin`/`Host` are rewritten so the dashboards' DNS-rebinding protection
  stays effective behind the proxy.

## Secrets on disk and in git

- Provider OAuth tokens (`auth.json`), agent API keys (`registry.json`), the
  session-cookie signing secret (`auth_secret`), `.env` files, agent homes,
  and the whole shared layer are **git-ignored** — a clone of this repository
  never contains credentials.
- The webhook subscriptions file (per-route HMAC secrets) is written with
  mode `0600`.

## Blast-radius containment

- Workspace-created agents run with `HOME` confined to their own agent
  directory, so Hermes's user-scope self-management (including systemd unit
  self-healing) cannot touch anything outside the workspace.
- Pre-existing Hermes installs are adopted **read-only**: the workspace never
  writes into their homes, configs, or profiles, and a watchdog guard
  restores their systemd unit files if any process rewrites them to point at
  the wrong home.
- Channel credentials (Telegram, WhatsApp, …) are never shared between
  agents; the sync strips per-agent identity sections by design.

## Reporting a vulnerability

Please open a GitHub security advisory (Security → Advisories → Report a
vulnerability) or a private report rather than a public issue. Include
reproduction steps and affected versions; you can expect an initial response
within a week.
