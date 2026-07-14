"""Workspace orchestrator — manages a fleet of independent Hermes Agent
installations ("agents"/gateways), each with its own HERMES_HOME under
agents/<name>/.hermes and its own API server port.

Registry, process supervision, shared-config seeding, subagent (Hermes
profile) management, incident capture and Fixer dispatch all live here.
server.py exposes this over HTTP for the workspace UI.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

ROOT = Path(__file__).resolve().parent.parent          # hermes-orchestrator/
WORKSPACE = ROOT / "workspace"
AGENTS_DIR = ROOT / "agents"
SHARED_DIR = WORKSPACE / "shared"
REGISTRY_PATH = WORKSPACE / "registry.json"
INCIDENTS_PATH = WORKSPACE / "incidents.json"


def _find_hermes() -> Path:
    """Prefer a workspace-bundled venv, else the machine's installed hermes."""
    bundled = ROOT / "hermes-venv" / "bin" / "hermes"
    if bundled.exists():
        return bundled
    found = shutil.which("hermes")
    if found:
        return Path(found).resolve()
    return Path("/usr/local/lib/hermes-agent/venv/bin/hermes")


HERMES_BIN = _find_hermes()
VENV_BIN = HERMES_BIN.parent

API_PORT_BASE = 9301
DASH_PORT_BASE = 9401
WORKSPACE_PORT = 9100

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")

# Platform credentials that must stay per-agent: inheriting them would make
# every gateway fight over the same bot/channel identity.
_ENV_EXCLUDE_PREFIXES = (
    "TELEGRAM_", "DISCORD_", "SLACK_", "WHATSAPP_", "SIGNAL_", "MATRIX_",
    "TEAMS_", "GOOGLE_CHAT_", "EMAIL_", "IMESSAGE_", "SMS_", "QQ_", "WEIXIN_",
    "API_SERVER_",
)

_LOG_ERROR_RE = re.compile(r"ERROR|Traceback \(most recent call last\)|CRITICAL")

_lock = threading.RLock()
_procs: Dict[str, subprocess.Popen] = {}          # gateway processes
_dash_procs: Dict[str, subprocess.Popen] = {}     # per-agent hermes dashboards
_log_offsets: Dict[str, int] = {}                 # incremental error-log scan
_health_fails: Dict[str, int] = {}


# ─── registry ────────────────────────────────────────────────────────────────

