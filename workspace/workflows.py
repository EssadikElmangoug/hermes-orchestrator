"""Workflow engine — visual automation DAGs over the fleet's resources.

A workflow is a JSON document of nodes and edges, edited on the canvas (or by
the workflow-builder agent through the same API). Executable nodes form the
flow DAG; capability nodes (skills, CLI tools, MCP servers, env keys, plugins)
attach to agent steps and become instructions in that step's prompt — they are
never executed on their own.

Node types
  trigger.manual   started from the UI (optional input text)
  trigger.cron     config.schedule = 5-field cron expression
  trigger.webhook  fired by POST /api/hooks/<workflow>/<secret>
  step.agent       config.agent / instruction / output ("text"|"json") /
                   json_fields — runs one chat-completions call on the agent
  gate.approval    pauses the run until a human approves from the UI
  out.channel      config.agent / channel / target — the owning agent delivers
                   the upstream output through its linked channel
  out.webhook      config.url — POSTs the upstream output as JSON
  cap.skill / cap.cli / cap.mcp / cap.env / cap.plugin
                   config.name — attached to a step.agent via a "cap" edge

Runs execute in a background thread, one node at a time in dependency order.
Every node's prompt/output/error lands in the run document so the UI can
overlay live progress on the canvas. Failures raise workspace incidents and
are dispatched to the fixer like any other fleet problem.
"""

from __future__ import annotations

import json
import re
import secrets
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

import orchestrator as orc

WORKFLOWS_DIR = orc.WORKSPACE / "workflows"
RUNS_DIR = orc.WORKSPACE / "workflow_runs"

ID_RE = re.compile(r"^[a-zA-Z0-9_-]{1,32}$")
EXEC_TYPES = {"trigger.manual", "trigger.cron", "trigger.webhook",
              "step.agent", "gate.approval", "out.channel", "out.webhook"}
CAP_TYPES = {"cap.skill", "cap.cli", "cap.mcp", "cap.env", "cap.plugin"}
NODE_TYPES = EXEC_TYPES | CAP_TYPES

MAX_NODES = 60
MAX_EDGES = 150
MAX_OUTPUT_CHARS = 40_000
RUNS_KEPT_PER_WORKFLOW = 25
STEP_TIMEOUT = 900          # one agent step
APPROVAL_TIMEOUT = 24 * 3600

_lock = threading.Lock()          # workflow docs
_runs_lock = threading.Lock()     # run files (engine + approve/cancel flags)

# Channel identity env prefixes → channel name (mirrors the per-agent
# credential prefixes the sharing layer refuses to inherit).
_ENV_CHANNELS = {
    "TELEGRAM_": "telegram", "DISCORD_": "discord", "SLACK_": "slack",
    "WHATSAPP_": "whatsapp", "SIGNAL_": "signal", "MATRIX_": "matrix",
    "TEAMS_": "teams", "GOOGLE_CHAT_": "googlechat", "EMAIL_": "email",
    "IMESSAGE_": "imessage", "SMS_": "sms",
}


def _ensure_dirs() -> None:
    WORKFLOWS_DIR.mkdir(exist_ok=True)
    RUNS_DIR.mkdir(exist_ok=True)


# ─── workflow documents ──────────────────────────────────────────────────────

def _wf_path(wf_id: str) -> Path:
    if not ID_RE.match(wf_id):
        raise ValueError("Bad workflow id")
    return WORKFLOWS_DIR / f"{wf_id}.json"


def load_workflow(wf_id: str) -> Dict[str, Any]:
    path = _wf_path(wf_id)
    if not path.exists():
        raise ValueError(f"No such workflow: {wf_id}")
    return json.loads(path.read_text())


