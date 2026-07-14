"""Workflow engine tests — run against a stub agent API server."""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

import orchestrator as orc
import workflows as wfl


# ─── stub agent: an OpenAI-compatible /v1/chat/completions echo server ──────

class _StubHandler(BaseHTTPRequestHandler):
    prompts = []          # class-level capture
    reply = "stub reply"

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        _StubHandler.prompts.append(body["messages"][0]["content"])
        payload = json.dumps({
            "choices": [{"message": {"content": _StubHandler.reply}}]
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *a):
        pass


@pytest.fixture
def stub_agent():
    server = HTTPServer(("127.0.0.1", 0), _StubHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    _StubHandler.prompts = []
    _StubHandler.reply = "stub reply"
    yield server.server_address[1]
    server.shutdown()


@pytest.fixture
def env(tmp_path, monkeypatch, stub_agent):
    """Isolated workflow dirs + a registry with one running stub agent."""
    monkeypatch.setattr(wfl, "WORKFLOWS_DIR", tmp_path / "workflows")
    monkeypatch.setattr(wfl, "RUNS_DIR", tmp_path / "runs")
    registry = {"agents": {
        "alpha": {"home": str(tmp_path / "alpha"), "api_port": stub_agent,
                  "api_key": "k", "description": "stub"},
        "beta": {"home": str(tmp_path / "beta"), "api_port": stub_agent,
                 "api_key": "k", "description": "stub"},
    }}
    monkeypatch.setattr(orc, "load_registry", lambda: registry)
    monkeypatch.setattr(orc, "is_running", lambda name: True)
    incidents = []
    monkeypatch.setattr(orc, "record_incident",
                        lambda *a, **k: incidents.append(a) or {"id": 1})
    monkeypatch.setattr(orc, "dispatch_to_fixer", lambda inc: None)
    return {"incidents": incidents}


def _wait(run_id, want, timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        run = wfl.load_run(run_id)
        if run["status"] in want:
            return run
        time.sleep(0.05)
    raise AssertionError(f"run stuck in {wfl.load_run(run_id)['status']}")


# ─── document validation ─────────────────────────────────────────────────────

def test_create_and_save(env):
    wf = wfl.create_workflow("My flow", "desc")
    assert wf["nodes"][0]["type"] == "trigger.manual"
    doc = wfl.load_workflow(wf["id"])
    doc["nodes"].append({"id": "s1", "type": "step.agent", "x": 1, "y": 2,
                         "config": {"agent": "alpha"}})
    doc["edges"].append({"from": "trigger", "to": "s1", "kind": "flow"})
    saved = wfl.save_workflow(wf["id"], doc)
    assert len(saved["nodes"]) == 2
    assert wfl.list_workflows()[0]["nodes"] == 2


def test_validation_rejects_garbage(env):
    wf = wfl.create_workflow("bad")
    doc = wfl.load_workflow(wf["id"])
    doc["nodes"].append({"id": "x", "type": "step.teleport", "config": {}})
    with pytest.raises(ValueError, match="Unknown node type"):
        wfl.save_workflow(wf["id"], doc)

    doc = wfl.load_workflow(wf["id"])
    doc["nodes"] += [{"id": "a", "type": "step.agent", "config": {}},
                     {"id": "b", "type": "step.agent", "config": {}}]
    doc["edges"] = [{"from": "a", "to": "b"}, {"from": "b", "to": "a"}]
    with pytest.raises(ValueError, match="cycle"):
        wfl.save_workflow(wf["id"], doc)


def test_cap_edge_normalized_and_flow_guarded(env):
    wf = wfl.create_workflow("caps")
    doc = wfl.load_workflow(wf["id"])
    doc["nodes"] += [{"id": "s1", "type": "step.agent", "config": {"agent": "alpha"}},
                     {"id": "c1", "type": "cap.skill", "config": {"name": "research"}}]
    # reversed direction gets normalized to cap → step
    doc["edges"] = [{"from": "s1", "to": "c1", "kind": "cap"}]
    saved = wfl.save_workflow(wf["id"], doc)
    assert saved["edges"][0] == {"from": "c1", "to": "s1", "kind": "cap"}
    # capability nodes cannot join the flow
    doc = wfl.load_workflow(wf["id"])
    doc["edges"].append({"from": "c1", "to": "s1", "kind": "flow"})
    with pytest.raises(ValueError, match="flow edges"):
        wfl.save_workflow(wf["id"], doc)


# ─── cron ────────────────────────────────────────────────────────────────────

def test_cron_match():
    t = time.strptime("2026-07-14 09:30", "%Y-%m-%d %H:%M")  # a Tuesday
    assert wfl.cron_match("30 9 * * *", t)
    assert wfl.cron_match("*/15 * * * *", t)
    assert wfl.cron_match("30 9 14 7 2", t)
    assert wfl.cron_match("0-45 8-10 * * 1-5", t)
    assert not wfl.cron_match("31 9 * * *", t)
    assert not wfl.cron_match("30 9 * * 0", t)
    with pytest.raises(ValueError):
        wfl.cron_match("* * *", t)


# ─── execution ───────────────────────────────────────────────────────────────

def _two_step_flow(caps=False):
    wf = wfl.create_workflow("chain")
    doc = wfl.load_workflow(wf["id"])
    doc["nodes"] += [
        {"id": "s1", "type": "step.agent",
         "config": {"agent": "alpha", "title": "Research",
                    "instruction": "find things"}},
        {"id": "s2", "type": "step.agent",
         "config": {"agent": "beta", "instruction": "write from research"}},
    ]
    doc["edges"] = [{"from": "trigger", "to": "s1", "kind": "flow"},
                    {"from": "s1", "to": "s2", "kind": "flow"}]
    if caps:
        doc["nodes"].append({"id": "c1", "type": "cap.skill",
                             "config": {"name": "deep-research"}})
        doc["edges"].append({"from": "c1", "to": "s1", "kind": "cap"})
    return wfl.save_workflow(wf["id"], doc)


def test_run_chains_outputs(env):
    wf = _two_step_flow(caps=True)
    _StubHandler.reply = "RESEARCH RESULT"
    run = wfl.start_run(wf["id"], payload="topic: hermes")
    run = _wait(run["id"], {"success"})
    assert [n["status"] for n in run["nodes"].values()] == ["done"] * 3
    assert run["nodes"]["s1"]["output"] == "RESEARCH RESULT"
    # step 1 saw the trigger payload and its capability hint
    assert "topic: hermes" in _StubHandler.prompts[0]
    assert "deep-research" in _StubHandler.prompts[0]
    # step 2 received step 1's output, labeled with the step title
    assert "RESEARCH RESULT" in _StubHandler.prompts[1]
    assert "Research" in _StubHandler.prompts[1]


def test_run_failure_records_incident(env):
    wf = wfl.create_workflow("broken")
    doc = wfl.load_workflow(wf["id"])
    doc["nodes"].append({"id": "s1", "type": "step.agent",
                         "config": {"agent": "ghost"}})
    doc["edges"] = [{"from": "trigger", "to": "s1", "kind": "flow"}]
    wfl.save_workflow(wf["id"], doc)
    run = wfl.start_run(wf["id"])
    run = _wait(run["id"], {"failed"})
    assert "ghost" in run["error"]
    assert env["incidents"], "failure must raise an incident"


def test_approval_gate(env):
    wf = wfl.create_workflow("gated")
    doc = wfl.load_workflow(wf["id"])
    doc["nodes"] += [
        {"id": "s1", "type": "step.agent", "config": {"agent": "alpha"}},
        {"id": "g1", "type": "gate.approval", "config": {}},
        {"id": "s2", "type": "step.agent", "config": {"agent": "beta"}},
    ]
    doc["edges"] = [{"from": "trigger", "to": "s1", "kind": "flow"},
                    {"from": "s1", "to": "g1", "kind": "flow"},
                    {"from": "g1", "to": "s2", "kind": "flow"}]
    wfl.save_workflow(wf["id"], doc)

    run = wfl.start_run(wf["id"])
    run = _wait(run["id"], {"waiting"})
    assert run["nodes"]["g1"]["status"] == "waiting"
    wfl.approve_run(run["id"], "g1", True)
    run = _wait(run["id"], {"success"})
    assert run["nodes"]["s2"]["status"] == "done"

    run2 = wfl.start_run(wf["id"])
    _wait(run2["id"], {"waiting"})
    wfl.approve_run(run2["id"], "g1", False)
    run2 = _wait(run2["id"], {"rejected"})
    assert run2["nodes"]["s2"]["status"] == "pending"


def test_json_output_retry(env):
    wf = wfl.create_workflow("json")
    doc = wfl.load_workflow(wf["id"])
    doc["nodes"].append({"id": "s1", "type": "step.agent",
                         "config": {"agent": "alpha", "output": "json",
                                    "json_fields": "title"}})
    doc["edges"] = [{"from": "trigger", "to": "s1", "kind": "flow"}]
    wfl.save_workflow(wf["id"], doc)
    _StubHandler.reply = '```json\n{"title": "ok"}\n```'
    run = wfl.start_run(wf["id"])
    run = _wait(run["id"], {"success"})
    assert json.loads(run["nodes"]["s1"]["output"]) == {"title": "ok"}


def test_webhook_out_and_cancel(env, stub_agent):
    received = []

    class Hook(_StubHandler):
        def do_POST(self):
            received.append(json.loads(
                self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")

    hook_srv = HTTPServer(("127.0.0.1", 0), Hook)
    threading.Thread(target=hook_srv.serve_forever, daemon=True).start()

    wf = wfl.create_workflow("hooky")
    doc = wfl.load_workflow(wf["id"])
    doc["nodes"] += [
        {"id": "s1", "type": "step.agent", "config": {"agent": "alpha"}},
        {"id": "w1", "type": "out.webhook",
         "config": {"url": f"http://127.0.0.1:{hook_srv.server_address[1]}/x"}},
    ]
    doc["edges"] = [{"from": "trigger", "to": "s1", "kind": "flow"},
                    {"from": "s1", "to": "w1", "kind": "flow"}]
    wfl.save_workflow(wf["id"], doc)
    _StubHandler.reply = "final text"
    run = wfl.start_run(wf["id"])
    run = _wait(run["id"], {"success"})
    hook_srv.shutdown()
    assert received and received[0]["output"] == "final text"
    assert received[0]["workflow"] == "hooky"


# ─── builder chat (no-tools JSON-block contract) ─────────────────────────────

def test_extract_doc():
    doc, text = wfl._extract_doc(
        'Built it!\n```json\n{"name": "x", "nodes": [], "edges": []}\n```\nEnjoy.')
    assert doc["name"] == "x" and "nodes" in doc
    assert "Built it!" in text and "```" not in text
    none_doc, text2 = wfl._extract_doc("Just chatting, no changes.")
    assert none_doc is None and text2 == "Just chatting, no changes."
    # json block without "nodes" is not a workflow document
    none_doc, _ = wfl._extract_doc('```json\n{"a": 1}\n```')
    assert none_doc is None


def test_builder_chat_applies_doc(env, monkeypatch):
    reg = orc.load_registry()
    reg["agents"]["workflow-builder"] = dict(reg["agents"]["alpha"])
    wf = wfl.create_workflow("chatty")
    new_doc = {
        "name": "chatty", "description": "",
        "nodes": [{"id": "trigger", "type": "trigger.manual", "x": 0, "y": 0, "config": {}},
                  {"id": "s1", "type": "step.agent", "x": 280, "y": 0,
                   "config": {"agent": "alpha", "instruction": "hi"}}],
        "edges": [{"from": "trigger", "to": "s1", "kind": "flow"}],
    }
    _StubHandler.reply = "Added a step.\n```json\n" + json.dumps(new_doc) + "\n```"
    text = wfl.builder_chat(wf["id"], "add a step")
    assert text == "Added a step."
    saved = wfl.load_workflow(wf["id"])
    assert len(saved["nodes"]) == 2 and saved["updated_by"] == "workflow-builder"


def test_builder_chat_bad_doc_reports(env, monkeypatch):
    reg = orc.load_registry()
    reg["agents"]["workflow-builder"] = dict(reg["agents"]["alpha"])
    wf = wfl.create_workflow("chatty2")
    bad = '{"name": "x", "nodes": [{"id": "z", "type": "step.teleport", "config": {}}], "edges": []}'
    _StubHandler.reply = "Trying.\n```json\n" + bad + "\n```"
    text = wfl.builder_chat(wf["id"], "do a thing")
    # both attempts return the same invalid doc -> user gets the rejection
    assert "could not apply" in text.lower()
    assert len(wfl.load_workflow(wf["id"])["nodes"]) == 1     # unchanged