def _load(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def _save(path: Path, data: Any) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(path)


def load_registry() -> Dict[str, Any]:
    return _load(REGISTRY_PATH, {"agents": {}})


def save_registry(reg: Dict[str, Any]) -> None:
    _save(REGISTRY_PATH, reg)


def _next_port(reg: Dict[str, Any], key: str, base: int) -> int:
    used = {a.get(key) for a in reg["agents"].values() if a.get(key)}
    port = base
    while port in used:
        port += 1
    return port


# ─── installed-gateway discovery (adopted, read-only) ───────────────────────
#
# Machines that already run Hermes (systemd units like hermes-gateway.service
# and hermes-gateway-<profile>.service) get those gateways ADOPTED into the
# workspace: they appear in the agent list, graph and dashboards, can be
# started/stopped through their own systemd unit, and their config seeds the
# shared layer — but the workspace NEVER writes into their homes, .env,
# config, memory or profiles. Breaking an existing install is not an option.

_UNIT_RE = re.compile(r"^(hermes-gateway(?:-([a-z0-9][a-z0-9-]*))?)\.service$")


def _truthy(value: str) -> bool:
    return value.strip().lower() in ("1", "true", "yes", "on")


def _read_env_keys(env_path: Path) -> Dict[str, str]:
    out: Dict[str, str] = {}
    try:
        text = env_path.read_text()
    except OSError:
        return out
    for line in text.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key, val = stripped.split("=", 1)
            out[key.strip()] = val.strip().strip('"').strip("'")
    return out


def _detect_api_server(home: Path) -> tuple:
    """(port, key) of an installed agent's OpenAI-compatible API server, or
    (None, "") when it doesn't expose one. Read-only: nothing is enabled."""
    env = _read_env_keys(home / ".env")
    if not _truthy(env.get("API_SERVER_ENABLED", "")):
        return None, ""
    try:
        port = int(env.get("API_SERVER_PORT", "") or 8642)
    except ValueError:
        port = 8642
    return port, env.get("API_SERVER_KEY", "")


def _port_open(port: int) -> bool:
    import socket
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


def _systemctl(agent: Dict[str, Any], *args: str,
               timeout: int = 60) -> subprocess.CompletedProcess:
    cmd = ["systemctl"]
    if agent.get("scope", "user") != "system":
        cmd.append("--user")
    cmd += [*args, agent.get("unit", "hermes-gateway.service")]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def _list_gateway_units() -> List[Dict[str, str]]:
    """All hermes-gateway*.service units on this machine (user then system)."""
    units: List[Dict[str, str]] = []
    for scope_flag, scope in ((["--user"], "user"), ([], "system")):
        r = subprocess.run(
            ["systemctl", *scope_flag, "list-unit-files",
             "hermes-gateway*.service", "--no-legend", "--plain"],
            capture_output=True, text=True)
        if r.returncode != 0:
            continue
        for line in r.stdout.splitlines():
            fields = line.split()
            m = _UNIT_RE.match(fields[0]) if fields else None
            if m and not any(u["unit"] == m.group(0) for u in units):
                units.append({"unit": m.group(0), "scope": scope,
                              "profile": m.group(2) or ""})
    return units


def _unit_home(unit: Dict[str, str]) -> Path:
    """HERMES_HOME a unit runs against (from its Environment=, else default)."""
    scope_flag = ["--user"] if unit["scope"] == "user" else []
    r = subprocess.run(
        ["systemctl", *scope_flag, "show", "-p", "Environment", unit["unit"]],
        capture_output=True, text=True)
    m = re.search(r"HERMES_HOME=(\S+)", r.stdout or "")
    if m:
        return Path(m.group(1))
    root = Path.home() / ".hermes"
    return root / "profiles" / unit["profile"] if unit["profile"] else root


def _soul_summary(home: Path) -> str:
    try:
        for line in (home / "SOUL.md").read_text().splitlines():
            line = line.strip().lstrip("#").strip()
            if line:
                return line[:120]
    except OSError:
        pass
    return ""


def _merge_webhooks_from(home: Path) -> bool:
    """Merge webhook routes an installed agent defined into the shared
    subscriptions file (shared wins on name collisions). A plain
    copy-if-missing would only ever propagate the FIRST webhook."""
    src = home / "webhook_subscriptions.json"
    if not src.is_file():
        return False
    try:
        theirs = json.loads(src.read_text())
    except Exception:
        return False
    if not isinstance(theirs, dict):
        return False
    dst = SHARED_DIR / "webhook_subscriptions.json"
    ours = _load(dst, {})
    added = {k: v for k, v in theirs.items() if k not in ours}
    if not added:
        return False
    ours.update(added)
    tmp = dst.with_suffix(".tmp")
    tmp.write_text(json.dumps(ours, indent=2))
    tmp.chmod(0o600)                    # per-route HMAC secrets live in here
    tmp.replace(dst)
    return True


def _merge_missing(src: Path, dst: Path) -> List[str]:
    """Recursively copy entries of src that dst lacks; existing dst entries
    always win and are never modified. Descends into directories present on
    both sides, so an item created inside an existing subtree still
    propagates — skills live at skills/<category>/<skill>/, and a new skill
    in an already-shared category is invisible to a single-level copy.
    Returns the copied paths relative to src."""
    copied: List[str] = []
    stack = [(src, dst, "")]
    while stack:
        s, d, prefix = stack.pop()
        try:
            entries = list(s.iterdir())
        except OSError:
            continue
        for item in entries:
            if item.is_symlink() and not item.exists():
                continue
            dest = d / item.name
            rel = prefix + item.name
            try:
                if item.is_dir() and not item.is_symlink():
                    if dest.is_dir():
                        stack.append((item, dest, rel + "/"))
                    elif not dest.exists():
                        shutil.copytree(item, dest, symlinks=True)
                        copied.append(rel)
                elif not dest.exists():
                    shutil.copy2(item, dest, follow_symlinks=False)
                    copied.append(rel)
            except OSError:
                pass
    return copied


def seed_shared_from_home(home: Path,
                          dirs=("skills", "memories", "bin", "clis", "plugins"),
                          include_auth: bool = True,
                          include_webhooks: bool = True) -> List[str]:
    """COPY (never move or modify) an installed agent's reusable resources
    into workspace/shared so future workspace agents inherit them: provider
    OAuth (auth.json), webhook routes, skills, memories, CLI tools (bin) and
    CLI manifests (clis). Existing shared entries always win — seeding never
    overwrites. Runs at adoption AND every sync pass, so resources the
    installed agent creates later (new skills, tools, webhooks…) flow into
    the shared layer too — not just what existed at adoption time."""
    ensure_shared_seed()
    copied: List[str] = []
    if include_auth:
        src = home / "auth.json"
        dst = SHARED_DIR / "auth.json"
        if src.is_file() and not dst.exists():
            shutil.copy2(src, dst)
            copied.append("auth.json")
    if include_webhooks and _merge_webhooks_from(home):
        copied.append("webhook_subscriptions.json")
    for sub in dirs:
        src = home / sub
        if not src.is_dir():
            continue
        dst_root = SHARED_DIR / sub
        dst_root.mkdir(exist_ok=True)
        copied.extend(f"{sub}/{rel}" for rel in _merge_missing(src, dst_root))
    return copied


UNIT_SNAP_DIR = WORKSPACE / "unit_snapshots"


def _adopted_unit_path(agent: Dict[str, Any]) -> Optional[Path]:
    unit = agent.get("unit")
    if not unit:
        return None
    if agent.get("scope") == "system":
        return Path("/etc/systemd/system") / unit
    return Path.home() / ".config" / "systemd" / "user" / unit


def guard_adopted_units() -> List[str]:
    """Protect installed gateways' systemd unit files.

    Hermes gateways rewrite their own unit definition at boot, and a gateway
    started with a custom HERMES_HOME resolves to the DEFAULT unit name — so
    a stray process can clobber an installed agent's unit to point at the
    wrong home. While the unit is correct we keep a snapshot; if it ever
    stops pointing at the adopted agent's real home, we restore the snapshot
    and record an incident. Legitimate unit updates (still pointing at the
    right home) simply refresh the snapshot.
    """
    repaired: List[str] = []
    reg = load_registry()
    for name, agent in reg["agents"].items():
        if not agent.get("adopted"):
            continue
        path = _adopted_unit_path(agent)
        if not path or not path.exists():
            continue
        try:
            text = path.read_text()
        except OSError:
            continue
        m = re.search(r'HERMES_HOME=([^"\n]+)', text)
        current_home = Path(m.group(1)) if m else Path.home() / ".hermes"
        snap = UNIT_SNAP_DIR / f"{agent.get('scope', 'user')}-{path.name}"
        if current_home.resolve() == Path(agent["home"]).resolve():
            UNIT_SNAP_DIR.mkdir(parents=True, exist_ok=True)
            if not snap.exists() or snap.read_text() != text:
                snap.write_text(text)
        elif snap.exists():
            path.write_text(snap.read_text())
            scope_flag = [] if agent.get("scope") == "system" else ["--user"]
            subprocess.run(["systemctl", *scope_flag, "daemon-reload"],
                           capture_output=True, timeout=30)
            repaired.append(name)
            inc = record_incident(
                name, "unit_clobbered",
                f"{path} pointed HERMES_HOME at {current_home} instead of "
                f"{agent['home']}; restored from snapshot",
                "unit file restored from workspace snapshot")
            if inc:
                dispatch_to_fixer(inc)
    return repaired


def adopt_installed() -> List[str]:
    """Discover installed Hermes gateways and register them as adopted,
    read-only agents. Idempotent; never touches the installs themselves."""
    adopted: List[str] = []
    with _lock:
        reg = load_registry()
        # Re-detect API servers on already-adopted agents: the user may have
        # enabled/disabled API_SERVER_* in an install's .env since adoption
        # (detection is read-only — nothing in the install is modified).
        refreshed = False
        for agent in reg["agents"].values():
            if not agent.get("adopted"):
                continue
            port, key = _detect_api_server(Path(agent["home"]))
            if (port, key) != (agent.get("api_port"), agent.get("api_key")):
                agent["api_port"], agent["api_key"] = port, key
                refreshed = True
        known_homes = {str(Path(a["home"]).resolve())
                       for a in reg["agents"].values()}
        for unit in _list_gateway_units():
            home = _unit_home(unit)
            if not home.is_dir() or str(home.resolve()) in known_homes:
                continue
            name = unit["profile"] or "main"
            if name in reg["agents"] or not NAME_RE.match(name):
                name = f"installed-{name}"[:32]
                if name in reg["agents"] or not NAME_RE.match(name):
                    continue
            api_port, api_key = _detect_api_server(home)
            desc = _soul_summary(home) or (
                f"Installed Hermes gateway ({unit['unit']})")
            if unit["profile"]:
                desc = f"{desc} — profile of the main install"
            reg["agents"][name] = {
                "home": str(home),
                "api_port": api_port,
                "dash_port": (9119 if not unit["profile"] and _port_open(9119)
                              else _next_port(reg, "dash_port", DASH_PORT_BASE)),
                "api_key": api_key,
                "description": desc,
                "created": time.time(),
                "external": True,        # supervised by systemd, not by us
                "adopted": True,         # pre-existing install, found on disk
                "read_only": True,       # the workspace never writes its home
                "contribute": not unit["profile"],   # only main seeds config
                "unit": unit["unit"],
                "scope": unit["scope"],
                "autostart": False,
            }
            known_homes.add(str(home.resolve()))
            adopted.append(name)
            if not unit["profile"]:
                seed_shared_from_home(home)
        if adopted or refreshed:
            save_registry(reg)
    return adopted


# ─── model catalog + per-request model routes ────────────────────────────────

_MODEL_CACHE_FILE = "provider_models_cache.json"


def model_catalog() -> Dict[str, List[str]]:
    """provider → sorted model ids, unioned from every agent home's model
    picker cache (written by `hermes model`; strictly read-only here)."""
    catalog: Dict[str, set] = {}
    for agent in load_registry()["agents"].values():
        try:
            data = json.loads((Path(agent["home"]) / _MODEL_CACHE_FILE).read_text())
        except Exception:
            continue
        for prov, entry in (data or {}).items():
            models = entry.get("models") if isinstance(entry, dict) else None
            for m in models or []:
                mid = m if isinstance(m, str) else (m or {}).get("id")
                if mid:
                    catalog.setdefault(str(prov), set()).add(str(mid))
    return {p: sorted(ms) for p, ms in sorted(catalog.items())}


def build_model_routes() -> Dict[str, Dict[str, str]]:
    """API-server model_routes covering the whole catalog: the alias a client
    sends as the request's ``model`` field is ``provider/model``."""
    routes: Dict[str, Dict[str, str]] = {}
    for prov, models in model_catalog().items():
        for mid in models:
            alias = mid if mid.startswith(f"{prov}/") else f"{prov}/{mid}"
            routes[alias] = {"provider": prov, "model": mid}
    return routes


def ensure_model_routes(home: Path) -> bool:
    """Write ``platforms.api_server.extra.model_routes`` into an agent's
    config.yaml so workflow steps can pick a model per node. The gateway
    reads routes at startup — callers decide about restarts. Returns True
    when the file changed."""
    routes = build_model_routes()
    if not routes:
        return False
    cfg_path = home / "config.yaml"
    try:
        cfg = yaml.safe_load(cfg_path.read_text()) or {}
    except Exception:
        cfg = {}
    platforms = cfg.setdefault("platforms", {})
    if not isinstance(platforms, dict):
        return False                      # unexpected shape — leave untouched
    api = platforms.setdefault("api_server", {})
    if not isinstance(api, dict):
        return False
    extra = api.setdefault("extra", {})
    if not isinstance(extra, dict):
        return False
    if extra.get("model_routes") == routes:
        return False
    extra["model_routes"] = routes
    cfg_path.write_text(yaml.safe_dump(cfg, sort_keys=False))
    return True


def agent_default_model(agent: Dict[str, Any]) -> str:
    """The agent's configured default, as 'provider/model' (best effort)."""
    try:
        cfg = yaml.safe_load((Path(agent["home"]) / "config.yaml").read_text()) or {}
        m = cfg.get("model") or {}
        if isinstance(m, dict) and m.get("default"):
            prov = m.get("provider", "")
            return f"{prov}/{m['default']}" if prov else str(m["default"])
    except Exception:
        pass
    return ""


# ─── busy tracking — never restart an agent mid workflow step ────────────────

_busy_agents: Dict[str, int] = {}
_busy_lock = threading.Lock()
_pending_restarts: set = set()


def mark_agent_busy(name: str) -> None:
    with _busy_lock:
        _busy_agents[name] = _busy_agents.get(name, 0) + 1


def unmark_agent_busy(name: str) -> None:
    with _busy_lock:
        left = _busy_agents.get(name, 0) - 1
        if left <= 0:
            _busy_agents.pop(name, None)
        else:
            _busy_agents[name] = left


def agent_is_busy(name: str) -> bool:
    with _busy_lock:
        return _busy_agents.get(name, 0) > 0


def _drain_pending_restarts(reg: Dict[str, Any]) -> None:
    """Apply restarts that were deferred because the agent was mid-step."""
    for name in list(_pending_restarts):
        agent = reg["agents"].get(name)
        if not agent:
            _pending_restarts.discard(name)
            continue
        if agent_is_busy(name):
            continue
        _pending_restarts.discard(name)
        try:
            if agent.get("external"):
                _systemctl(agent, "reload")
            else:
                stop_agent(name)
                start_agent(name)
        except Exception:
            pass


# ─── agent lifecycle ─────────────────────────────────────────────────────────

def _agent_env(agent: Dict[str, Any]) -> Dict[str, str]:
    env = dict(os.environ)
    env["HERMES_HOME"] = agent["home"]
    # home/bin (→ shared/bin for workspace agents) goes on PATH so shared
    # CLI tools are invocable by name, not just present on disk.
    env["PATH"] = f"{VENV_BIN}:{Path(agent['home']) / 'bin'}:{env.get('PATH', '')}"
    env.pop("VIRTUAL_ENV", None)
    # Confine HOME to the agent's directory. Hermes gateways "self-heal"
    # their systemd unit file under ~/.config/systemd/user on every boot,
    # and a gateway with a custom (non-profile) HERMES_HOME resolves to the
    # DEFAULT unit name — without this, starting any workspace agent would
    # rewrite the machine's real hermes-gateway.service to point at the
    # workspace agent's home, breaking a pre-existing install on its next
    # restart. With HOME set to the agent dir, HERMES_HOME is exactly
    # $HOME/.hermes (a standard layout) and every user-scope file hermes
    # manages stays inside the agent's own directory. Adopted installs keep
    # the real HOME — they must behave exactly as the user runs them.
    if not agent.get("external"):
        env["HOME"] = str(Path(agent["home"]).parent)
        env.pop("XDG_CONFIG_HOME", None)
    return env


# Per-agent config sections that must NEVER be shared: channels are bound to
# one agent identity (platforms, and hermes's top-level whatsapp block), and
# the dashboard block is instance-local.
_CONFIG_PER_AGENT_KEYS = ("platforms", "dashboard", "whatsapp")

# Files/dirs physically stored in workspace/shared and symlinked into every
# workspace-created agent's home: provider OAuth, long-term memory, skills,
# home-local CLI tools (bin + clis manifests), and webhook route definitions.
# Adopted (pre-installed) agents are NEVER linked — their homes are read-only
# to the workspace; they only seed these by copy.
_SHARED_LINKS = ("auth.json", "webhook_subscriptions.json",
                 "memories", "skills", "bin", "clis", "plugins")

_ENV_BLOCK_BEGIN = "# >>> workspace shared (managed — edits inside are overwritten) >>>"
_ENV_BLOCK_END = "# <<< workspace shared <<<"


def _shareable_config_view(home: Path) -> Dict[str, Any]:
    """An agent's config.yaml minus per-agent sections — its inheritable part."""
    try:
        cfg = yaml.safe_load((home / "config.yaml").read_text()) or {}
    except Exception:
        return {}
    for key in _CONFIG_PER_AGENT_KEYS:
        cfg.pop(key, None)
    return cfg


def _shareable_env_view(env_path: Path) -> Dict[str, str]:
    """Shareable KEY → full line from an agent's .env (any position; the last
    occurrence wins, matching dotenv load order)."""
    view: Dict[str, str] = {}
    if not env_path.exists():
        return view
    for line in env_path.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key = line.split("=", 1)[0].strip()
        if not any(key.startswith(p) for p in _ENV_EXCLUDE_PREFIXES):
            view[key] = line
    return view


def _apply_env_block(env_path: Path, shared_lines: List[str]) -> bool:
    """Replace the managed block in an agent's .env; returns True on change."""
    text = env_path.read_text() if env_path.exists() else ""
    own_lines, in_block = [], False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped == _ENV_BLOCK_BEGIN:
            in_block = True
            continue
        if stripped == _ENV_BLOCK_END:
            in_block = False
            continue
        if not in_block:
            own_lines.append(line)
    while own_lines and not own_lines[-1].strip():
        own_lines.pop()
    new_text = "\n".join(
        own_lines + ["", _ENV_BLOCK_BEGIN, *shared_lines, _ENV_BLOCK_END]
    ) + "\n"
    if new_text != text:
        env_path.write_text(new_text)
        return True
    return False


def _ensure_shared_links(home: Path) -> bool:
    """Point home's auth.json/memories/skills at workspace/shared. Existing
    real files/dirs are migrated into shared first (shared wins on conflict)."""
    changed = False
    for name in _SHARED_LINKS:
        shared = SHARED_DIR / name
        target = home / name
        if name.endswith(".json"):
            # A token refresh or route write replaces the symlink with a real
            # file (atomic rename). Never lose that before re-linking.
            if target.is_file() and not target.is_symlink():
                if name == "webhook_subscriptions.json":
                    # Routes are independent entries — merge per route so a
                    # diverged file can't clobber routes other agents added
                    # to the shared copy meanwhile.
                    _merge_webhooks_from(home)
                elif not shared.exists() or target.stat().st_mtime > shared.stat().st_mtime:
                    shutil.copy2(target, shared)
        else:
            shared.mkdir(exist_ok=True)
            if target.is_dir() and not target.is_symlink():
                # Must be recursive: a one-level move drops anything nested
                # inside a subtree shared already has, and the rmtree below
                # would then destroy the only copy.
                _merge_missing(target, shared)
        if target.is_symlink():
            if target.resolve() == shared.resolve():
                continue
            target.unlink()
        elif target.exists():
            if target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink()
        if shared.exists():
            target.symlink_to(shared)
            changed = True
    return changed


def _apply_shared_config(home: Path, shared_cfg: Dict[str, Any]) -> bool:
    """Overlay the shared config onto an agent's config.yaml (per-agent keys
    are preserved); returns True on change."""
    cfg_path = home / "config.yaml"
    try:
        cfg = yaml.safe_load(cfg_path.read_text()) or {}
    except Exception:
        cfg = {}
    merged = dict(shared_cfg)
    for key in _CONFIG_PER_AGENT_KEYS:
        if key in cfg:
            merged[key] = cfg[key]
    if merged != cfg:
        cfg_path.write_text(yaml.safe_dump(merged, sort_keys=False))
        return True
    return False


# ─── workspace self-documentation ────────────────────────────────────────────
#
# Agents can only use the shared-resources feature reliably if it is part of
# their knowledge: a repo-bundled skill (seeded into shared/skills and kept in
# sync — the repo is its source of truth), a managed section inside the shared
# hermes-agent skill (which agents consult whenever they reason about Hermes
# itself), and a managed identity block in every workspace agent's SOUL.md.
# All three refresh continuously, so fresh installs and existing fleets get
# them without any manual step.

SEED_SKILLS_DIR = WORKSPACE / "seed_skills"
_DOC_BLOCK_BEGIN = "<!-- >>> hermes-orchestrator workspace (managed block — edits inside are overwritten) >>> -->"
_DOC_BLOCK_END = "<!-- <<< hermes-orchestrator workspace <<< -->"

_HERMES_AGENT_SKILL_NOTE = """
## This machine: Hermes Orchestrator workspace — resources are FLEET-SHARED

This Hermes instance is one agent in a fleet managed by Hermes Orchestrator.
The fleet has a single shared resource layer: skills, plugins, CLI tools
(`$HERMES_HOME/bin` + `$HERMES_HOME/clis`), memories, webhook routes, MCP
servers, API keys (`.env`), model config, and provider logins are shared
between ALL agents automatically. Your `$HERMES_HOME/skills`, `plugins`,
`bin`, `clis`, and `memories` directories ARE the shared layer.

Practical consequences:

- Anything you save to those directories is instantly available to every
  other agent; running agents auto-reload when shared skills/plugins change.
- Install CLI tools by placing the executable in `$HERMES_HOME/bin/` (already
  on every agent's PATH) with a manifest in `$HERMES_HOME/clis/<name>.md` —
  NEVER via `pip install --user` / `npm install -g` (your HOME is private, so
  those installs are invisible to other agents).
- Config edits (`mcp_servers`, `.env` keys, `model`) propagate to the whole
  fleet within ~10 seconds via the workspace sync.
- Channel bindings (Telegram/WhatsApp/…), the dashboard section, and API
  server identity always stay per-agent and are never shared.
- The machine's pre-installed Hermes agent and its systemd units are
  READ-ONLY to this workspace — never modify them.

Read the `shared-resources` skill (workspace category) for full details, and
the `cli-tools` skill for the CLI manifest format.
""".strip()

_SOUL_NOTE = """
## Workspace context (Hermes Orchestrator fleet)

You are one agent in a fleet managed by Hermes Orchestrator. Reusable
resources are SHARED with every other agent: whatever you save under
`$HERMES_HOME/skills/<category>/<name>/`, `$HERMES_HOME/plugins/<name>/`,
`$HERMES_HOME/bin/` (+ manifest in `$HERMES_HOME/clis/<name>.md`), or
`$HERMES_HOME/memories/` is instantly available fleet-wide, and MCP servers,
API keys, and model config you set sync to all agents within seconds. Install
CLI tools into `$HERMES_HOME/bin/` — never `pip install --user` or
`npm install -g`, which are invisible to other agents. Before installing or
creating shared things, consult the `shared-resources` skill. A dedicated
`fixer` agent automatically receives incident reports and repairs broken
agents in this fleet. Never modify the machine's pre-installed Hermes agent
or its systemd units — it is read-only to this workspace.
""".strip()


def _upsert_marked_block(path: Path, content: str) -> bool:
    """Insert or refresh the managed workspace block at the end of a markdown
    file; creates the file if missing. Returns True when the file changed."""
    block = f"{_DOC_BLOCK_BEGIN}\n{content}\n{_DOC_BLOCK_END}"
    try:
        text = path.read_text()
    except OSError:
        text = ""
    if _DOC_BLOCK_BEGIN in text and _DOC_BLOCK_END in text:
        pre = text.split(_DOC_BLOCK_BEGIN)[0]
        post = text.split(_DOC_BLOCK_END, 1)[1]
        new = pre.rstrip() + ("\n\n" if pre.strip() else "") + block + post
    else:
        new = (text.rstrip() + "\n\n" if text.strip() else "") + block + "\n"
    if new != text:
        try:
            path.write_text(new)
        except OSError:
            return False
        return True
    return False


def seed_workspace_docs() -> List[str]:
    """Sync the repo-bundled workspace skills into shared/skills (unlike
    agent-created resources, the REPO is their source of truth, so changed
    files are overwritten) and refresh the managed workspace section in the
    shared hermes-agent skill."""
    changed: List[str] = []
    if SEED_SKILLS_DIR.is_dir():
        for src in SEED_SKILLS_DIR.rglob("*"):
            if not src.is_file():
                continue
            rel = src.relative_to(SEED_SKILLS_DIR)
            dst = SHARED_DIR / "skills" / rel
            try:
                if not dst.exists() or dst.read_bytes() != src.read_bytes():
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dst)
                    changed.append(f"skills/{rel}")
            except OSError:
                pass
    hermes_skill = (SHARED_DIR / "skills" / "autonomous-ai-agents"
                    / "hermes-agent" / "SKILL.md")
    if hermes_skill.is_file() and _upsert_marked_block(
            hermes_skill, _HERMES_AGENT_SKILL_NOTE):
        changed.append("hermes-agent/SKILL.md (workspace section)")
    return changed


