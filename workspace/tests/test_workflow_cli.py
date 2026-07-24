"""The agent-facing workflow surface: run summaries, workflow detail, the
seeded CLI's name resolution, and the shared-resource seeding that puts the
CLI + skill on every agent without manual setup."""

import importlib.util
from pathlib import Path

import pytest

import orchestrator as orc
import workflows as wfl
# the stub-agent harness and its fixtures live with the engine tests
from test_workflows import _StubHandler, _wait, env, stub_agent  # noqa: F401

CLI_PATH = Path(__file__).resolve().parents[1] / "seed_bin" / "hermes-workflow"


@pytest.fixture(scope="module")
def cli():
    """Import the extension-less CLI script as a module."""
    spec = importlib.util.spec_from_loader(
        "hermes_workflow_cli",
        importlib.machinery.SourceFileLoader("hermes_workflow_cli", str(CLI_PATH)))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ─── name resolution (the "run my facebook workflow" path) ───────────────────

WFS = [
    {"id": "wf-1", "name": "Facebook Post"},
    {"id": "wf-2", "name": "Newsletter"},
    {"id": "wf-3", "name": "Daily Report"},
]


@pytest.mark.parametrize("query,expect", [
    ("wf-1", "wf-1"),                 # exact id
    ("Facebook Post", "wf-1"),        # exact name
    ("facebook post", "wf-1"),        # case-insensitive
    ("facebook", "wf-1"),             # substring — the user's phrasing
    ("newsletter", "wf-2"),
    ("Daily", "wf-3"),
    ("facebok", "wf-1"),              # fuzzy typo
])
def test_resolve_matches(cli, query, expect):
    assert cli._resolve(query, WFS)["id"] == expect


def test_resolve_ambiguous_lists_candidates(cli, capsys):
    two = [{"id": "a", "name": "Facebook Post"}, {"id": "b", "name": "Facebook Ad"}]
    with pytest.raises(SystemExit):
        cli._resolve("facebook", two)
    err = capsys.readouterr().err
    assert "Facebook Post" in err and "Facebook Ad" in err


def test_resolve_no_match_lists_available(cli, capsys):
    with pytest.raises(SystemExit):
        cli._resolve("zzzz-nothing", WFS)
    assert "Newsletter" in capsys.readouterr().err


def test_resolve_empty_workspace(cli, capsys):
    with pytest.raises(SystemExit):
        cli._resolve("facebook", [])
    assert "no workflows exist" in capsys.readouterr().err


# ─── run summary / detail ────────────────────────────────────────────────────

def _flow(env):
    wf = wfl.create_workflow("Facebook Post", "posts to facebook")
    doc = wfl.load_workflow(wf["id"])
    doc["nodes"] += [
        {"id": "s1", "type": "step.agent",
         "config": {"agent": "alpha", "title": "Draft"}},
        {"id": "s2", "type": "step.agent",
         "config": {"agent": "beta", "title": "Publish"}},
    ]
    doc["edges"] = [{"from": "trigger", "to": "s1", "kind": "flow"},
                    {"from": "s1", "to": "s2", "kind": "flow"}]
    return wfl.save_workflow(wf["id"], doc)


def test_workflow_detail(env):
    wf = _flow(env)
    d = wfl.workflow_detail(wf["id"])
    assert d["name"] == "Facebook Post"
    assert d["description"] == "posts to facebook"
    assert len(d["steps"]) == 2
    assert "manual" in d["triggers"]


def test_run_summary_final_output_is_last_step(env):
    wf = _flow(env)
    _StubHandler.reply = "PUBLISHED"
    run = wfl.start_run(wf["id"], trigger="agent", payload="go")
    run = _wait(run["id"], {"success"})
    s = wfl.run_summary(run)
    assert s["status"] == "success"
    assert s["trigger"] == "agent"
    assert s["final_output"] == "PUBLISHED"     # last node's output, not the trigger's
    assert s["workflow_name"] == "Facebook Post"


def test_run_summary_reports_failed_step(env):
    wf = wfl.create_workflow("broken")
    doc = wfl.load_workflow(wf["id"])
    doc["nodes"].append({"id": "s1", "type": "step.agent",
                         "config": {"agent": "ghost", "title": "Nope"}})
    doc["edges"] = [{"from": "trigger", "to": "s1", "kind": "flow"}]
    wfl.save_workflow(wf["id"], doc)
    run = _wait(wfl.start_run(wf["id"])["id"], {"failed"})
    s = wfl.run_summary(run)
    assert s["status"] == "failed"
    assert s["failed_step"] == "Nope"
    assert "ghost" in s["error"]


# ─── seeding: no manual setup for a fresh install ────────────────────────────

