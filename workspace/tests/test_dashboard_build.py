"""First-start dashboard web-UI build handling (fresh installs).

`hermes dashboard --skip-build` serves the prebuilt hermes_cli/web_dist —
which is git-ignored upstream, so a fresh install has none and the dashboard
exits immediately with "no web dist found". The orchestrator must drop
--skip-build in that case (hermes then builds the UI on first start) and the
proxy must report "building" instead of a dead-end timeout page.
"""

from pathlib import Path

import orchestrator as orc
import proxy


class _FakeProc:
    def __init__(self, pid=4242, alive=True):
        self.pid = pid
        self._alive = alive

    def poll(self):
        return None if self._alive else 1


def _setup_agent(monkeypatch, tmp_path, dist_ready):
    agent = {"home": str(tmp_path / "agent" / ".hermes"), "dash_port": 9999}
    (tmp_path / "agent").mkdir()
    reg = {"agents": {"general": agent}}
    monkeypatch.setattr(orc, "load_registry", lambda: reg)
    monkeypatch.setattr(orc, "save_registry", lambda r: None)
    monkeypatch.setattr(orc, "is_running", lambda n: True)
    monkeypatch.setattr(orc, "dash_is_running", lambda n: False)
    monkeypatch.setattr(orc, "web_dist_ready", lambda: dist_ready)
    monkeypatch.setattr(orc, "_agent_env", lambda a: {})
    monkeypatch.setattr(orc, "_web_build_agent", None)
    monkeypatch.setattr(orc, "_dash_procs", {})
    spawned = {}

    def fake_popen(cmd, **kw):
        spawned["cmd"] = cmd
        return _FakeProc()

    monkeypatch.setattr(orc.subprocess, "Popen", fake_popen)
    return spawned


def test_skip_build_used_when_dist_ready(monkeypatch, tmp_path):
    spawned = _setup_agent(monkeypatch, tmp_path, dist_ready=True)
    orc.start_dashboard("general")
    assert "--skip-build" in spawned["cmd"]


def test_skip_build_dropped_on_fresh_install(monkeypatch, tmp_path):
    spawned = _setup_agent(monkeypatch, tmp_path, dist_ready=False)
    orc.start_dashboard("general")
    assert "--skip-build" not in spawned["cmd"]
    assert orc._web_build_agent == "general"


def test_second_agent_waits_while_build_runs(monkeypatch, tmp_path):
    spawned = _setup_agent(monkeypatch, tmp_path, dist_ready=False)
    orc.start_dashboard("general")
    # a second agent must not start a concurrent npm build
    monkeypatch.setattr(orc, "dash_is_running", lambda n: n == "general")
    orc.load_registry()["agents"]["other"] = {
        "home": str(tmp_path / "agent" / ".hermes"), "dash_port": 9998}
    try:
        orc.start_dashboard("other")
        assert False, "expected ValueError while the UI build runs"
    except ValueError as exc:
        assert "being built" in str(exc)
    assert spawned["cmd"][0].endswith("hermes")  # only the first spawn happened


def test_web_build_active_tracks_builder(monkeypatch, tmp_path):
    _setup_agent(monkeypatch, tmp_path, dist_ready=False)
    orc.start_dashboard("general")
    monkeypatch.setattr(orc, "dash_is_running", lambda n: n == "general")
    assert orc.web_build_active()
    # dist appearing (build finished) ends the "building" state
    monkeypatch.setattr(orc, "web_dist_ready", lambda: True)
    assert not orc.web_build_active()


def test_ensure_dashboard_reports_building(monkeypatch):
    agent = {"dash_port": 9999}
    monkeypatch.setattr(proxy.orc, "load_registry",
                        lambda: {"agents": {"general": agent}})
    monkeypatch.setattr(proxy.orc, "_port_open", lambda p: False)
    monkeypatch.setattr(proxy.orc, "start_dashboard", lambda n: "url")
    monkeypatch.setattr(proxy.orc, "dash_proc_died", lambda n: False)
    monkeypatch.setattr(proxy.orc, "web_build_active", lambda: True)
    monkeypatch.setattr(proxy, "_DASH_START_TIMEOUT", 0)
    port, err = proxy._ensure_dashboard("general")
    assert port is None and err == proxy._DASH_BUILDING


def test_ensure_dashboard_reports_dead_process_with_log(monkeypatch):
    agent = {"dash_port": 9999}
    monkeypatch.setattr(proxy.orc, "load_registry",
                        lambda: {"agents": {"general": agent}})
    monkeypatch.setattr(proxy.orc, "_port_open", lambda p: False)
    monkeypatch.setattr(proxy.orc, "start_dashboard", lambda n: "url")
    monkeypatch.setattr(proxy.orc, "dash_proc_died", lambda n: True)
    monkeypatch.setattr(
        proxy.orc, "dash_log_tail",
        lambda n: "✗ --skip-build was passed but no web dist found at: <x>")
    port, err = proxy._ensure_dashboard("general")
    assert port is None
    assert "exited during startup" in err
    assert "no web dist found" in err
    assert "<x>" not in err          # log tail is HTML-escaped