def _merge_cfg_edits(prev: Dict[str, Any], view: Dict[str, Any],
                     out: Dict[str, Any]) -> None:
    """Fold one editor's config changes (its view vs the previous canonical
    state) into out. Dict-valued sections (mcp_servers, tools…) merge per
    sub-key, so two agents adding different entries in the same window both
    land — a whole-section diff would let the newer editor silently clobber
    the other's addition. Sub-keys an editor removed are removed."""
    for key in set(view) | set(prev):
        ours, theirs = prev.get(key), view.get(key)
        if theirs == ours:
            continue
        if key not in view:
            out.pop(key, None)
        elif isinstance(theirs, dict) and isinstance(ours, dict):
            base = out.get(key)
            merged = dict(base) if isinstance(base, dict) else dict(ours)
            for sub in set(theirs) | set(ours):
                if theirs.get(sub) != ours.get(sub):
                    if sub in theirs:
                        merged[sub] = theirs[sub]
                    else:
                        merged.pop(sub, None)
            out[key] = merged
        else:
            out[key] = theirs


SHARED_STATE_PATH = WORKSPACE / "shared_state.json"
_last_sync_restart: Dict[str, float] = {}


def _file_mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def sync_shared(restart_changed: bool = True, force: bool = False) -> Dict[str, Any]:
    """Multi-master sync of shareable config between ALL agents.

    Any agent may be (re)configured — providers, API keys, tools, MCP
    servers. Each pass:
      1. Agents whose config.yaml/.env changed since the last pass are
         treated as editors; their per-key differences against the previous
         canonical state are merged in, ordered by mtime (last writer wins
         on the same key, independent edits from different agents merge).
      2. The new canonical state is written back to every agent, preserving
         each agent's per-agent sections (channels, dashboard, API_SERVER_*).
    """
    with _lock:
        reg = load_registry()
        try:
            seed_workspace_docs()      # cheap no-op unless repo docs changed
        except Exception:
            pass
        state = _load(SHARED_STATE_PATH, {"config": {}, "env": {}, "mtimes": {}})
        prev_cfg: Dict[str, Any] = state["config"]
        prev_env: Dict[str, str] = state["env"]
        mtimes: Dict[str, Dict[str, float]] = state["mtimes"]

        # 1. collect edits (agents whose files moved since last pass).
        # Adopted profile gateways don't contribute: they are clones of the
        # main install and would fight it for canonical state.
        editors = []
        for name, agent in reg["agents"].items():
            if not agent.get("contribute", True):
                continue
            home = Path(agent["home"])
            cfg_m = _file_mtime(home / "config.yaml")
            env_m = _file_mtime(home / ".env")
            seen = mtimes.get(name, {"cfg": 0, "env": 0})
            if force or cfg_m > seen["cfg"] or env_m > seen["env"]:
                editors.append((max(cfg_m, env_m), name, home))
        editors.sort()                      # oldest first → newest wins conflicts

        new_cfg = dict(prev_cfg)
        new_env = dict(prev_env)
        for _, name, home in editors:
            _merge_cfg_edits(prev_cfg, _shareable_config_view(home), new_cfg)
            env_view = _shareable_env_view(home / ".env")
            for key in set(env_view) | set(prev_env):
                if env_view.get(key) != prev_env.get(key):
                    if key in env_view:
                        new_env[key] = env_view[key]
                    else:
                        new_env.pop(key, None)

        # 2. propagate canonical state to every agent
        report: Dict[str, Any] = {"synced_at": time.time(),
                                  "editors": [e[1] for e in editors],
                                  "changed": [], "restarted": []}
        env_lines = [new_env[k] for k in sorted(new_env)]
        for name, agent in reg["agents"].items():
            home = Path(agent["home"])
            if agent.get("read_only"):
                # Adopted installs are sources only — record what we read so
                # they aren't re-treated as editors, but NEVER write to them.
                mtimes[name] = {"cfg": _file_mtime(home / "config.yaml"),
                                "env": _file_mtime(home / ".env")}
                # Resources adopted agents create (skills, CLI tools,
                # webhooks, memories) flow into the shared layer
                # continuously — copy-only, never move. Profile gateways
                # contribute their functional resources but not memories,
                # auth, or config (they are clones of main; letting them
                # fight over canonical config/identity would cause churn).
                try:
                    if agent.get("contribute"):
                        seed_shared_from_home(home)
                    else:
                        seed_shared_from_home(
                            home, dirs=("skills", "bin", "clis", "plugins"),
                            include_auth=False, include_webhooks=False)
                except Exception:
                    pass
                continue
            changed = _ensure_shared_links(home)
            changed |= _apply_shared_config(home, new_cfg)
            changed |= _apply_env_block(home / ".env", env_lines)
            # Every workspace agent's soul carries the fleet/shared-resources
            # context — refreshed here so wording updates reach existing agents.
            changed |= _upsert_marked_block(home / "SOUL.md", _SOUL_NOTE)
            mtimes[name] = {"cfg": _file_mtime(home / "config.yaml"),
                            "env": _file_mtime(home / ".env")}
            if changed:
                report["changed"].append(name)
                recently = time.time() - _last_sync_restart.get(name, 0) < 300
                if restart_changed and is_running(name) and not recently:
                    _last_sync_restart[name] = time.time()
                    if agent_is_busy(name):
                        # mid workflow step — restart once it is idle
                        _pending_restarts.add(name)
                        report.setdefault("deferred", []).append(name)
                    else:
                        stop_agent(name)
                        start_agent(name)
                        report["restarted"].append(name)

        state.update({"config": new_cfg, "env": new_env, "mtimes": mtimes})
        _save(SHARED_STATE_PATH, state)
        (SHARED_DIR / "config.yaml").write_text(yaml.safe_dump(new_cfg, sort_keys=False))
        if report["editors"] or report["changed"]:
            _save(WORKSPACE / "last_sync.json", report)
        return report


