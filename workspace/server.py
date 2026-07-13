"""Workspace UI server — FastAPI wrapper around the orchestrator.

Run with any python that has fastapi/uvicorn/pyyaml — the bundled
hermes-venv if present, otherwise the machine's installed hermes venv:
    hermes-venv/bin/python workspace/server.py
    /usr/local/lib/hermes-agent/venv/bin/python workspace/server.py
Binds 127.0.0.1:9100.
"""

from __future__ import annotations

import os
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, Response
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import orchestrator as orc
from auth import AuthGate
from proxy import DashboardProxy, agent_for_host

app = FastAPI(title="Hermes Orchestrator")
STATIC = Path(__file__).parent / "static"

# Public domain the workspace is served under (e.g. orchestrator.kundlas.com).
# When set, agent dashboards are exposed at https://<agent>.<domain>/ through
# the DashboardProxy middleware; when unset (local machine), dashboards are
# opened directly on their 127.0.0.1 ports.
DOMAIN = os.environ.get("WORKSPACE_DOMAIN", "").strip().lower().rstrip(".")


def _password() -> str:
    """Workspace password: WORKSPACE_PASSWORD, or WORKSPACE_PASSWORD_FILE.
    Empty → authentication disabled (local mode)."""
    pw = os.environ.get("WORKSPACE_PASSWORD", "").strip()
    if pw:
        return pw
    pw_file = os.environ.get("WORKSPACE_PASSWORD_FILE", "").strip()
    if pw_file:
        try:
            return Path(pw_file).read_text().strip()
        except OSError:
            pass
    return ""


class AgentCreate(BaseModel):
    name: str
    description: str = ""
    soul: str = ""


class SubagentCreate(BaseModel):
    name: str
    description: str = ""


class CliCreate(BaseModel):
    name: str
    description: str
    commands: str = ""


@app.get("/api/agents")
def api_agents():
    reg = orc.load_registry()
    return [orc.agent_status(name) for name in sorted(reg["agents"])]


@app.post("/api/agents")
def api_create_agent(body: AgentCreate):
    try:
        orc.create_agent(body.name, body.description, body.soul)
        orc.start_agent(body.name)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return orc.agent_status(body.name)


@app.delete("/api/agents/{name}")
def api_delete_agent(name: str):
    try:
        orc.delete_agent(name)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"ok": True}


@app.post("/api/agents/{name}/start")
def api_start(name: str):
    _require(name)
    orc.start_agent(name)
    return {"ok": True}


@app.post("/api/agents/{name}/stop")
def api_stop(name: str):
    _require(name)
    orc.stop_agent(name)
    return {"ok": True}


@app.post("/api/agents/{name}/restart")
def api_restart(name: str):
    _require(name)
    orc.stop_agent(name)
    orc.start_agent(name)
    return {"ok": True}


@app.get("/api/agents/{name}/logs", response_class=PlainTextResponse)
def api_logs(name: str, lines: int = 120):
    _require(name)
    return orc.tail_log(name, lines)


@app.post("/api/agents/{name}/dashboard")
def api_dashboard(name: str):
    _require(name)
    try:
        url = orc.start_dashboard(name)
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    if DOMAIN:
        # One fixed dashboard hostname for all agents (no per-agent certs):
        # /a/<name> pins the selection, then the SPA runs at the root.
        url = f"https://dash.{DOMAIN}/a/{name}"
    return {"url": url}


@app.get("/api/tls-check")
def api_tls_check(domain: str = ""):
    """Caddy on_demand_tls 'ask' endpoint (kept for setups that still use
    per-agent subdomains): approve the workspace domain, the dash host, and
    <agent>.<domain> hosts that actually exist."""
    d = domain.strip().lower().rstrip(".")
    if DOMAIN and (d == DOMAIN or d == f"dash.{DOMAIN}"
                   or agent_for_host(d, DOMAIN)):
        return {"ok": True}
    return Response(status_code=403)


@app.get("/api/agents/{name}/subagents")
def api_subagents(name: str):
    _require(name)
    return orc.list_subagents(name)


@app.post("/api/agents/{name}/subagents")
def api_create_subagent(name: str, body: SubagentCreate):
    _require(name)
    try:
        return orc.create_subagent(name, body.name, body.description)
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(400, str(exc))


@app.delete("/api/agents/{name}/subagents/{sub}")
def api_delete_subagent(name: str, sub: str):
    _require(name)
    try:
        orc.delete_subagent(name, sub)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"ok": True}


@app.get("/api/graph")
def api_graph():
    return orc.graph()


@app.get("/api/incidents")
def api_incidents(limit: int = 100):
    return orc.list_incidents(limit)


@app.get("/api/shared")
def api_shared():
    return orc.shared_summary()


@app.get("/api/clis")
def api_clis():
    return orc.list_clis()


@app.post("/api/clis")
def api_create_cli(body: CliCreate):
    try:
        return orc.create_cli(body.name, body.description, body.commands)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.delete("/api/clis/{name}")
def api_delete_cli(name: str):
    orc.delete_cli(name)
    return {"ok": True}


@app.post("/api/sync")
def api_sync():
    return orc.sync_shared(force=True)


def _require(name: str) -> None:
    if name not in orc.load_registry()["agents"]:
        raise HTTPException(404, f"No such agent: {name}")


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")


def bootstrap() -> None:
    orc.AGENTS_DIR.mkdir(exist_ok=True)
    orc.ensure_shared_seed()
    # Discover pre-installed Hermes gateways (systemd) and adopt them
    # read-only; the main install seeds shared auth/skills/memories/CLIs.
    try:
        adopted = orc.adopt_installed()
        if adopted:
            print(f"Adopted installed gateways: {', '.join(adopted)}")
        orc.guard_adopted_units()      # snapshot units before anything starts
    except Exception as exc:
        print(f"Install discovery skipped: {exc}")
    try:
        orc.sync_shared(restart_changed=False)     # adopt current state first
    except Exception:
        pass
    reg = orc.load_registry()
    if "fixer" not in reg["agents"]:
        orc.create_agent(
            "fixer",
            "Repairs other agents automatically when the watchdog reports an incident",
            soul=(Path(__file__).parent / "fixer_soul.md").read_text(),
        )
    for name, agent in orc.load_registry()["agents"].items():
        # should_run reflects the user's last start/stop choice and outlives
        # orchestrator restarts; autostart only applies to never-started agents.
        want = agent.get("should_run", agent.get("autostart", False))
        if want and not agent.get("external"):
            try:
                orc.start_agent(name)
            except Exception:
                pass
    orc.start_watchdog()


if __name__ == "__main__":
    bootstrap()
    # AuthGate is outermost so one session cookie (issued for the parent
    # domain) protects the workspace AND every agent dashboard subdomain.
    stack = AuthGate(DashboardProxy(app, DOMAIN), _password(), DOMAIN,
                     Path(__file__).parent)
    uvicorn.run(stack, host="127.0.0.1", port=orc.WORKSPACE_PORT,
                log_level="warning")
