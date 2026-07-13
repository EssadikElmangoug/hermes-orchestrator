import orchestrator as orc
import proxy


def test_cli_manifest_parsing():
    body = "# mytool\n\nDoes a thing.\nAcross lines.\n\n## Commands\n\n```\nmytool --run\n```\n"
    parsed = orc._parse_cli_manifest(body)
    assert parsed["description"] == "Does a thing. Across lines."
    assert parsed["commands"] == "mytool --run"


def test_shareable_env_view_excludes_per_agent_keys(tmp_path):
    env = tmp_path / ".env"
    env.write_text(
        "API_SERVER_PORT=9301\n"        # per-agent — excluded
        "MY_API_KEY=abc\n"
        "# comment\n"
        "MY_API_KEY=override\n"         # last occurrence wins
    )
    view = orc._shareable_env_view(env)
    assert "MY_API_KEY" in view and view["MY_API_KEY"] == "MY_API_KEY=override"
    assert not any(k.startswith("API_SERVER") for k in view)


def test_options_cache_key_ignores_refresh_param():
    assert proxy._options_key("test", "refresh=true") == proxy._options_key("test", "")
    assert proxy._options_key("test", "profile=x") != proxy._options_key("test", "")
    assert proxy._options_key("a", "") != proxy._options_key("b", "")


def test_agent_for_host_matches_only_registered_agents(monkeypatch):
    monkeypatch.setattr(
        proxy.orc, "load_registry", lambda: {"agents": {"fixer": {}}})
    assert proxy.agent_for_host("fixer.example.com", "example.com") == "fixer"
    assert proxy.agent_for_host("ghost.example.com", "example.com") is None
    assert proxy.agent_for_host("fixer.other.com", "example.com") is None
    assert proxy.agent_for_host("fixer.example.com", "") is None
