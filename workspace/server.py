"""Workspace UI server — FastAPI wrapper around the orchestrator.

Run with the hermes venv python:
    hermes-venv/bin/python workspace/server.py
Binds 127.0.0.1:9100.
"""

from __future__ import annotations

from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import orchestrator as orc

app = FastAPI(title="AI Agents Workspace")
STATIC = Path(__file__).parent / "static"


class AgentCreate(BaseModel):
    name: str
    description: str = ""
    soul: str = ""


class SubagentCreate(BaseModel):
    name: str
    description: str = ""


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
        return {"url": orc.start_dashboard(name)}
    except ValueError as exc:
        raise HTTPException(409, str(exc))


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
    orc.delete_subagent(name, sub)
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
    orc.register_main()
    reg = orc.load_registry()
    if "fixer" not in reg["agents"]:
        orc.create_agent(
            "fixer",
            "Repairs other agents automatically when the watchdog reports an incident",
            soul=(Path(__file__).parent / "fixer_soul.md").read_text(),
        )
    try:
        orc.sync_shared(restart_changed=False)     # adopt current state first
    except Exception:
        pass
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
    uvicorn.run(app, host="127.0.0.1", port=orc.WORKSPACE_PORT, log_level="warning")
