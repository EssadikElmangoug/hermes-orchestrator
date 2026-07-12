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

ROOT = Path(__file__).resolve().parent.parent          # ai agents workplace/
WORKSPACE = ROOT / "workspace"
AGENTS_DIR = ROOT / "agents"
SHARED_DIR = WORKSPACE / "shared"
REGISTRY_PATH = WORKSPACE / "registry.json"
INCIDENTS_PATH = WORKSPACE / "incidents.json"
VENV_BIN = ROOT / "hermes-venv" / "bin"
HERMES_BIN = VENV_BIN / "hermes"
MAIN_HOME = ROOT / "hermes-home"

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


# ─── agent lifecycle ─────────────────────────────────────────────────────────

def _agent_env(agent: Dict[str, Any]) -> Dict[str, str]:
    env = dict(os.environ)
    env["HERMES_HOME"] = agent["home"]
    env["PATH"] = f"{VENV_BIN}:{env.get('PATH', '')}"
    env.pop("VIRTUAL_ENV", None)
    return env


# Per-agent config sections that must NEVER be shared: channels are bound to
# one agent identity, and the dashboard block is instance-local.
_CONFIG_PER_AGENT_KEYS = ("platforms", "dashboard")

# Files/dirs physically stored in workspace/shared and symlinked into every
# agent's home (including main): provider OAuth, long-term memory, skills.
_SHARED_LINKS = ("auth.json", "memories", "skills")

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
            # A token refresh replaces the symlink with a real file (atomic
            # rename). Never lose that: the newer file wins and becomes the
            # shared copy before re-linking.
            if target.is_file() and not target.is_symlink():
                if not shared.exists() or target.stat().st_mtime > shared.stat().st_mtime:
                    shutil.copy2(target, shared)
        else:
            shared.mkdir(exist_ok=True)
            if target.is_dir() and not target.is_symlink():
                for item in target.iterdir():
                    dest = shared / item.name
                    if not dest.exists():
                        shutil.move(str(item), dest)
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
        state = _load(SHARED_STATE_PATH, {"config": {}, "env": {}, "mtimes": {}})
        prev_cfg: Dict[str, Any] = state["config"]
        prev_env: Dict[str, str] = state["env"]
        mtimes: Dict[str, Dict[str, float]] = state["mtimes"]

        # 1. collect edits (agents whose files moved since last pass)
        editors = []
        for name, agent in reg["agents"].items():
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
            cfg_view = _shareable_config_view(home)
            for key in set(cfg_view) | set(prev_cfg):
                if cfg_view.get(key) != prev_cfg.get(key):
                    if key in cfg_view:
                        new_cfg[key] = cfg_view[key]
                    else:
                        new_cfg.pop(key, None)
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
            changed = _ensure_shared_links(home)
            changed |= _apply_shared_config(home, new_cfg)
            changed |= _apply_env_block(home / ".env", env_lines)
            mtimes[name] = {"cfg": _file_mtime(home / "config.yaml"),
                            "env": _file_mtime(home / ".env")}
            if changed:
                report["changed"].append(name)
                recently = time.time() - _last_sync_restart.get(name, 0) < 300
                if restart_changed and is_running(name) and not recently:
                    _last_sync_restart[name] = time.time()
                    if agent.get("external"):
                        subprocess.run(["systemctl", "--user", "restart", "hermes-gateway"],
                                       capture_output=True, timeout=60)
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
        "skills": sorted(p.name for p in skills_dir.iterdir() if p.is_dir()) if skills_dir.is_dir() else [],
        "last_sync": _load(WORKSPACE / "last_sync.json", {}),
        "source": "any agent — last writer wins",
    }


def ensure_shared_seed() -> None:
    """Create workspace/shared and migrate main's shareable files into it."""
    SHARED_DIR.mkdir(parents=True, exist_ok=True)
    (SHARED_DIR / "memories").mkdir(exist_ok=True)
    (SHARED_DIR / "skills").mkdir(exist_ok=True)


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