def shared_summary() -> Dict[str, Any]:
    """What the fleet currently inherits — for the UI's Shared tab."""
    state = _load(SHARED_STATE_PATH, {"config": {}, "env": {}})
    cfg = state["config"]
    providers = []
    try:
        auth = json.loads((SHARED_DIR / "auth.json").read_text())
        providers = sorted((auth.get("providers") or auth).keys())
    except Exception:
        pass
    skills_dir = SHARED_DIR / "skills"
    return {
        "model": cfg.get("model", {}),
        "mcp_servers": sorted((cfg.get("mcp_servers") or {}).keys()),
        "env_keys": sorted(state["env"].keys()),
        "providers": providers,
        # Real skills (dirs holding a SKILL.md), not just top-level category
        # folders — categories hid whether a nested skill was actually shared.
        "skills": sorted(
            str(p.parent.relative_to(skills_dir))
            for p in skills_dir.rglob("SKILL.md")) if skills_dir.is_dir() else [],
        "plugins": sorted(
            p.name for p in (SHARED_DIR / "plugins").iterdir()
            if p.is_dir()) if (SHARED_DIR / "plugins").is_dir() else [],
        "last_sync": _load(WORKSPACE / "last_sync.json", {}),
        "source": "any agent — last writer wins",
    }


def ensure_shared_seed() -> None:
    """Create workspace/shared and migrate main's shareable files into it."""
    SHARED_DIR.mkdir(parents=True, exist_ok=True)
    for sub in ("memories", "skills", "bin", "clis", "plugins"):
        (SHARED_DIR / sub).mkdir(exist_ok=True)