def _validate_doc(doc: Dict[str, Any]) -> None:
    nodes = doc.get("nodes")
    edges = doc.get("edges")
    if not isinstance(nodes, list) or not isinstance(edges, list):
        raise ValueError("nodes and edges must be lists")
    if len(nodes) > MAX_NODES or len(edges) > MAX_EDGES:
        raise ValueError("Workflow too large")
    seen = set()
    for n in nodes:
        nid = n.get("id", "")
        if not isinstance(nid, str) or not ID_RE.match(nid) or nid in seen:
            raise ValueError(f"Bad or duplicate node id: {nid!r}")
        seen.add(nid)
        if n.get("type") not in NODE_TYPES:
            raise ValueError(f"Unknown node type: {n.get('type')!r}")
        if not isinstance(n.get("config", {}), dict):
            raise ValueError("node.config must be an object")
        n.setdefault("config", {})
        n["x"] = float(n.get("x", 0))
        n["y"] = float(n.get("y", 0))
    by_id = {n["id"]: n for n in nodes}
    norm_edges = []
    for e in edges:
        a, b = e.get("from"), e.get("to")
        if a not in by_id or b not in by_id or a == b:
            raise ValueError(f"Edge references unknown node: {a!r} → {b!r}")
        kind = e.get("kind") or "flow"
        if kind == "flow":
            if by_id[a]["type"] in CAP_TYPES or by_id[b]["type"] in CAP_TYPES:
                raise ValueError("flow edges may only join executable nodes")
        elif kind == "cap":
            # normalize: capability → step
            if by_id[b]["type"] in CAP_TYPES:
                a, b = b, a
            if by_id[a]["type"] not in CAP_TYPES or by_id[b]["type"] != "step.agent":
                raise ValueError("cap edges join a capability to an agent step")
        else:
            raise ValueError(f"Unknown edge kind: {kind!r}")
        norm_edges.append({"from": a, "to": b, "kind": kind})
    doc["edges"] = norm_edges
    _topo_order(doc)      # raises on flow cycles


def list_workflows() -> List[Dict[str, Any]]:
    _ensure_dirs()
    out = []
    for path in sorted(WORKFLOWS_DIR.glob("*.json")):
        try:
            doc = json.loads(path.read_text())
        except Exception:
            continue
        runs = list_runs(doc["id"], limit=1)
        out.append({
            "id": doc["id"], "name": doc.get("name", doc["id"]),
            "description": doc.get("description", ""),
            "nodes": len(doc.get("nodes", [])),
            "updated_at": doc.get("updated_at", 0),
            "last_run": ({"status": runs[0]["status"], "ts": runs[0]["started"]}
                         if runs else None),
        })
    out.sort(key=lambda w: -w["updated_at"])
    return out


def create_workflow(name: str, description: str = "") -> Dict[str, Any]:
    _ensure_dirs()
    name = (name or "").strip() or "Untitled workflow"
    wf_id = f"wf-{secrets.token_hex(4)}"
    doc = {
        "id": wf_id, "name": name[:80], "description": description[:400],
        "nodes": [{"id": "trigger", "type": "trigger.manual",
                   "x": 0, "y": 0, "config": {}}],
        "edges": [],
        "hook_secret": secrets.token_urlsafe(16),
        "created_at": time.time(), "updated_at": time.time(),
        "updated_by": "ui",
    }
    with _lock:
        _wf_path(wf_id).write_text(json.dumps(doc, indent=1))
    return doc


def save_workflow(wf_id: str, doc: Dict[str, Any],
                  updated_by: str = "ui") -> Dict[str, Any]:
    with _lock:
        cur = load_workflow(wf_id)
        new = {
            **cur,
            "name": str(doc.get("name", cur["name"]))[:80],
            "description": str(doc.get("description", cur.get("description", "")))[:400],
            "nodes": doc.get("nodes", cur["nodes"]),
            "edges": doc.get("edges", cur["edges"]),
            "updated_at": time.time(),
            "updated_by": updated_by,
        }
        _validate_doc(new)
        _wf_path(wf_id).write_text(json.dumps(new, indent=1))
        return new


def delete_workflow(wf_id: str) -> None:
    with _lock:
        path = _wf_path(wf_id)
        if path.exists():
            path.unlink()
    for run in RUNS_DIR.glob(f"run-{wf_id}-*.json"):
        run.unlink(missing_ok=True)


