---
name: shared-resources
description: "How resource sharing works in this Hermes Orchestrator workspace — skills, plugins, CLI tools, MCP servers, API keys, webhooks, memories, and provider logins are ALL shared between every agent in the fleet. Use this whenever you install a tool or dependency, create a skill/plugin/webhook/MCP server, wonder where to save something so other agents can use it, or something you or another agent made is not visible where you expected."
version: 1.0.0
author: Hermes Orchestrator
license: MIT
platforms: [linux]
metadata:
  hermes:
    tags: [workspace, shared, fleet, skills, plugins, cli, mcp, webhooks, memory, orchestrator]
    related_skills: [cli-tools, hermes-agent]
---

# Shared Resources — this workspace shares everything between agents

You are one agent in a **fleet** managed by Hermes Orchestrator. The fleet has a
single shared resource layer: anything reusable that you create or install in
the right place becomes available to **every other agent automatically** — and
anything they create becomes available to you. You never need to copy files
between agents or "send" a resource to another agent.

## Where to put things so they are shared

Your own `$HERMES_HOME` directories ARE the shared layer (they are symlinks
into the workspace's shared storage), so you always work with normal local
paths:

| To share a…                | Save it to…                                            | Visible to other agents |
|----------------------------|--------------------------------------------------------|--------------------------|
| **Skill**                  | `$HERMES_HOME/skills/<category>/<skill-name>/SKILL.md` | instantly (running agents auto-reload) |
| **Plugin**                 | `$HERMES_HOME/plugins/<plugin-name>/`                  | instantly (running agents auto-reload) |
| **CLI tool (executable)**  | `$HERMES_HOME/bin/<tool>` — it is on every agent's PATH | instantly |
| **CLI tool (manifest)**    | `$HERMES_HOME/clis/<tool>.md` — see the `cli-tools` skill | instantly |
| **Long-term memory**       | `$HERMES_HOME/memories/`                               | instantly |
| **Webhook route**          | `$HERMES_HOME/webhook_subscriptions.json` (normal hermes webhook commands) | instantly |
| **MCP server**             | `config.yaml` → `mcp_servers:` (normal hermes config)  | within ~10 seconds |
| **API key / env var**      | `$HERMES_HOME/.env`                                    | within ~10 seconds |
| **Model / provider choice**| `config.yaml` → `model:` (or the `/model` command)     | within ~10 seconds |
| **Provider login (OAuth)** | `hermes auth` (writes the shared `auth.json`)          | instantly |

The "~10 seconds" rows are synchronized by the workspace's watchdog, which
merges every agent's config edits into a canonical shared config and pushes it
to the whole fleet (per-entry merge — two agents adding different MCP servers
at the same time both land).

## Rules that make sharing work — follow these

1. **Install CLI tools into `$HERMES_HOME/bin/`, never with `pip install
   --user`, `npm install -g`, or `pipx`.** Your HOME is private to you — a
   tool installed under `~/.local` or a global npm prefix exists only for you
   and is invisible to every other agent. Download/copy the final executable
   into `$HERMES_HOME/bin/` (it is already on PATH) and write a short manifest
   to `$HERMES_HOME/clis/<tool>.md` so agents and humans can discover it (the
   `cli-tools` skill documents the manifest format).
2. **System-wide package installs (`apt install …`) are fine** — all agents
   run on the same machine, so OS packages are naturally shared. But prefer a
   self-contained binary in `$HERMES_HOME/bin/` when possible: it survives
   reinstalls and is discoverable in the workspace UI.
3. **Skills always live in a category folder**: `skills/<category>/<name>/SKILL.md`.
   Never place a skill directly under `skills/`.
4. **Never delete or overwrite a shared resource another agent may be using**
   without being asked — the shared layer is fleet-wide state, and existing
   shared files win over re-seeded copies by design.
5. **Python dependencies for a shared tool** belong inside the tool: ship a
   self-contained script (e.g. `uv run --with <pkg>` shebang, or a bundled
   venv inside `$HERMES_HOME/bin/<tool>.d/`), not a bare `pip install` into
   your private environment.

## What is NEVER shared (per-agent identity)

Channel bindings and instance identity stay private to each agent: Telegram /
WhatsApp / Discord credentials (`platforms:` and `whatsapp:` config sections),
the `dashboard:` section, and the agent's API server port/key. Do not try to
share these — the sync deliberately strips them.

## The fleet around you

- **The workspace UI** (Hermes Orchestrator) is where humans create, start,
  stop, and monitor agents, and see every shared skill, plugin, and CLI tool.
- **The fixer agent**: a dedicated maintenance agent receives automatic
  incident reports (crashes, log errors, failed health checks) about other
  agents and repairs them. If you are asked to fix another agent, remember the
  fix may belong in the shared layer so the whole fleet benefits.
- **The machine's pre-installed Hermes agent is read-only to this workspace.**
  Its skills, tools, and logins flow INTO the shared layer, but nothing is
  ever written back into its home (`~/.hermes` of the real user), its
  profiles, or its systemd units. Never modify them.

## Troubleshooting

- *"I created a tool/skill but another agent can't see it."* Check you saved
  it to the paths in the table above — especially CLI tools (must be in
  `$HERMES_HOME/bin/`, not `~/.local/bin`). Skills must contain a `SKILL.md`.
- *"An env var / MCP server I added isn't on the other agent yet."* Config
  syncs every ~10 seconds and the other agent is restarted automatically when
  its config changes; wait a moment or ask the human to press "Sync now" in
  the workspace UI.
- *"A resource disappeared or was overwritten."* Shared-layer conflicts
  resolve as "existing shared copy wins". Check the workspace UI's Shared tab
  to see the canonical state.
