# hermes-workflow

Runs the workspace's saved workflows by name. Use it whenever someone asks you to run, execute, trigger, or start "a workflow" (for example "run my facebook workflow") — resolve the name with `hermes-workflow list`, then execute it with `hermes-workflow run`. Workflows are shared fleet-wide, so every agent sees the same list. The full guidance is in the `workflows` skill (workspace category).

## Commands

```
hermes-workflow list                             # every workflow: name, steps, last run
hermes-workflow list --json                      # same, machine-readable
hermes-workflow show facebook                    # one workflow's steps and triggers
hermes-workflow run facebook                     # run it, wait, print the final output
hermes-workflow run facebook --input "post about our new clinic hours"
hermes-workflow run facebook --no-wait           # start it and return immediately
hermes-workflow run facebook --timeout 600       # wait longer than the 300s default
hermes-workflow status run-facebook-1753...      # check a run started earlier
```

## Notes

- The workflow name is matched loosely: exact id or name first, then substring,
  then a close fuzzy match. "facebook" finds a workflow called "Facebook Post".
  If the phrase is ambiguous the command lists the candidates instead of guessing.
- `run` exits non-zero and prints the failing step when a workflow fails, so you
  can report the real reason back to the user.
- Authentication is automatic (the workspace agent token); no setup is needed.