# ─── palette resources ───────────────────────────────────────────────────────

def agent_channels(agent: Dict[str, Any]) -> List[str]:
    """Channels an agent has linked: platform config sections + channel
    credential prefixes in its own .env (channels are always per-agent)."""
    home = Path(agent["home"])
    found = set()
    try:
        import yaml
        cfg = yaml.safe_load((home / "config.yaml").read_text()) or {}
        found.update(k for k in (cfg.get("platforms") or {}))
        if cfg.get("whatsapp"):
            found.add("whatsapp")
    except Exception:
        pass
    try:
        for key in orc._read_env_keys(home / ".env"):
            for prefix, channel in _ENV_CHANNELS.items():
                if key.startswith(prefix):
                    found.add(channel)
    except Exception:
        pass
    return sorted(found)


# Shared env keys that indicate a provider has credentials fleet-wide.
_PROVIDER_ENV_HINTS = {
    "anthropic": ("ANTHROPIC_API_KEY",),
    "openai": ("OPENAI_API_KEY",),
    "openrouter": ("OPENROUTER_API_KEY",),
    "deepseek": ("DEEPSEEK_API_KEY",),
    "gemini": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
    "groq": ("GROQ_API_KEY",),
    "xai": ("XAI_API_KEY",),
    "mistral": ("MISTRAL_API_KEY",),
    "moonshot": ("MOONSHOT_API_KEY",),
}


