from pathlib import Path

from mcp_llm_wiki.config import Config, load_from_env


def test_load_from_env_defaults(monkeypatch):
    for key in (
        "MCP_LLM_WIKI_ROOT",
        "AGENT_LLM_WIKIS_RW",
        "AGENT_LLM_WIKIS_READONLY",
        "AGENT_LLM_WIKI_USER",
        "MCP_LLM_WIKI_PORT",
        "MCP_LLM_WIKI_READ_TTL",
    ):
        monkeypatch.delenv(key, raising=False)
    cfg = load_from_env()
    assert cfg.root == Path("/wikis")
    assert cfg.wikis_rw == frozenset()
    assert cfg.wikis_readonly == frozenset()
    assert cfg.port == 3100
    assert cfg.read_refresh_ttl_seconds == 30


def test_load_from_env_parses_csv(monkeypatch):
    monkeypatch.setenv("MCP_LLM_WIKI_ROOT", "/tmp/x")
    monkeypatch.setenv("AGENT_LLM_WIKIS_RW", "a, b ,c")
    monkeypatch.setenv("AGENT_LLM_WIKIS_READONLY", "d")
    monkeypatch.setenv("AGENT_LLM_WIKI_USER", "wiki-bot-vmA")
    monkeypatch.setenv("MCP_LLM_WIKI_PORT", "3300")
    monkeypatch.setenv("MCP_LLM_WIKI_READ_TTL", "5")
    cfg = load_from_env()
    assert cfg.root == Path("/tmp/x")
    assert cfg.wikis_rw == frozenset({"a", "b", "c"})
    assert cfg.wikis_readonly == frozenset({"d"})
    assert cfg.agent_identity == "wiki-bot-vmA"
    assert cfg.port == 3300
    assert cfg.read_refresh_ttl_seconds == 5


def test_capability_methods():
    cfg = Config(
        root=Path("/tmp/x"),
        wikis_rw=frozenset({"alpha"}),
        wikis_readonly=frozenset({"beta"}),
    )
    assert cfg.can_write("alpha")
    assert not cfg.can_write("beta")
    assert not cfg.can_write("unknown")
    assert cfg.is_known("alpha")
    assert cfg.is_known("beta")
    assert not cfg.is_known("unknown")
    assert cfg.known_wikis == frozenset({"alpha", "beta"})
