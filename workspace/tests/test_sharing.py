"""The sharing invariants: merges must match resource granularity, never
coarser. Each test encodes a real failure mode that once lost user data."""
import os

import orchestrator as orc


# ─── _merge_missing: recursive copy-if-missing ───────────────────────────────

def _mk(path, text="x"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def test_merge_missing_propagates_nested_new_skill(tmp_path):
    """A new skill inside an already-shared category must still copy —
    a one-level copy-if-missing silently strands it (the sedx3d bug)."""
    src, dst = tmp_path / "src", tmp_path / "dst"
    _mk(src / "software-development" / "new-skill" / "SKILL.md", "new")
    _mk(dst / "software-development" / "old-skill" / "SKILL.md", "old")

    copied = orc._merge_missing(src, dst)

    assert (dst / "software-development" / "new-skill" / "SKILL.md").read_text() == "new"
    assert "software-development/new-skill" in copied


def test_merge_missing_never_overwrites_destination(tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    _mk(src / "cat" / "skill" / "SKILL.md", "SOURCE")
    _mk(dst / "cat" / "skill" / "SKILL.md", "SHARED WINS")

    orc._merge_missing(src, dst)

    assert (dst / "cat" / "skill" / "SKILL.md").read_text() == "SHARED WINS"


def test_merge_missing_skips_broken_and_preserves_good_symlinks(tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    _mk(src / "real.txt")
    os.symlink("/nonexistent-target", src / "broken")
    os.symlink("real.txt", src / "alias")
    dst.mkdir()

    orc._merge_missing(src, dst)

    assert (dst / "real.txt").exists()
    assert not (dst / "broken").is_symlink()
    assert (dst / "alias").is_symlink()


def test_merge_missing_is_idempotent(tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    _mk(src / "a" / "b" / "SKILL.md")
    dst.mkdir()
    assert orc._merge_missing(src, dst) != []
    assert orc._merge_missing(src, dst) == []


# ─── _merge_cfg_edits: per-entry config merge ───────────────────────────────

def test_concurrent_mcp_additions_both_survive():
    """Two agents adding different MCP servers in the same sync window must
    both land — a whole-section diff lets the newer editor clobber the other."""
    prev = {"mcp_servers": {"github": {"cmd": "gh"}}}
    view_a = {"mcp_servers": {"github": {"cmd": "gh"}, "notion": {"cmd": "n"}}}
    view_b = {"mcp_servers": {"github": {"cmd": "gh2"}, "slack": {"cmd": "s"}}}

    out = dict(prev)
    orc._merge_cfg_edits(prev, view_a, out)   # oldest editor first
    orc._merge_cfg_edits(prev, view_b, out)   # newest wins conflicts

    assert out["mcp_servers"] == {
        "github": {"cmd": "gh2"},   # newest in-place edit wins
        "notion": {"cmd": "n"},     # A's addition survives
        "slack": {"cmd": "s"},      # B's addition survives
    }


def test_cfg_subkey_and_toplevel_deletions_propagate():
    prev = {"mcp_servers": {"a": 1, "b": 2}, "obsolete": {"x": 1}}
    out = dict(prev)
    orc._merge_cfg_edits(prev, {"mcp_servers": {"a": 1}}, out)
    assert out == {"mcp_servers": {"a": 1}}


def test_cfg_type_change_and_new_key():
    out = {}
    orc._merge_cfg_edits({"k": {"a": 1}}, {"k": "scalar", "new": {"x": 1}}, out)
    assert out == {"k": "scalar", "new": {"x": 1}}


def test_cfg_noop_when_nothing_changed():
    prev = {"model": {"default": "m"}}
    out = dict(prev)
    orc._merge_cfg_edits(prev, dict(prev), out)
    assert out == prev


# ─── _merge_webhooks_from: per-route merge ──────────────────────────────────

def test_webhook_routes_merge_per_route(tmp_path, monkeypatch):
    monkeypatch.setattr(orc, "SHARED_DIR", tmp_path / "shared")
    (tmp_path / "shared").mkdir()
    shared = tmp_path / "shared" / "webhook_subscriptions.json"
    shared.write_text('{"existing": {"secret": "keep-me"}}')

    home = tmp_path / "home"
    home.mkdir()
    (home / "webhook_subscriptions.json").write_text(
        '{"existing": {"secret": "mine"}, "added": {"secret": "new"}}')

    assert orc._merge_webhooks_from(home) is True
    import json
    merged = json.loads(shared.read_text())
    assert merged["existing"]["secret"] == "keep-me"   # shared wins collisions
    assert merged["added"]["secret"] == "new"          # new route propagates


# ─── _upsert_marked_block: idempotent doc injection ─────────────────────────

def test_marked_block_inserts_updates_and_never_duplicates(tmp_path):
    f = tmp_path / "SOUL.md"
    f.write_text("# My agent\n\noriginal soul\n")

    assert orc._upsert_marked_block(f, "v1") is True
    assert orc._upsert_marked_block(f, "v1") is False          # idempotent
    assert orc._upsert_marked_block(f, "v2") is True           # updates in place

    text = f.read_text()
    assert text.count(orc._DOC_BLOCK_BEGIN) == 1
    assert "v2" in text and "v1" not in text
    assert "original soul" in text                             # user content kept


def test_marked_block_creates_missing_file(tmp_path):
    f = tmp_path / "SOUL.md"
    assert orc._upsert_marked_block(f, "hello") is True
    assert "hello" in f.read_text()
