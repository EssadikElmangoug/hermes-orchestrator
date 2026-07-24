---
name: workflows
description: "USE THIS whenever the user asks to run, execute, trigger, start, or kick off a WORKFLOW by name — 'run my facebook workflow', 'execute the newsletter flow', 'trigger my daily report', 'kick off my X workflow'. The word 'workflow' is the deciding signal and TAKES PRECEDENCE over any topic skill whose subject happens to match the name: 'my facebook workflow' means the saved workflow called facebook, NOT a Facebook content/reel skill. Run it with the hermes-workflow CLI; never improvise the steps yourself. Also use it to list what workflows exist, check whether a run finished, or explain why one failed."
version: 1.0.0
author: Hermes Orchestrator
license: MIT
platforms: [linux]
metadata:
  hermes:
    tags: [workspace, workflows, automation, orchestrator, run, trigger, pipeline]
    related_skills: [shared-resources, cli-tools, hermes-agent]
---

# Workflows — run the workspace's saved automations by name

This machine runs **Hermes Orchestrator**, which stores named *workflows*: saved
graphs of agent steps, approvals, and outputs that the user builds on a canvas.
Workflows are shared across the whole fleet — every agent sees the same list, so
you can run any of them no matter which agent or channel you are.

You run them with the **`hermes-workflow`** CLI, already on your PATH.

## When to use this

Use it whenever a user refers to a workflow by name and wants it executed:

> "run my facebook workflow" · "execute the newsletter flow" · "kick off my
> daily report" · "trigger the onboarding automation" · "start my facebook one
> with this text"

Also use it when they ask **what** workflows exist, **whether** one finished, or
**why** one failed.

Do **not** hand-simulate a workflow. If the user names one, run the real thing —
its steps may post to channels, call webhooks, or require approvals that you
cannot reproduce by improvising.

## The three commands you need

```
hermes-workflow list                  # what exists (name, steps, last run)
hermes-workflow run <name>            # run it, wait, print the final output
hermes-workflow status <run-id>       # check something started earlier
```

### Running one

```
hermes-workflow run facebook
hermes-workflow run facebook --input "post about our new clinic hours"
```

Pass whatever content the user supplied with `--input` — it becomes the
workflow's trigger payload, which the first step receives as its input. If they
just said "run my facebook workflow" with no extra detail, omit `--input`.

`run` **waits** for the workflow to finish (up to 300s) and prints the final
output, which is what you report back to the user. Use `--timeout 600` for long
ones, or `--no-wait` when the user only wants it started.

## How to handle the name

You do not need the exact name — matching is loose (exact → substring → fuzzy),
so `facebook` finds a workflow called "Facebook Post". So:

1. Take the user's phrase and pass it straight to `hermes-workflow run`.
2. If it reports the name is **ambiguous**, it lists the candidates — ask the
   user which one they meant, then re-run with the fuller name.
3. If it reports **no match**, run `hermes-workflow list` and show the user what
   actually exists rather than guessing.

## Reporting the result

- **Success** — the command prints the final output. Relay that to the user in
  your own voice; don't dump raw CLI noise at them.
- **Failure** — it exits non-zero and names the step that failed and why. Tell
  the user which step broke and the reason. Don't retry blindly; a failed step
  usually means a missing credential, an unreachable service, or a bad input.
- **Still running** — it prints the run id. Tell the user it's running and that
  you can check on it; use `hermes-workflow status <run-id>` when they ask.
- **Waiting for approval** — the workflow has a human-approval gate. Tell the
  user it is paused and needs their approval in the workspace UI.

## Good to know

- Workflows are **created and edited** in the workspace UI (the workflow canvas,
  or by chatting with the `workflow-builder` agent) — not from this CLI. If the
  user wants a *new* workflow, point them there rather than trying to build one.
- A workflow's steps run on specific agents in the fleet, which may not be you.
  That's fine and expected — you are triggering it, not necessarily executing it.
- Runs are visible in the workspace UI, so the user can watch progress live.