def create_agent(name: str, description: str = "", soul: str = "") -> Dict[str, Any]:
    if not NAME_RE.match(name):
        raise ValueError("Name must be lowercase letters/digits/hyphens (max 32 chars)")
    with _lock:
        reg = load_registry()
        if name in reg["agents"]:
            raise ValueError(f"Agent '{name}' already exists")

        home = AGENTS_DIR / name / ".hermes"
        home.mkdir(parents=True, exist_ok=True)
        api_port = _next_port(reg, "api_port", API_PORT_BASE)
        dash_port = _next_port(reg, "dash_port", DASH_PORT_BASE)
        api_key = secrets.token_urlsafe(24)

        # Inherit the shared layer: canonical config, provider OAuth,
        # memories, skills, and the shared env block.
        ensure_shared_seed()
        state = _load(SHARED_STATE_PATH, {"config": {}, "env": {}})
        if state["config"]:
            (home / "config.yaml").write_text(
                yaml.safe_dump(state["config"], sort_keys=False))
        (home / ".env").write_text("\n".join([
            "# Per-agent API server (workspace-managed)",
            "API_SERVER_ENABLED=1",
            "API_SERVER_HOST=127.0.0.1",
            f"API_SERVER_PORT={api_port}",
            f"API_SERVER_KEY={api_key}",
        ]) + "\n")
        _apply_env_block(home / ".env",
                         [state["env"][k] for k in sorted(state["env"])])
        _ensure_shared_links(home)

        if soul or description:
            (home / "SOUL.md").write_text(soul or f"# {name}\n\n{description}\n")
        _upsert_marked_block(home / "SOUL.md", _SOUL_NOTE)
        try:
            ensure_model_routes(home)
        except Exception:
            pass

        reg["agents"][name] = {
            "home": str(home),
            "api_port": api_port,
            "dash_port": dash_port,
            "api_key": api_key,
            "description": description,
            "created": time.time(),
            "external": False,
            "autostart": True,
        }
        save_registry(reg)
        return reg["agents"][name]


def delete_agent(name: str) -> None:
    with _lock:
        reg = load_registry()
        agent = reg["agents"].get(name)
        if not agent:
            raise ValueError(f"No such agent: {name}")
        if agent.get("external"):
            raise ValueError(
                "This is an adopted pre-installed gateway — the workspace "
                "never deletes or modifies existing Hermes installs")
        stop_agent(name)
        stop_dashboard(name)
        agent_dir = Path(agent["home"]).parent
        if agent_dir.is_relative_to(AGENTS_DIR):
            shutil.rmtree(agent_dir, ignore_errors=True)
        del reg["agents"][name]
        save_registry(reg)