def model_options(shared: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Flat model list for the UI/builder: id is the per-node 'model' value.
    ``ready`` marks providers with fleet-wide credentials (OAuth in the
    shared auth.json or an API key in the shared env)."""
    authed = set(shared.get("providers") or [])
    env_keys = set(shared.get("env_keys") or [])
    out = []
    for prov, models in orc.model_catalog().items():
        ready = (prov in authed
                 or any(k in env_keys for k in _PROVIDER_ENV_HINTS.get(prov, ())))
        for mid in models:
            alias = mid if mid.startswith(f"{prov}/") else f"{prov}/{mid}"
            out.append({"id": alias, "provider": prov, "model": mid,
                        "ready": ready})
    out.sort(key=lambda m: (not m["ready"], m["id"]))
    return out


def resources() -> Dict[str, Any]:
    """Everything the palette can drag onto the canvas."""
    reg = orc.load_registry()
    shared = orc.shared_summary()
    agents, channels = [], []
    for name in sorted(reg["agents"]):
        agent = reg["agents"][name]
        has_api = bool(agent.get("api_port"))
        agents.append({
            "name": name,
            "description": agent.get("description", "")[:160],
            "api": has_api,
            "running": orc.is_running(name),
            "model": orc.agent_default_model(agent),
        })
        if has_api:
            for ch in agent_channels(agent):
                channels.append({"agent": name, "channel": ch})
    return {
        "agents": agents,
        "channels": channels,
        "models": model_options(shared),
        "skills": shared["skills"],
        "clis": [t["name"] for t in orc.list_clis()],
        "mcp_servers": shared["mcp_servers"],
        "env_keys": shared["env_keys"],
        "plugins": shared.get("plugins", []),
    }


# ─── runs ────────────────────────────────────────────────────────────────────

def _run_path(run_id: str) -> Path:
    if not re.match(r"^run-[a-zA-Z0-9_-]+$", run_id):
        raise ValueError("Bad run id")
    return RUNS_DIR / f"{run_id}.json"


def load_run(run_id: str) -> Dict[str, Any]:
    path = _run_path(run_id)
    if not path.exists():
        raise ValueError(f"No such run: {run_id}")
    with _runs_lock:
        return json.loads(path.read_text())


def list_runs(wf_id: str, limit: int = 20) -> List[Dict[str, Any]]:
    _ensure_dirs()
    if not ID_RE.match(wf_id):
        raise ValueError("Bad workflow id")
    runs = []
    for path in RUNS_DIR.glob(f"run-{wf_id}-*.json"):
        try:
            r = json.loads(path.read_text())
            runs.append({"id": r["id"], "status": r["status"],
                         "trigger": r.get("trigger", "manual"),
                         "started": r["started"], "finished": r.get("finished")})
        except Exception:
            continue
    runs.sort(key=lambda r: -r["started"])
    return runs[:limit]


def _save_run(run: Dict[str, Any]) -> None:
    """Persist the engine's run state, preserving flags (approval decisions,
    cancel) that the API may have written to the file meanwhile."""
    with _runs_lock:
        path = _run_path(run["id"])
        if path.exists():
            try:
                disk = json.loads(path.read_text())
                if disk.get("cancel"):
                    run["cancel"] = True
                for nid, dn in (disk.get("nodes") or {}).items():
                    if "decision" in dn:
                        run["nodes"].setdefault(nid, {})["decision"] = dn["decision"]
            except Exception:
                pass
        path.write_text(json.dumps(run, indent=1))


def _flag_run(run_id: str, mutate) -> Dict[str, Any]:
    """Read-modify-write a run file for API-side flags."""
    with _runs_lock:
        path = _run_path(run_id)
        run = json.loads(path.read_text())
        mutate(run)
        path.write_text(json.dumps(run, indent=1))
        return run


def approve_run(run_id: str, node_id: str, approve: bool) -> Dict[str, Any]:
    def _mut(run):
        node = run["nodes"].setdefault(node_id, {})
        node["decision"] = "approved" if approve else "rejected"
    return _flag_run(run_id, _mut)


def cancel_run(run_id: str) -> Dict[str, Any]:
    return _flag_run(run_id, lambda run: run.update(cancel=True))


def _prune_runs(wf_id: str) -> None:
    paths = sorted(RUNS_DIR.glob(f"run-{wf_id}-*.json"),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    for path in paths[RUNS_KEPT_PER_WORKFLOW:]:
        path.unlink(missing_ok=True)


# ─── graph helpers ───────────────────────────────────────────────────────────

def _topo_order(doc: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Executable nodes in dependency order (Kahn). Raises on cycles."""
    nodes = [n for n in doc["nodes"] if n["type"] in EXEC_TYPES]
    ids = {n["id"] for n in nodes}
    flow = [e for e in doc["edges"] if (e.get("kind") or "flow") == "flow"
            and e["from"] in ids and e["to"] in ids]
    indeg = {n["id"]: 0 for n in nodes}
    for e in flow:
        indeg[e["to"]] += 1
    # stable order: triggers first, then insertion order
    queue = [n for n in nodes if indeg[n["id"]] == 0]
    order, done = [], set()
    while queue:
        n = queue.pop(0)
        order.append(n)
        done.add(n["id"])
        for e in flow:
            if e["from"] == n["id"]:
                indeg[e["to"]] -= 1
                if indeg[e["to"]] == 0:
                    queue.append(next(x for x in nodes if x["id"] == e["to"]))
    if len(order) != len(nodes):
        raise ValueError("Workflow contains a cycle")
    return order


def node_label(node: Dict[str, Any]) -> str:
    cfg = node.get("config", {})
    if cfg.get("title"):
        return str(cfg["title"])[:60]
    t = node["type"]
    if t == "step.agent":
        return cfg.get("agent") or "agent step"
    if t == "out.channel":
        return f"{cfg.get('channel', 'channel')} via {cfg.get('agent', '?')}"
    if t == "out.webhook":
        return "webhook out"
    if t == "gate.approval":
        return "human approval"
    if t.startswith("cap."):
        return cfg.get("name", t.split(".", 1)[1])
    return {"trigger.manual": "manual trigger", "trigger.cron": "schedule",
            "trigger.webhook": "webhook trigger"}.get(t, t)


def _caps_for_step(doc: Dict[str, Any], step_id: str) -> List[Dict[str, Any]]:
    by_id = {n["id"]: n for n in doc["nodes"]}
    return [by_id[e["from"]] for e in doc["edges"]
            if e.get("kind") == "cap" and e["to"] == step_id
            and e["from"] in by_id]


