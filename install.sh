#!/usr/bin/env bash
#
# Hermes Orchestrator — one-command installer.
#
#   curl -fsSL https://raw.githubusercontent.com/EssadikElmangoug/hermes-orchestrator/main/install.sh | bash
#
# What it does (idempotent — safe to re-run for updates):
#   1. Installs Hermes Agent via its official installer if `hermes` is missing.
#   2. Clones (or updates) this repository to ~/hermes-orchestrator.
#   3. Creates a private Python virtualenv and installs the few dependencies.
#   4. Registers a systemd user service and starts the workspace.
#
# Options via environment variables:
#   HERMES_ORCH_HOME=/path   install location   (default: ~/hermes-orchestrator)
#   HERMES_ORCH_REPO=<url>   repository to clone (default: official repo)
#   HERMES_ORCH_NO_SERVICE=1 skip the systemd service; print the run command
#
set -euo pipefail

main() {
  REPO_URL="${HERMES_ORCH_REPO:-https://github.com/EssadikElmangoug/hermes-orchestrator}"
  DEST="${HERMES_ORCH_HOME:-$HOME/hermes-orchestrator}"
  VENV="$DEST/.venv"
  PORT=9100

  say()  { printf '\033[1;33m[hermes-orchestrator]\033[0m %s\n' "$*"; }
  die()  { printf '\033[1;31m[hermes-orchestrator] ERROR:\033[0m %s\n' "$*" >&2; exit 1; }

  command -v git >/dev/null 2>&1 || die "git is required. Install it (e.g. 'apt install git') and re-run."
  command -v python3 >/dev/null 2>&1 || die "python3 is required. Install Python 3.10+ and re-run."
  command -v curl >/dev/null 2>&1 || die "curl is required."

  # ── 1. Hermes Agent ────────────────────────────────────────────────────
  if ! command -v hermes >/dev/null 2>&1; then
    say "Hermes Agent not found — running its official installer first…"
    curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash
    # the hermes installer puts the binary in one of these
    export PATH="$HOME/.local/bin:/usr/local/bin:$PATH"
    command -v hermes >/dev/null 2>&1 \
      || die "hermes is installed but not on PATH yet — open a new shell and re-run this installer."
    say "Hermes Agent installed."
  else
    say "Found existing Hermes Agent: $(command -v hermes)"
  fi

  # ── 2. Get the code ────────────────────────────────────────────────────
  if [ -d "$DEST/.git" ]; then
    say "Updating existing checkout at $DEST…"
    git -C "$DEST" pull --ff-only || say "Could not fast-forward (local changes?) — keeping your version."
  else
    say "Cloning into $DEST…"
    git clone --depth 1 "$REPO_URL" "$DEST"
  fi

  # ── 3. Python environment ──────────────────────────────────────────────
  # uv is preferred: a small static binary that needs no system packages
  # (python3 -m venv fails on stock Debian/Ubuntu without python3-venv).
  UV=""
  for cand in uv "$HOME/.local/bin/uv" "$HOME/.cargo/bin/uv"; do
    if command -v "$cand" >/dev/null 2>&1; then UV="$cand"; break; fi
  done
  if [ -z "$UV" ]; then
    say "Installing uv (Python environment manager)…"
    curl -LsSf https://astral.sh/uv/install.sh | sh >/dev/null 2>&1 || true
    for cand in "$HOME/.local/bin/uv" "$HOME/.cargo/bin/uv"; do
      [ -x "$cand" ] && UV="$cand" && break
    done
  fi
  if [ ! -x "$VENV/bin/python" ]; then
    say "Creating virtualenv…"
    if [ -n "$UV" ]; then
      "$UV" venv --quiet "$VENV"
    else
      python3 -m venv "$VENV" \
        || die "Could not create a virtualenv — install your distro's python3-venv package (e.g. 'apt install python3-venv') and re-run."
    fi
  fi
  say "Installing Python dependencies…"
  if [ -n "$UV" ]; then
    "$UV" pip install --quiet --python "$VENV/bin/python" \
      fastapi uvicorn pydantic pyyaml httpx websockets
  else
    "$VENV/bin/pip" install --quiet --upgrade \
      fastapi uvicorn pydantic pyyaml httpx websockets
  fi

  # ── 4. Service ─────────────────────────────────────────────────────────
  RUN_CMD="$VENV/bin/python $DEST/workspace/server.py"
  if [ -z "${HERMES_ORCH_NO_SERVICE:-}" ] \
     && command -v systemctl >/dev/null 2>&1 \
     && systemctl --user show-environment >/dev/null 2>&1; then
    UNIT_DIR="$HOME/.config/systemd/user"
    UNIT="$UNIT_DIR/hermes-orchestrator.service"
    mkdir -p "$UNIT_DIR"
    if [ -f "$UNIT" ]; then
      say "Service already exists — keeping your configuration and restarting it."
    else
      cat > "$UNIT" <<EOF
[Unit]
Description=Hermes Orchestrator Workspace (agents fleet UI on 127.0.0.1:$PORT)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=$RUN_CMD
WorkingDirectory=$DEST/workspace
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=default.target
EOF
    fi
    systemctl --user daemon-reload
    systemctl --user enable --now hermes-orchestrator.service
    # 'enable --now' does NOT restart an already-running service, which would
    # leave a pre-update process serving old code after a 'git pull' update.
    systemctl --user restart hermes-orchestrator.service
    # keep the user service alive after logout on headless servers
    loginctl enable-linger "$USER" >/dev/null 2>&1 || true
    say "Service started."
  else
    say "systemd user services unavailable (or skipped) — start the workspace with:"
    echo
    echo "    $RUN_CMD"
    echo
  fi

  echo
  say "Done! Open the workspace at:  http://127.0.0.1:$PORT"
  say "Next steps:"
  say "  • If this is a fresh Hermes install, run 'hermes' once to sign in to an AI provider."
  say "  • Existing Hermes agents on this machine are adopted automatically (read-only)."
  say "  • Re-run this installer any time to update."
}

main "$@"