def _gateway_log(agent: Dict[str, Any]) -> Path:
    return Path(agent["home"]).parent / "gateway.log"


def start_agent(name: str) -> None:
    with _lock:
        reg = load_registry()
        agent = reg["agents"][name]
        if agent.get("external"):
            _systemctl(agent, "start")
            return
        if is_running(name):
            agent["should_run"] = True
            save_registry(reg)
            return
        # Refresh per-request model routes so workflow steps can pick any
        # catalog model on this agent (gateway reads them at startup).
        try:
            ensure_model_routes(Path(agent["home"]))
        except Exception:
            pass
        log = open(_gateway_log(agent), "ab")
        # --force: Hermes refuses shell-started gateways while ANY systemd
        # hermes-gateway unit is active, but that guard protects a shared
        # HERMES_HOME (kanban DB) — every workspace agent has its own home,
        # so the concern doesn't apply here.
        proc = subprocess.Popen(
            [str(HERMES_BIN), "gateway", "run", "--force"],
            env=_agent_env(agent), stdout=log, stderr=log,
            cwd=agent["home"], start_new_session=True,
        )
        _procs[name] = proc
        agent["pid"] = proc.pid
        agent["should_run"] = True
        save_registry(reg)


def stop_agent(name: str) -> None:
    with _lock:
        reg = load_registry()
        agent = reg["agents"][name]
        if agent.get("external"):
            # Only closes dashboards the workspace itself spawned; an
            # install's own dashboard service is never touched.
            stop_dashboard(name)
            _systemctl(agent, "stop")
            return
        agent["should_run"] = False
        save_registry(reg)
        # A stopped agent must not answer anywhere: its dashboard chat runs
        # the agent directly (not via the gateway), so close it as well.
        stop_dashboard(name)
        proc = _procs.pop(name, None)
        if proc and proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
                proc.wait(timeout=15)
            except Exception:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except Exception:
                    pass
        else:
            # Adopted from a previous orchestrator run — kill by stored pid.
            pid = agent.get("pid")
            if pid and _pid_is_agent(pid, agent["home"]):
                try:
                    os.killpg(os.getpgid(pid), signal.SIGTERM)
                except Exception:
                    try:
                        os.kill(pid, signal.SIGTERM)
                    except Exception:
                        pass
        # Wait until the process is actually gone, so an immediate
        # start_agent() doesn't see the dying pid as "already running".
        pid = agent.get("pid")
        if pid:
            deadline = time.time() + 15
            while time.time() < deadline and _pid_is_agent(pid, agent["home"]):
                time.sleep(0.5)


def _pid_is_agent(pid: int, home: str) -> bool:
    """True when pid is alive and is a gateway for this agent's HERMES_HOME.

    Lets the orchestrator re-adopt gateways it spawned in a previous run
    (they outlive us thanks to start_new_session) instead of duplicating them.
    """
    try:
        environ = Path(f"/proc/{pid}/environ").read_bytes()
    except OSError:
        return False
    return f"HERMES_HOME={home}".encode() in environ.split(b"\0")


def is_running(name: str) -> bool:
    reg = load_registry()
    agent = reg["agents"].get(name)
    if not agent:
        return False
    if agent.get("external"):
        return _systemctl(agent, "is-active").stdout.strip() == "active"
    proc = _procs.get(name)
    if proc is not None:
        return proc.poll() is None
    pid = agent.get("pid")
    return bool(pid and _pid_is_agent(pid, agent["home"]))


def _pid_is_dashboard(pid: int, home: str) -> bool:
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return False
    return b"dashboard" in cmdline and _pid_is_agent(pid, home)


def dash_is_running(name: str) -> bool:
    reg = load_registry()
    agent = reg["agents"].get(name)
    if not agent:
        return False
    if agent.get("external"):
        return _port_open(agent["dash_port"])
    proc = _dash_procs.get(name)
    if proc is not None and proc.poll() is None:
        return True
    pid = agent.get("dash_pid")
    return bool(pid and _pid_is_dashboard(pid, agent["home"]))


def start_dashboard(name: str) -> str:
    """Spawn (or reuse) this agent's full native Hermes dashboard."""
    with _lock:
        reg = load_registry()
        agent = reg["agents"][name]
        port = agent["dash_port"]
        if agent.get("external") and _port_open(port):
            # An already-running dashboard (e.g. the install's own systemd
            # dashboard service) is reused as-is.
            return f"http://127.0.0.1:{port}"
        # The dashboard's embedded chat runs the agent directly (not via the
        # gateway), so it must not exist while the agent is stopped.
        if not is_running(name):
            raise ValueError(
                f"Agent '{name}' is stopped — start it before opening its dashboard"
            )
        if not dash_is_running(name):
            if agent.get("read_only"):
                # Never write anything into an adopted install's directory —
                # even a log file. Keep it in the workspace instead.
                log_dir = WORKSPACE / "dash_logs"
                log_dir.mkdir(exist_ok=True)
                log = open(log_dir / f"{name}.log", "ab")
            else:
                log = open(Path(agent["home"]).parent / "dashboard.log", "ab")
            proc = subprocess.Popen(
                [str(HERMES_BIN), "dashboard", "--no-open", "--skip-build",
                 "--isolated", "--host", "127.0.0.1", "--port", str(port)],
                env=_agent_env(agent), stdout=log, stderr=log,
                cwd=agent["home"], start_new_session=True,
            )
            _dash_procs[name] = proc
            agent["dash_pid"] = proc.pid
            save_registry(reg)
        return f"http://127.0.0.1:{port}"


def stop_dashboard(name: str) -> None:
    with _lock:
        reg = load_registry()
        agent = reg["agents"].get(name, {})
        proc = _dash_procs.pop(name, None)
        if proc and proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
                proc.wait(timeout=10)
            except Exception:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except Exception:
                    pass
        else:
            # Adopted from a previous orchestrator run — kill by stored pid.
            pid = agent.get("dash_pid")
            if pid and _pid_is_dashboard(pid, agent.get("home", "")):
                try:
                    os.killpg(os.getpgid(pid), signal.SIGTERM)
                except Exception:
                    try:
                        os.kill(pid, signal.SIGTERM)
                    except Exception:
                        pass
                # Verify death; escalate — a surviving dashboard means the
                # stopped agent can still chat.
                deadline = time.time() + 10
                while time.time() < deadline and _pid_is_dashboard(pid, agent.get("home", "")):
                    time.sleep(0.5)
                if _pid_is_dashboard(pid, agent.get("home", "")):
                    try:
                        os.killpg(os.getpgid(pid), signal.SIGKILL)
                    except Exception:
                        try:
                            os.kill(pid, signal.SIGKILL)
                        except Exception:
                            pass
        agent.pop("dash_pid", None)
        if name in reg["agents"]:
            save_registry(reg)


def tail_log(name: str, lines: int = 120) -> str:
    reg = load_registry()
    agent = reg["agents"][name]
    if agent.get("external"):
        cmd = ["journalctl"]
        if agent.get("scope", "user") != "system":
            cmd.append("--user")
        cmd += ["-u", agent.get("unit", "hermes-gateway.service"),
                "-n", str(lines), "--no-pager", "-o", "cat"]
        r = subprocess.run(cmd, capture_output=True, text=True)
        return r.stdout or r.stderr
    log = _gateway_log(agent)
    if not log.exists():
        return ""
    data = log.read_bytes()[-200_000:]
    return "\n".join(data.decode("utf-8", "replace").splitlines()[-lines:])


# ─── subagents (Hermes profiles inside one agent's home) ────────────────────