# ─── agent invocation ────────────────────────────────────────────────────────

def _call_agent(agent: Dict[str, Any], prompt: str, session: str,
                timeout: int = STEP_TIMEOUT, model: Optional[str] = None) -> str:
    body = json.dumps({
        # A "provider/model" value matching one of the agent's model_routes
        # runs this request on that model; anything else uses its default.
        "model": model or "hermes-agent",
        "messages": [{"role": "user", "content": prompt}],
    }).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{agent['api_port']}/v1/chat/completions",
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {agent['api_key']}",
            "X-Hermes-Session-Id": session,
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read())
    return data["choices"][0]["message"]["content"]


def _agent_model_aliases(agent: Dict[str, Any]) -> Optional[set]:
    """Model aliases the agent's API server accepts (GET /v1/models), or
    None when the list can't be fetched (callers then skip the check)."""
    req = urllib.request.Request(
        f"http://127.0.0.1:{agent['api_port']}/v1/models",
        headers={"Authorization": f"Bearer {agent['api_key']}"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        items = data.get("data") if isinstance(data, dict) else data
        return {m.get("id") if isinstance(m, dict) else str(m) for m in items or []}
    except Exception:
        return None


def _check_model(agent_name: str, agent: Dict[str, Any], model: str) -> None:
    """Fail fast when a per-node model isn't routed on the agent — otherwise
    the gateway silently ignores unknown model values and the step would run
    on the default model while claiming otherwise."""
    aliases = _agent_model_aliases(agent)
    if aliases is not None and model not in aliases:
        raise RuntimeError(
            f'Agent "{agent_name}" has no model route for "{model}". Restart '
            "the agent to refresh its routes (the workspace writes them at "
            "start), or pick a model from the step's model list.")


def _get_agent(name: str) -> Dict[str, Any]:
    agent = orc.load_registry()["agents"].get(name or "")
    if not agent:
        raise RuntimeError(f"Agent '{name}' does not exist")
    if not agent.get("api_port"):
        raise RuntimeError(f"Agent '{name}' has no API server to call")
    if not orc.is_running(name):
        raise RuntimeError(f"Agent '{name}' is not running — start it first")
    return agent


_CAP_HINTS = {
    "cap.skill": 'Use the shared skill "{name}" (installed in your skills folder — read and follow it).',
    "cap.cli": 'Use the shared CLI tool "{name}" (on your PATH; its manifest is in your clis folder).',
    "cap.mcp": 'Use the MCP server "{name}" (configured in your config).',
    "cap.env": 'The environment variable "{name}" is set in your environment for this task.',
    "cap.plugin": 'Use the plugin "{name}" (installed in your shared plugins).',
}


def _strip_fences(text: str) -> str:
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    return (m.group(1) if m else text).strip()


def _step_prompt(wf: Dict[str, Any], node: Dict[str, Any],
                 inputs: List[tuple], caps: List[Dict[str, Any]]) -> str:
    cfg = node.get("config", {})
    parts = [
        f'You are executing the step "{node_label(node)}" of the automated '
        f'workflow "{wf["name"]}". Work autonomously — no human is in this '
        "conversation.",
        "\n## Your task\n" + (cfg.get("instruction") or "Process the input and produce a useful result."),
    ]
    if caps:
        parts.append("\n## Capabilities to use for this step\n" + "\n".join(
            "- " + _CAP_HINTS[c["type"]].format(name=c.get("config", {}).get("name", "?"))
            for c in caps if c["type"] in _CAP_HINTS))
    if inputs:
        blocks = [f"### From “{label}”\n{out or '(empty)'}" for label, out in inputs]
        parts.append("\n## Input from previous steps\n" + "\n\n".join(blocks))
    if cfg.get("output") == "json":
        fields = cfg.get("json_fields") or "result"
        parts.append(f"\n## Output format\nReply with ONLY a valid JSON object "
                     f"with these fields: {fields}. No markdown, no commentary.")
    else:
        parts.append("\n## Output rules\nReply ONLY with this step's result — "
                     "no preamble or commentary. Your entire reply is passed "
                     "verbatim to the next step of the workflow.")
    return "\n".join(parts)


def _channel_prompt(node: Dict[str, Any], message: str) -> str:
    cfg = node.get("config", {})
    channel = cfg.get("channel", "?")
    target = (cfg.get("target") or "").strip()
    where = (f'Send it to: {target}.' if target
             else "Send it to your default/home chat for that channel.")
    return (f"Deliver a message through your linked **{channel}** channel now, "
            f"using your {channel} message-sending tool. {where}\n"
            "Deliver the content below (format it appropriately for the "
            "channel, but do not change its meaning):\n"
            f"---\n{message}\n---\n"
            "After it is sent, reply with exactly: SENT")


# ─── the engine ──────────────────────────────────────────────────────────────

def start_run(wf_id: str, trigger: str = "manual",
              payload: str = "") -> Dict[str, Any]:
    _ensure_dirs()
    wf = load_workflow(wf_id)
    order = _topo_order(wf)
    if not any(n["type"] == "step.agent" for n in order):
        raise ValueError("Add at least one agent step before running")
    run = {
        "id": f"run-{wf_id}-{int(time.time())}-{secrets.token_hex(3)}",
        "workflow_id": wf_id, "workflow_name": wf["name"],
        "status": "running", "trigger": trigger,
        "started": time.time(), "finished": None,
        "nodes": {n["id"]: {"status": "pending"} for n in order},
    }
    _save_run(run)
    threading.Thread(target=_execute, args=(wf, run, payload),
                     daemon=True, name=run["id"]).start()
    _prune_runs(wf_id)
    return run


def _execute(wf: Dict[str, Any], run: Dict[str, Any], payload: str) -> None:
    order = _topo_order(wf)
    flow = [e for e in wf["edges"] if (e.get("kind") or "flow") == "flow"]
    by_id = {n["id"]: n for n in wf["nodes"]}
    outputs: Dict[str, str] = {}

    def fail(node: Dict[str, Any], err: str) -> None:
        nr = run["nodes"][node["id"]]
        nr.update(status="failed", error=str(err)[:2000], finished=time.time())
        run.update(status="failed", finished=time.time(),
                   error=f'{node_label(node)}: {str(err)[:300]}')
        _save_run(run)
        agent_name = node.get("config", {}).get("agent") or "workflow"
        inc = orc.record_incident(
            agent_name, "workflow",
            f'Workflow "{wf["name"]}" ({wf["id"]}) failed at node '
            f'"{node_label(node)}" ({node["type"]}): {str(err)[:1200]}',
            f"run {run['id']} marked failed")
        if inc:
            orc.dispatch_to_fixer(inc)

    for node in order:
        # refresh API-side flags (cancel / approval decisions)
        _save_run(run)
        run.update(load_run(run["id"]))
        if run.get("cancel"):
            run.update(status="cancelled", finished=time.time())
            _save_run(run)
            return

        nr = run["nodes"][node["id"]]
        nr.update(status="running", started=time.time())
        _save_run(run)
        inputs = [(node_label(by_id[e["from"]]), outputs.get(e["from"], ""))
                  for e in flow if e["to"] == node["id"] and e["from"] in outputs]
        try:
            if node["type"].startswith("trigger."):
                outputs[node["id"]] = payload or ""
                nr["output"] = (payload or "")[:MAX_OUTPUT_CHARS]

            elif node["type"] == "step.agent":
                cfg = node.get("config", {})
                agent = _get_agent(cfg.get("agent"))
                model = (cfg.get("model") or "").strip() or None
                if model:
                    _check_model(cfg.get("agent"), agent, model)
                    nr["model"] = model
                caps = _caps_for_step(wf, node["id"])
                prompt = _step_prompt(wf, node, inputs, caps)
                session = f"wf-{wf['id']}-{run['id'][-10:]}-{node['id']}"
                reply = _call_agent(agent, prompt, session, model=model)
                if cfg.get("output") == "json":
                    try:
                        json.loads(_strip_fences(reply))
                        reply = _strip_fences(reply)
                    except Exception:
                        reply = _call_agent(agent, prompt +
                            "\n\nYour previous reply was not valid JSON. "
                            "Reply again with ONLY the JSON object.\n"
                            f"Previous reply:\n{reply[:4000]}", session,
                            model=model)
                        reply = _strip_fences(reply)
                        json.loads(reply)      # raises → fail()
                outputs[node["id"]] = reply
                nr["output"] = reply[:MAX_OUTPUT_CHARS]

            elif node["type"] == "gate.approval":
                combined = "\n\n".join(o for _, o in inputs)
                nr["output"] = combined[:MAX_OUTPUT_CHARS]
                nr["status"] = "waiting"
                run["status"] = "waiting"
                _save_run(run)
                decision = _wait_for_decision(run, node["id"])
                if decision != "approved":
                    run.update(
                        status="cancelled" if decision == "cancelled" else "rejected",
                        finished=time.time())
                    nr.update(status=decision, finished=time.time())
                    _save_run(run)
                    return
                run["status"] = "running"
                outputs[node["id"]] = combined

            elif node["type"] == "out.channel":
                cfg = node.get("config", {})
                agent = _get_agent(cfg.get("agent"))
                message = "\n\n".join(o for _, o in inputs) or "(workflow produced no output)"
                session = f"wf-{wf['id']}-{run['id'][-10:]}-{node['id']}"
                reply = _call_agent(agent, _channel_prompt(node, message), session)
                outputs[node["id"]] = message
                nr["output"] = f"delivery reply: {reply[:500]}"

            elif node["type"] == "out.webhook":
                url = (node.get("config", {}).get("url") or "").strip()
                if not url.startswith(("http://", "https://")):
                    raise RuntimeError("out.webhook needs an http(s) url")
                message = "\n\n".join(o for _, o in inputs)
                body = json.dumps({"workflow": wf["name"], "workflow_id": wf["id"],
                                   "run_id": run["id"], "output": message}).encode()
                req = urllib.request.Request(
                    url, data=body, headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=60) as resp:
                    nr["output"] = f"HTTP {resp.status}"
                outputs[node["id"]] = message

            nr.update(status=nr.get("status", "running"), finished=time.time())
            if nr["status"] in ("running", "waiting"):
                nr["status"] = "done"
            _save_run(run)
        except Exception as exc:               # noqa: BLE001 — reported per node
            fail(node, exc)
            return

    run.update(status="success", finished=time.time())
    _save_run(run)


def _wait_for_decision(run: Dict[str, Any], node_id: str) -> str:
    deadline = time.time() + APPROVAL_TIMEOUT
    while time.time() < deadline:
        fresh = load_run(run["id"])
        if fresh.get("cancel"):
            return "cancelled"
        decision = (fresh["nodes"].get(node_id) or {}).get("decision")
        if decision in ("approved", "rejected"):
            run["nodes"][node_id]["decision"] = decision
            return decision
        time.sleep(2)
    return "rejected"


# ─── cron scheduler ──────────────────────────────────────────────────────────

def _field_match(field: str, value: int, lo: int, hi: int) -> bool:
    for part in field.split(","):
        part, step = part.strip(), 1
        if "/" in part:
            part, s = part.split("/", 1)
            step = int(s)
        if part in ("*", ""):
            rng = range(lo, hi + 1, step)
        elif "-" in part:
            a, b = part.split("-", 1)
            rng = range(int(a), int(b) + 1, step)
        elif step > 1:
            rng = range(int(part), hi + 1, step)
        else:
            rng = range(int(part), int(part) + 1)
        if value in rng:
            return True
    return False


def cron_match(expr: str, t: Optional[time.struct_time] = None) -> bool:
    t = t or time.localtime()
    fields = expr.split()
    if len(fields) != 5:
        raise ValueError("cron needs 5 fields: min hour dom mon dow")
    values = [t.tm_min, t.tm_hour, t.tm_mday, t.tm_mon, (t.tm_wday + 1) % 7]
    bounds = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 6)]
    return all(_field_match(f, v, lo, hi)
               for f, v, (lo, hi) in zip(fields, values, bounds))


