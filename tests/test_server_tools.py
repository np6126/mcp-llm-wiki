"""Tool-registration + dispatch tests for the FastMCP server.

We don't spin up the network transport; we exercise the registered
callables through FastMCP's tool-manager. Each registered tool gets:

  - a metadata check (annotation, description present)
  - a behaviour check via direct call
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mcp_llm_wiki import server as server_mod
from mcp_llm_wiki.config import Config


@pytest.fixture
def populated_wiki(tmp_path: Path) -> Path:
    """A wiki with three pages and one raw source."""
    root = tmp_path / "wikis"
    wiki = root / "test"
    (wiki / "wiki" / "concepts").mkdir(parents=True)
    (wiki / "wiki" / "entities").mkdir()
    (wiki / "raw").mkdir()
    (wiki / "wiki" / "concepts" / "etag.md").write_text(
        "---\ntitle: ETag\nkind: concept\nupdated: 2026-05-10\n---\nSee [[gitea]].\n"
    )
    (wiki / "wiki" / "entities" / "gitea.md").write_text(
        "---\ntitle: Gitea\nkind: entity\nupdated: 2026-05-10\n---\nA git server.\n"
    )
    (wiki / "wiki" / "index.md").write_text("# Index\n\n## Concepts\n- [[etag]]\n")
    (wiki / "raw" / "note.txt").write_text("raw text")
    return root


@pytest.fixture
def server(populated_wiki: Path):
    config = Config(
        root=populated_wiki,
        wikis_rw=frozenset({"test"}),
        wikis_readonly=frozenset({"readme"}),
        agent_identity="test-agent",
        port=3100,
    )
    return server_mod.build_server(config)


async def _call(server, tool_name: str, **kwargs):
    """Invoke a tool via FastMCP's internal call_tool. Returns the
    structured result (post-JSON-serialisation)."""
    return await server.call_tool(tool_name, kwargs)


@pytest.mark.asyncio
async def test_all_eight_tools_registered(server):
    tools = await server.list_tools()
    names = {t.name for t in tools}
    assert names == {
        "wiki_list",
        "wiki_read",
        "wiki_read_raw",
        "wiki_search",
        "wiki_save",
        "wiki_log_append",
        "wiki_lint",
        "wiki_graph",
    }


@pytest.mark.asyncio
async def test_annotations_present(server):
    tools = await server.list_tools()
    by_name = {t.name: t for t in tools}
    # Read-side tools claim readOnlyHint=True
    for name in (
        "wiki_list",
        "wiki_read",
        "wiki_read_raw",
        "wiki_search",
        "wiki_lint",
        "wiki_graph",
    ):
        assert by_name[name].annotations is not None
        assert by_name[name].annotations.readOnlyHint is True

    # wiki_save is idempotent + non-destructive
    save = by_name["wiki_save"]
    assert save.annotations.readOnlyHint is False
    assert save.annotations.idempotentHint is True
    assert save.annotations.destructiveHint is False

    # wiki_log_append is non-idempotent
    log_t = by_name["wiki_log_append"]
    assert log_t.annotations.idempotentHint is False


@pytest.mark.asyncio
async def test_wiki_list_returns_pages(server):
    result = await _call(server, "wiki_list", wiki="test")
    # FastMCP returns (content_blocks, structured_payload)
    _, payload = result
    items = payload["result"]
    paths = {item["path"] for item in items}
    assert "wiki/concepts/etag.md" in paths
    assert "wiki/entities/gitea.md" in paths


@pytest.mark.asyncio
async def test_wiki_read_returns_etag_and_links(server):
    _, payload = await _call(server, "wiki_read", wiki="test", page="concepts/etag.md")
    page = payload
    assert page["frontmatter"]["title"] == "ETag"
    assert "gitea" in page["outgoing_links"]
    assert len(page["etag"]) == 64


@pytest.mark.asyncio
async def test_wiki_read_raw(server):
    _, payload = await _call(server, "wiki_read_raw", wiki="test", path="note.txt")
    assert payload["size"] == len(b"raw text")
    import base64

    assert base64.b64decode(payload["content_base64"]) == b"raw text"


@pytest.mark.asyncio
async def test_wiki_lint(server):
    _, payload = await _call(server, "wiki_lint", wiki="test")
    assert "issues" in payload
    assert "clean" in payload


@pytest.mark.asyncio
async def test_wiki_graph(server):
    _, payload = await _call(server, "wiki_graph", wiki="test")
    # etag.md has at least one inbound link from index.md
    assert "wiki/index.md" in payload["wiki/concepts/etag.md"]


@pytest.mark.asyncio
async def test_unknown_wiki_returns_error(server):
    with pytest.raises(Exception, match="unknown_wiki"):
        await _call(server, "wiki_list", wiki="not-configured")


@pytest.mark.asyncio
async def test_readonly_wiki_blocks_write(server, tmp_path):
    # 'readme' is in wikis_readonly per the fixture.
    with pytest.raises(Exception, match="read_only"):
        await _call(server, "wiki_save", wiki="readme", page="x.md", content="hi")


def _ttl_server(populated_wiki: Path, ttl: int):
    """Build a server whose 'test' wiki looks like a git working tree,
    so _refresh takes the TTL/pull path rather than the no-.git skip."""
    (populated_wiki / "test" / ".git").mkdir(exist_ok=True)
    config = Config(
        root=populated_wiki,
        wikis_rw=frozenset({"test"}),
        agent_identity="test-agent",
        port=3100,
        read_refresh_ttl_seconds=ttl,
    )
    return server_mod.build_server(config)


@pytest.mark.asyncio
async def test_read_refresh_coalesces_within_ttl(populated_wiki, monkeypatch):
    """A burst of reads inside the TTL window triggers a single pull."""
    pulls: list = []
    monkeypatch.setattr(server_mod.git_ops, "pull_rebase", lambda d: pulls.append(d))
    server = _ttl_server(populated_wiki, ttl=3600)
    for _ in range(3):
        await _call(server, "wiki_list", wiki="test")
    assert len(pulls) == 1


@pytest.mark.asyncio
async def test_read_refresh_ttl_zero_pulls_every_read(populated_wiki, monkeypatch):
    """TTL 0 reproduces the original pull-on-every-read behaviour."""
    pulls: list = []
    monkeypatch.setattr(server_mod.git_ops, "pull_rebase", lambda d: pulls.append(d))
    server = _ttl_server(populated_wiki, ttl=0)
    for _ in range(3):
        await _call(server, "wiki_list", wiki="test")
    assert len(pulls) == 3


@pytest.mark.asyncio
async def test_read_survives_pull_failure(populated_wiki, monkeypatch):
    """A failing pull must not fail the read — serve the local tree."""
    def boom(_dir):
        raise server_mod.git_ops.GitOpsError("git host unreachable")

    monkeypatch.setattr(server_mod.git_ops, "pull_rebase", boom)
    server = _ttl_server(populated_wiki, ttl=0)
    result = await _call(server, "wiki_list", wiki="test")
    assert result is not None


# Write-side integration (against a real git bare repo + clone) lives
# in test_server_writes.py — those tests exercise wiki_save and
# wiki_log_append end-to-end with the git_ops layer.