def list_subagents(name: str) -> List[Dict[str, Any]]:
    reg = load_registry()
    agent = reg["agents"][name]
    profiles_dir = Path(agent["home"]) / "profiles"
    # Profiles that run as their own adopted gateway are promoted to
    # first-class agents — don't list them twice.
    promoted = {str(Path(a["home"]).resolve())
                for n, a in reg["agents"].items() if n != name}
    out = []
    if profiles_dir.is_dir():
        for p in sorted(profiles_dir.iterdir()):
            if p.is_dir() and str(p.resolve()) not in promoted:
                soul = p / "SOUL.md"
                out.append({
                    "name": p.name,
                    "description": soul.read_text()[:200].strip() if soul.exists() else "",
                })
    return out


def create_subagent(name: str, sub_name: str, description: str = "") -> Dict[str, Any]:
    if not NAME_RE.match(sub_name):
        raise ValueError("Subagent name must be lowercase letters/digits/hyphens")
    reg = load_registry()
    agent = reg["agents"][name]
    if agent.get("read_only"):
        raise ValueError(
            "This is an adopted pre-installed agent — the workspace won't "
            "modify it; manage its profiles with the hermes CLI instead")
    r = subprocess.run(
        [str(HERMES_BIN), "profile", "create", sub_name, "--clone"],
        env=_agent_env(agent), capture_output=True, text=True, timeout=120,
    )
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout).strip()[-500:])
    if description:
        prof_home = Path(agent["home"]) / "profiles" / sub_name
        (prof_home / "SOUL.md").write_text(f"# {sub_name}\n\n{description}\n")
    return {"name": sub_name, "description": description}


def delete_subagent(name: str, sub_name: str) -> None:
    reg = load_registry()
    agent = reg["agents"][name]
    if agent.get("read_only"):
        raise ValueError(
            "This is an adopted pre-installed agent — the workspace won't "
            "modify it; manage its profiles with the hermes CLI instead")
    r = subprocess.run(
        [str(HERMES_BIN), "profile", "delete", sub_name],
        env=_agent_env(agent), input="y\n", capture_output=True, text=True, timeout=120,
    )
    prof_home = Path(agent["home"]) / "profiles" / sub_name
    if prof_home.exists():
        shutil.rmtree(prof_home, ignore_errors=True)


# ─── shared skills propagation ───────────────────────────────────────────────
#
# Hermes gateways cache the skill index in-process and only invalidate it in
# the process that created a skill. With a SHARED skills directory that means
# every OTHER running gateway keeps a stale index until it restarts. The
# watchdog watches the shared skills dir and, when it changes, gracefully
# reloads running gateways: adopted ones via their unit's ExecReload (hermes's
# own planned-restart signal), workspace ones via stop/start.

_SKILLS_WATCH_PATH = WORKSPACE / "skills_watch.json"
_SKILL_RELOAD_COOLDOWN = 600
_last_skill_reload: Dict[str, float] = {}


def _skills_manifest() -> str:
    """Fingerprint of the shared resources gateways load at startup: the
    skill index (SKILL.md files) and plugin code (*.py — deliberately not
    plugin state files like state.json, which change constantly and don't
    require a reload)."""
    h = hashlib.sha1()
    for d, pattern in ((SHARED_DIR / "skills", "SKILL.md"),
                       (SHARED_DIR / "plugins", "*.py")):
        if not d.is_dir():
            continue
        for p in sorted(d.rglob(pattern)):
            try:
                st = p.stat()
            except OSError:
                continue
            h.update(f"{p}|{st.st_mtime_ns}|{st.st_size}\n".encode())
    return h.hexdigest()


def propagate_skill_changes() -> List[str]:
    state = _load(_SKILLS_WATCH_PATH, {})
    current = _skills_manifest()
    if state.get("manifest") == current:
        return []
    first_run = "manifest" not in state
    _save(_SKILLS_WATCH_PATH, {"manifest": current, "changed_at": time.time()})
    if first_run:                       # baseline only — nothing to propagate
        return []
    reloaded: List[str] = []
    now = time.time()
    for name, agent in load_registry()["agents"].items():
        if not is_running(name):
            continue
        if now - _last_skill_reload.get(name, 0) < _SKILL_RELOAD_COOLDOWN:
            continue
        if agent_is_busy(name):
            # mid workflow step — reload once idle (cooldown not consumed)
            _pending_restarts.add(name)
            continue
        _last_skill_reload[name] = now
        try:
            if agent.get("external"):
                _systemctl(agent, "reload")
            else:
                stop_agent(name)
                start_agent(name)
            reloaded.append(name)
        except Exception:
            pass
    return reloaded


# ─── shared CLI tools registry ───────────────────────────────────────────────
#
# The same concept as shared skills, for command-line tools: executables live
# in shared/bin (already on every workspace agent's PATH via the home bin
# symlink) and each tool is described by a markdown manifest in shared/clis/
# (<name>.md: title, description paragraph, fenced command examples). Agents
# register tools by simply writing those two files — the shared "cli-tools"
# skill teaches them the convention — and the workspace UI's CLI tab and all
# other agents see them immediately.

def _parse_cli_manifest(text: str) -> Dict[str, str]:
    """description = prose before the first ## heading; commands = fenced code."""
    description: List[str] = []
    commands: List[str] = []
    in_fence = False
    past_intro = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            commands.append(line)
        elif stripped.startswith("##"):
            past_intro = True
        elif stripped and not stripped.startswith("#") and not past_intro:
            description.append(stripped)
    return {"description": " ".join(description),
            "commands": "\n".join(commands)}


def list_clis() -> List[Dict[str, Any]]:
    clis_dir = SHARED_DIR / "clis"
    bin_dir = SHARED_DIR / "bin"
    out: List[Dict[str, Any]] = []
    documented = set()
    if clis_dir.is_dir():
        for p in sorted(clis_dir.glob("*.md")):
            try:
                parsed = _parse_cli_manifest(p.read_text())
            except OSError:
                continue
            documented.add(p.stem)
            out.append({"name": p.stem, "documented": True, **parsed})
    if bin_dir.is_dir():
        for p in sorted(bin_dir.iterdir()):
            if p.name not in documented and not p.name.startswith("."):
                out.append({"name": p.name, "documented": False,
                            "description": "binary in shared bin — no manifest yet",
                            "commands": ""})
    return out