_fired: Dict[str, str] = {}       # "<wf>/<node>" → last fired minute key


def _scheduler_tick() -> None:
    minute = time.strftime("%Y%m%d%H%M")
    for meta in list_workflows():
        try:
            wf = load_workflow(meta["id"])
        except Exception:
            continue
        for node in wf.get("nodes", []):
            if node.get("type") != "trigger.cron":
                continue
            key = f"{wf['id']}/{node['id']}"
            expr = (node.get("config", {}).get("schedule") or "").strip()
            if not expr or _fired.get(key) == minute:
                continue
            try:
                if cron_match(expr):
                    _fired[key] = minute
                    start_run(wf["id"], trigger="cron")
            except ValueError:
                continue      # bad expression — visible in the UI, not fatal


def start_scheduler(interval: int = 20) -> threading.Thread:
    def _loop() -> None:
        while True:
            try:
                _scheduler_tick()
            except Exception:
                pass
            time.sleep(interval)
    t = threading.Thread(target=_loop, daemon=True, name="workflow-cron")
    t.start()
    return t


# ─── workflow-builder chat ───────────────────────────────────────────────────

BUILDER_AGENT = "workflow-builder"

_CHAT_CONTEXT = """The user is editing the workflow "{name}" (id: {wf_id}) on
the canvas and talks to you through the chat panel.

Current workflow document:
```json
{doc}
```

Available fleet resources (agents, channels, skills, CLI tools, MCP servers,
env keys, plugins) — never reference anything not in this list:
```json
{res}
```

Reply for the user in 2-5 plain sentences. If (and only if) the workflow
should change, ALSO include the complete updated document as a single fenced
block in this exact form — the orchestrator applies it directly, do NOT use
any tools, terminal or API calls:
```json
{{"name": ..., "description": ..., "nodes": [...], "edges": [...]}}
```
The block replaces the whole document, so always include every node and edge
that should remain, with stable ids and positions.

User message:
{message}"""