def test_seed_installs_cli_skill_and_manifest(tmp_path, monkeypatch):
    shared = tmp_path / "shared"
    monkeypatch.setattr(orc, "SHARED_DIR", shared)
    monkeypatch.setattr(orc, "AGENT_TOKEN_PATH", tmp_path / "agent_token")
    # don't touch the real /usr/local/bin from a test
    monkeypatch.setattr(orc, "_link_system_bin", lambda: None)
    orc.seed_workspace_docs()

    cli = shared / "bin" / "hermes-workflow"
    assert cli.is_file(), "workflow CLI must be seeded into shared/bin"
    assert cli.stat().st_mode & 0o111, "seeded CLI must be executable"
    assert (shared / "clis" / "hermes-workflow.md").is_file()
    skill = shared / "skills" / "workspace" / "workflows" / "SKILL.md"
    assert skill.is_file(), "workflows skill must be seeded"
    assert "hermes-workflow run" in skill.read_text()
    # the shared-resources skill still seeds too (no regression)
    assert (shared / "skills" / "workspace" / "shared-resources" / "SKILL.md").is_file()


def test_hermes_agent_skill_gets_workflow_section(tmp_path, monkeypatch):
    shared = tmp_path / "shared"
    monkeypatch.setattr(orc, "SHARED_DIR", shared)
    monkeypatch.setattr(orc, "_link_system_bin", lambda: None)
    hermes = shared / "skills" / "autonomous-ai-agents" / "hermes-agent"
    hermes.mkdir(parents=True)
    (hermes / "SKILL.md").write_text("# hermes-agent\n\nOriginal content.\n")
    orc.seed_workspace_docs()
    text = (hermes / "SKILL.md").read_text()
    assert "Original content." in text          # never clobbers the upstream skill
    assert "hermes-workflow list" in text
    assert "Running workspace workflows" in text


# ─── adopted (read-only) agents still learn about workflows ──────────────────

def _adopted_home(tmp_path) -> Path:
    """A pre-existing Hermes install: its own skills, config, env, memories."""
    home = tmp_path / "adopted" / ".hermes"
    (home / "skills" / "autonomous-ai-agents" / "hermes-agent").mkdir(parents=True)
    (home / "skills" / "autonomous-ai-agents" / "hermes-agent" / "SKILL.md"
     ).write_text("# hermes-agent\n\nUpstream guidance.\n")
    (home / "skills" / "mine").mkdir()
    (home / "skills" / "mine" / "SKILL.md").write_text("my own skill")
    (home / "memories").mkdir()
    (home / "memories" / "notes.md").write_text("private notes")
    (home / "config.yaml").write_text("model: sonnet\nplatforms:\n  telegram: {}\n")
    (home / ".env").write_text("TELEGRAM_BOT_TOKEN=secret\n")
    (home / "SOUL.md").write_text("original soul")
    return home


def test_adopted_home_gets_workflow_skill_and_manifest(tmp_path):
    home = _adopted_home(tmp_path)
    changed = orc.seed_docs_into_home(home)
    skill = home / "skills" / "workspace" / "workflows" / "SKILL.md"
    assert skill.is_file(), "adopted agents must receive the workflows skill"
    assert "hermes-workflow run" in skill.read_text()
    assert (home / "skills" / "workspace" / "shared-resources" / "SKILL.md").is_file()
    assert (home / "clis" / "hermes-workflow.md").is_file()
    assert changed
    # and the hermes-agent skill gains the block without losing upstream text
    text = (home / "skills" / "autonomous-ai-agents" / "hermes-agent"
            / "SKILL.md").read_text()
    assert "Upstream guidance." in text
    assert "hermes-workflow list" in text


def test_adopted_home_functional_files_never_touched(tmp_path):
    home = _adopted_home(tmp_path)
    before = {p: p.read_bytes() for p in (
        home / "config.yaml", home / ".env", home / "SOUL.md",
        home / "memories" / "notes.md", home / "skills" / "mine" / "SKILL.md")}
    orc.seed_docs_into_home(home)
    for path, content in before.items():
        assert path.read_bytes() == content, f"{path.name} must not be modified"


def test_adopted_seeding_is_idempotent(tmp_path):
    home = _adopted_home(tmp_path)
    orc.seed_docs_into_home(home)
    assert orc.seed_docs_into_home(home) == [], "second pass must be a no-op"


def test_adopted_seeding_can_be_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_NO_ADOPTED_DOCS", "1")
    assert not orc._adopted_docs_enabled()
    monkeypatch.delenv("HERMES_NO_ADOPTED_DOCS")
    assert orc._adopted_docs_enabled()


def test_adopted_without_hermes_agent_skill_gets_no_stub(tmp_path):
    home = tmp_path / "bare" / ".hermes"
    home.mkdir(parents=True)
    orc.seed_docs_into_home(home)
    assert (home / "skills" / "workspace" / "workflows" / "SKILL.md").is_file()
    assert not (home / "skills" / "autonomous-ai-agents").exists(), \
        "must not invent a hermes-agent skill the install never had"


def test_workspace_token_is_stable_and_private(tmp_path, monkeypatch):
    monkeypatch.setattr(orc, "AGENT_TOKEN_PATH", tmp_path / "agent_token")
    first = orc.workspace_token()
    assert len(first) > 20
    assert orc.workspace_token() == first       # stable across calls
    assert (tmp_path / "agent_token").stat().st_mode & 0o077 == 0