def create_cli(name: str, description: str, commands: str = "") -> Dict[str, Any]:
    if not re.match(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$", name):
        raise ValueError("CLI name must be alphanumeric with ._- (max 64 chars)")
    ensure_shared_seed()
    body = f"# {name}\n\n{description.strip()}\n"
    if commands.strip():
        body += f"\n## Commands\n\n```\n{commands.strip()}\n```\n"
    (SHARED_DIR / "clis" / f"{name}.md").write_text(body)
    return {"name": name, "documented": True,
            **_parse_cli_manifest(body)}


def delete_cli(name: str) -> None:
    path = SHARED_DIR / "clis" / f"{name}.md"
    if path.is_file():
        path.unlink()


# ─── incidents + fixer ───────────────────────────────────────────────────────

def list_incidents(limit: int = 100) -> List[Dict[str, Any]]:
    return _load(INCIDENTS_PATH, [])[-limit:][::-1]


_recent_incidents: Dict[str, float] = {}
_INCIDENT_DEDUP_SECS = 600


def record_incident(agent: str, kind: str, detail: str, action: str) -> Optional[Dict[str, Any]]:
    fingerprint = hashlib.sha1(f"{agent}|{kind}|{detail[:200]}".encode()).hexdigest()
    now = time.time()
    last = _recent_incidents.get(fingerprint, 0)
    _recent_incidents[fingerprint] = now
    if now - last < _INCIDENT_DEDUP_SECS:
        return None
    with _lock:
        incidents = _load(INCIDENTS_PATH, [])
        inc = {
            "id": len(incidents) + 1,
            "ts": time.time(),
            "agent": agent,
            "kind": kind,
            "detail": detail[-2000:],
            "action": action,
            "fixer": "pending",
        }
        incidents.append(inc)
        _save(INCIDENTS_PATH, incidents[-500:])
        return inc


def _set_fixer_status(inc_id: int, status: str) -> None:
    with _lock:
        incidents = _load(INCIDENTS_PATH, [])
        for inc in incidents:
            if inc["id"] == inc_id:
                inc["fixer"] = status[:300]
        _save(INCIDENTS_PATH, incidents)


FIXER_PLAYBOOK = """You are the workspace Fixer. An incident occurred in another agent.

Workspace layout:
- Root: {root}
- Workspace agent homes: {root}/agents/<name>/.hermes
- Gateway logs: {root}/agents/<name>/gateway.log
- Adopted pre-installed agents (systemd-managed, e.g. ~/.hermes and its
  profiles) are READ-ONLY: never edit their files, config, .env or memory.
  For those, only use the orchestrator API restart endpoint (it restarts the
  systemd unit) and journalctl for logs.
- Orchestrator API (no auth, localhost): http://127.0.0.1:{port}/api/agents,
  POST /api/agents/<name>/restart, GET /api/agents/<name>/logs

Incident #{id} — agent '{agent}', type: {kind}
Automatic action already taken: {action}

Details:
{detail}

Diagnose the root cause using your terminal tools (read the agent's log and
config), apply the smallest fix you can (config/env/file repair), then restart
the agent via the orchestrator API and verify it stays up. Reply with a short
summary of the cause and the fix."""


def dispatch_to_fixer(inc: Dict[str, Any]) -> None:
    """Send an incident to the fixer agent's OpenAI-compatible API server."""
    if inc["agent"] == "fixer":
        _set_fixer_status(inc["id"], "skipped (fixer incident)")
        return

    def _run() -> None:
        reg = load_registry()
        fixer = reg["agents"].get("fixer")
        if not fixer:
            _set_fixer_status(inc["id"], "no fixer agent")
            return
        prompt = FIXER_PLAYBOOK.format(
            root=ROOT, port=WORKSPACE_PORT, id=inc["id"], agent=inc["agent"],
            kind=inc["kind"], action=inc["action"], detail=inc["detail"],
        )
        body = json.dumps({
            "model": "hermes-agent",
            "messages": [{"role": "user", "content": prompt}],
        }).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{fixer['api_port']}/v1/chat/completions",
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {fixer['api_key']}",
                "X-Hermes-Session-Id": f"incident-{inc['agent']}",
            },
        )
        try:
            _set_fixer_status(inc["id"], "dispatched")
            with urllib.request.urlopen(req, timeout=600) as resp:
                data = json.loads(resp.read())
            answer = data["choices"][0]["message"]["content"]
            _set_fixer_status(inc["id"], f"fixed: {answer[:250]}")
        except Exception as exc:
            _set_fixer_status(inc["id"], f"dispatch failed: {exc}")

    threading.Thread(target=_run, daemon=True, name=f"fixer-inc-{inc['id']}").start()


# ─── watchdog ────────────────────────────────────────────────────────────────

def _scan_log_errors(name: str, agent: Dict[str, Any]) -> Optional[str]:
    log = _gateway_log(agent) if not agent.get("external") else None
    if log is None or not log.exists():
        return None
    size = log.stat().st_size
    offset = _log_offsets.get(name, size if name not in _log_offsets else 0)
    if name not in _log_offsets:            # first pass: don't replay history
        _log_offsets[name] = size
        return None
    if size <= offset:
        _log_offsets[name] = min(offset, size)
        return None
    with open(log, "rb") as fh:
        fh.seek(offset)
        chunk = fh.read(200_000).decode("utf-8", "replace")
    _log_offsets[name] = size
    hits = [l for l in chunk.splitlines() if _LOG_ERROR_RE.search(l)]
    return "\n".join(hits[-30:]) if hits else None


def _check_health(name: str, agent: Dict[str, Any]) -> bool:
    if not agent.get("api_port"):
        return False
    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{agent['api_port']}/health", timeout=5):
            return True
    except Exception:
        return False


def watchdog_tick() -> None:
    try:
        guard_adopted_units()
    except Exception:
        pass
    reg = load_registry()
    _drain_pending_restarts(reg)
    for name, agent in reg["agents"].items():
        managed = not agent.get("external")

        # 1. crash detection + auto-restart
        if managed and agent.get("should_run") and not is_running(name):
            proc = _procs.get(name)
            code = proc.poll() if proc else None
            inc = record_incident(
                name, "crash",
                f"Gateway process exited (code={code}). Last log lines:\n"
                + tail_log(name, 40),
                "auto-restarted by watchdog",
            )
            try:
                _procs.pop(name, None)
                start_agent(name)
            except Exception as exc:
                if inc:
                    inc["action"] = f"restart FAILED: {exc}"
            if inc:
                dispatch_to_fixer(inc)
            continue

        # 2. error lines in the gateway log
        if managed and is_running(name):
            errors = _scan_log_errors(name, agent)
            if errors:
                inc = record_incident(name, "log_error", errors, "none (agent still running)")
                if inc:
                    dispatch_to_fixer(inc)

        # 3. API health (only meaningful while running, and only for agents
        # that actually expose an API server)
        if agent.get("api_port") and is_running(name):
            if _check_health(name, agent):
                _health_fails[name] = 0
            else:
                _health_fails[name] = _health_fails.get(name, 0) + 1
                if _health_fails[name] == 3:
                    inc = record_incident(
                        name, "health",
                        f"API server on port {agent['api_port']} failed 3 health probes",
                        "none", )
                    if inc:
                        dispatch_to_fixer(inc)


def start_watchdog(interval: int = 10) -> threading.Thread:
    def _loop() -> None:
        while True:
            try:
                watchdog_tick()
            except Exception:
                pass
            try:
                sync_shared()          # mtime-gated: no-op unless someone edited
            except Exception:
                pass
            try:
                propagate_skill_changes()
            except Exception:
                pass
            time.sleep(interval)
    t = threading.Thread(target=_loop, daemon=True, name="watchdog")
    t.start()
    return t


# ─── status / graph ──────────────────────────────────────────────────────────

def agent_status(name: str) -> Dict[str, Any]:
    reg = load_registry()
    agent = dict(reg["agents"][name])
    agent.pop("api_key", None)
    agent["name"] = name
    agent["running"] = is_running(name)
    # Agents without an API server (adopted profile gateways) can't be
    # probed — running is the best signal we have.
    agent["healthy"] = agent["running"] and (
        _check_health(name, reg["agents"][name])
        if agent.get("api_port") else True)
    agent["subagents"] = list_subagents(name)
    agent["dash_running"] = dash_is_running(name)
    return agent


def graph() -> Dict[str, Any]:
    reg = load_registry()
    homes = {str(Path(a["home"]).resolve()): n for n, a in reg["agents"].items()}
    nodes, edges = [], []
    for name in reg["agents"]:
        st = agent_status(name)
        nodes.append({
            "id": name, "type": "gateway", "running": st["running"],
            "healthy": st["healthy"], "port": st["api_port"],
            "adopted": bool(st.get("adopted")),
            "description": st.get("description", ""),
        })
        # Adopted profile gateways hang off the install they were cloned
        # from: <main home>/profiles/<name>.
        home = Path(reg["agents"][name]["home"]).resolve()
        if st.get("adopted") and home.parent.name == "profiles":
            parent = homes.get(str(home.parent.parent))
            if parent:
                edges.append({"from": parent, "to": name, "kind": "profile"})
        for sub in st["subagents"]:
            sid = f"{name}/{sub['name']}"
            nodes.append({"id": sid, "type": "subagent", "label": sub["name"]})
            edges.append({"from": name, "to": sid, "kind": "subagent"})
    return {"nodes": nodes, "edges": edges}