_BUILDER_RETRY = """Your workflow document was rejected by validation:
{err}

Reply again with the corrected COMPLETE document in one ```json fenced block
(plus one short sentence for the user)."""


def _extract_doc(reply: str):
    """Split a builder reply into (workflow doc or None, message text)."""
    for m in re.finditer(r"```(?:json)?\s*(\{.*?\})\s*```", reply, re.S):
        try:
            obj = json.loads(m.group(1))
        except Exception:
            continue
        if isinstance(obj, dict) and "nodes" in obj:
            text = (reply[:m.start()] + reply[m.end():]).strip()
            return obj, text
    return None, reply.strip()


def builder_chat(wf_id: str, message: str) -> str:
    agent = _get_agent(BUILDER_AGENT)
    wf = load_workflow(wf_id)
    prompt = _CHAT_CONTEXT.format(
        name=wf["name"], wf_id=wf_id,
        doc=json.dumps({k: wf[k] for k in ("id", "name", "description",
                                           "nodes", "edges")}, indent=1),
        res=json.dumps(resources(), indent=1),
        message=message)
    session = f"wf-builder-{wf_id}"
    reply = _call_agent(agent, prompt, session=session, timeout=600)
    doc, text = _extract_doc(reply)
    if not doc:
        return text
    try:
        save_workflow(wf_id, doc, updated_by=BUILDER_AGENT)
    except ValueError as err:
        reply = _call_agent(agent, _BUILDER_RETRY.format(err=err),
                            session=session, timeout=600)
        doc, text = _extract_doc(reply)
        if not doc:
            return text
        try:
            save_workflow(wf_id, doc, updated_by=BUILDER_AGENT)
        except ValueError as err2:
            return f"{text}\n\n⚠ I could not apply the change — the document " \
                   f"was rejected: {err2}"
    return text or "Done — the canvas is updated."