def register_main() -> None:
    """Register the pre-existing hermes-home install as agent 'main'.

    It is supervised by systemd (hermes-gateway.service), not by us.
    """
    with _lock:
        reg = load_registry()
        if "main" in reg["agents"]:
            return
        api_key = secrets.token_urlsafe(24)
        env_path = MAIN_HOME / ".env"
        text = env_path.read_text() if env_path.exists() else ""
        if "API_SERVER_ENABLED" not in text:
            text = text.rstrip() + (
                "\n\n# Per-agent API server (workspace-managed)\n"
                "API_SERVER_ENABLED=1\nAPI_SERVER_HOST=127.0.0.1\n"
                f"API_SERVER_PORT={API_PORT_BASE - 1}\nAPI_SERVER_KEY={api_key}\n"
            )
            env_path.write_text(text)
        else:
            m = re.search(r"API_SERVER_KEY=(\S+)", text)
            api_key = m.group(1) if m else api_key
        reg["agents"]["main"] = {
            "home": str(MAIN_HOME),
            "api_port": API_PORT_BASE - 1,
            "dash_port": 9119,
            "api_key": api_key,
            "description": "Primary agent (original hermes-home, systemd-managed)",
            "created": time.time(),
            "external": True,
            "autostart": True,
        }
        save_registry(reg)
        subprocess.run(["systemctl", "--user", "restart", "hermes-gateway"],
                       capture_output=True, timeout=60)


def delete_agent(name: str) -> None:
    with _lock:
        reg = load_registry()
        agent = reg["agents"].get(name)
        if not agent:
            raise ValueError(f"No such agent: {name}")
        if agent.get("external"):
            raise ValueError("The main agent cannot be deleted from the workspace")
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
            subprocess.run(["systemctl", "--user", "start", "hermes-gateway"],
                           capture_output=True, timeout=60)
            return
        if is_running(name):
            agent["should_run"] = True
            save_registry(reg)
            return
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
            subprocess.run(["systemctl", "--user", "stop", "hermes-gateway"],
                           capture_output=True, timeout=60)
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
        r = subprocess.run(["systemctl", "--user", "is-active", "hermes-gateway"],
                           capture_output=True, text=True)
        return r.stdout.strip() == "active"
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
        return True
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
        if agent.get("external"):
            return f"http://127.0.0.1:{port}"
        # The dashboard's embedded chat runs the agent directly (not via the
        # gateway), so it must not exist while the agent is stopped.
        if not is_running(name):
            raise ValueError(
                f"Agent '{name}' is stopped — start it before opening its dashboard"
            )
        if not dash_is_running(name):
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
        r = subprocess.run(
            ["journalctl", "--user", "-u", "hermes-gateway", "-n", str(lines),
             "--no-pager", "-o", "cat"], capture_output=True, text=True)
        return r.stdout
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
    out = []
    if profiles_dir.is_dir():
        for p in sorted(profiles_dir.iterdir()):
            if p.is_dir():
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
    r = subprocess.run(
        [str(HERMES_BIN), "profile", "delete", sub_name],
        env=_agent_env(agent), input="y\n", capture_output=True, text=True, timeout=120,
    )
    prof_home = Path(agent["home"]) / "profiles" / sub_name
    if prof_home.exists():
        shutil.rmtree(prof_home, ignore_errors=True)


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
- Agent homes: {root}/agents/<name>/.hermes (main agent: {root}/hermes-home)
- Gateway logs: {root}/agents/<name>/gateway.log (main: journalctl --user -u hermes-gateway)
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
    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{agent['api_port']}/health", timeout=5):
            return True
    except Exception:
        return False


def watchdog_tick() -> None:
    reg = load_registry()
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

        # 3. API health (only meaningful while running)
        if is_running(name):
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
    agent["healthy"] = agent["running"] and _check_health(name, reg["agents"][name])
    agent["subagents"] = list_subagents(name)
    agent["dash_running"] = dash_is_running(name)
    return agent


def graph() -> Dict[str, Any]:
    reg = load_registry()
    nodes, edges = [], []
    for name in reg["agents"]:
        st = agent_status(name)
        nodes.append({
            "id": name, "type": "gateway", "running": st["running"],
            "healthy": st["healthy"], "port": st["api_port"],
            "description": st.get("description", ""),
        })
        for sub in st["subagents"]:
            sid = f"{name}/{sub['name']}"
            nodes.append({"id": sid, "type": "subagent", "label": sub["name"]})
            edges.append({"from": name, "to": sid})
    return {"nodes": nodes, "edges": edges}
