"""End-to-end integration for wiki_save + wiki_log_append against a
real git bare repo. Each test gets:

  - a bare repo at <tmp>/test.git
  - a working tree at <tmp>/test/ with wiki/ + raw/ subdirs, an initial
    log.md, and a .gitattributes that wires the custom merge-drivers
  - both standard git identity and the merge-driver config

The server build_server() is wired against that wiki_root, with 'test'
configured as a writable wiki.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from mcp_llm_wiki import server as server_mod
from mcp_llm_wiki.config import Config

MERGE_DRIVERS = Path(__file__).resolve().parent.parent / "merge_drivers"


def _run(cmd: list[str], cwd: Path, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, check=check, capture_output=True, text=True)


@pytest.fixture
def wiki_workspace(tmp_path: Path) -> tuple[Path, Path]:
    """Return (wiki_root, bare_remote). The wiki_root has a cloned
    `test` wiki working tree that pushes/pulls from the bare remote.
    """
    bare = tmp_path / "test.git"
    _run(["git", "init", "--quiet", "--bare", "--initial-branch=main", str(bare)], tmp_path)

    wiki_root = tmp_path / "wikis"
    wiki_root.mkdir()
    wiki = wiki_root / "test"
    _run(["git", "clone", "--quiet", str(bare), str(wiki)], wiki_root)
    _run(["git", "config", "user.email", "wiki-bot-test@gitea.local"], wiki)
    _run(["git", "config", "user.name", "wiki-bot-test"], wiki)
    _run(
        [
            "git", "config", "merge.llm-wiki-log.driver",
            f"{MERGE_DRIVERS}/log_md_merge.sh %A %O %B",
        ],
        wiki,
    )
    _run(
        [
            "git", "config", "merge.llm-wiki-index.driver",
            f"{MERGE_DRIVERS}/index_md_merge.sh %A %O %B",
        ],
        wiki,
    )
    (wiki / "wiki").mkdir()
    (wiki / "raw").mkdir()
    (wiki / ".gitattributes").write_text(
        "log.md merge=llm-wiki-log\nindex.md merge=llm-wiki-index\n"
    )
    (wiki / "log.md").write_text("# Log\n\n")
    (wiki / "wiki" / "index.md").write_text("# Index\n\n")
    _run(["git", "add", "."], wiki)
    _run(["git", "commit", "--quiet", "-m", "seed"], wiki)
    _run(["git", "push", "--quiet"], wiki)
    return wiki_root, bare


@pytest.fixture
def server(wiki_workspace):
    wiki_root, _ = wiki_workspace
    config = Config(
        root=wiki_root,
        wikis_rw=frozenset({"test"}),
        wikis_readonly=frozenset(),
        agent_identity="wiki-bot-test",
        port=3100,
    )
    return server_mod.build_server(config)


async def _call(server, tool: str, **kwargs):
    return await server.call_tool(tool, kwargs)


@pytest.mark.asyncio
async def test_wiki_save_creates_commits_pushes(server, wiki_workspace):
    wiki_root, bare = wiki_workspace
    _, payload = await _call(
        server,
        "wiki_save",
        wiki="test",
        page="concepts/etag.md",
        content="---\ntitle: ETag\nkind: concept\n---\nA content fingerprint.\n",
    )
    assert payload["committed"] is True
    assert payload["pushed"] is True
    assert len(payload["etag"]) == 64

    # File exists in working tree.
    assert (wiki_root / "test" / "wiki" / "concepts" / "etag.md").is_file()
    # And in the bare remote.
    log = _run(["git", "log", "--oneline", "main"], bare).stdout
    assert "wiki: update concepts/etag.md" in log


@pytest.mark.asyncio
async def test_wiki_save_commit_author_is_configured_identity(server, wiki_workspace):
    _, bare = wiki_workspace
    await _call(
        server,
        "wiki_save",
        wiki="test",
        page="entities/git.md",
        content="A version control system.\n",
    )
    author = _run(
        ["git", "log", "-1", "--pretty=%an <%ae>", "main"], bare
    ).stdout.strip()
    assert "wiki-bot-test" in author
    assert "Co-Authored-By" not in _run(["git", "log", "-1", "--pretty=%B"], bare).stdout


@pytest.mark.asyncio
async def test_wiki_save_sanitises_content(server, wiki_workspace):
    wiki_root, bare = wiki_workspace
    _, payload = await _call(
        server,
        "wiki_save",
        wiki="test",
        page="concepts/dirty.md",
        content="Visible<!-- secret instructions --> text<script>evil()</script>",
    )
    on_disk = (wiki_root / "test" / "wiki" / "concepts" / "dirty.md").read_text()
    assert "<!--" not in on_disk
    assert "<script>" not in on_disk
    assert "secret instructions" not in on_disk
    # Sanitisation report is reflected in the commit message.
    msg = _run(["git", "log", "-1", "--pretty=%B", "main"], bare).stdout
    assert "sanitize: stripped" in msg
    assert "html-comment" in msg
    assert payload["sanitize_report"].startswith("sanitize:")


@pytest.mark.asyncio
async def test_wiki_save_etag_match_overwrites(server, wiki_workspace):
    wiki_root, _ = wiki_workspace
    # First write to establish the page + capture its etag.
    _, first = await _call(
        server,
        "wiki_save",
        wiki="test",
        page="concepts/iteration.md",
        content="v1\n",
    )
    first_etag = first["etag"]

    _, second = await _call(
        server,
        "wiki_save",
        wiki="test",
        page="concepts/iteration.md",
        content="v2\n",
        etag=first_etag,
    )
    assert second["committed"] is True
    on_disk = (wiki_root / "test" / "wiki" / "concepts" / "iteration.md").read_text()
    assert on_disk == "v2\n"


@pytest.mark.asyncio
async def test_wiki_save_etag_mismatch_rejects(server, wiki_workspace):
    await _call(
        server,
        "wiki_save",
        wiki="test",
        page="concepts/x.md",
        content="original\n",
    )
    with pytest.raises(Exception, match="etag_mismatch"):
        await _call(
            server,
            "wiki_save",
            wiki="test",
            page="concepts/x.md",
            content="conflicting update\n",
            etag="0" * 64,  # deliberately wrong
        )


@pytest.mark.asyncio
async def test_wiki_save_noop_does_not_commit(server, wiki_workspace):
    _, bare = wiki_workspace
    await _call(
        server,
        "wiki_save",
        wiki="test",
        page="concepts/idempotent.md",
        content="same content\n",
    )
    sha_before = _run(["git", "rev-parse", "main"], bare).stdout.strip()
    _, payload = await _call(
        server,
        "wiki_save",
        wiki="test",
        page="concepts/idempotent.md",
        content="same content\n",
    )
    sha_after = _run(["git", "rev-parse", "main"], bare).stdout.strip()
    assert payload["committed"] is False
    assert sha_before == sha_after


@pytest.mark.asyncio
async def test_wiki_save_rejects_traversal(server):
    with pytest.raises(Exception, match="(absolute|traversal)"):
        await _call(
            server,
            "wiki_save",
            wiki="test",
            page="../../etc/passwd",
            content="x",
        )


@pytest.mark.asyncio
async def test_wiki_log_append_writes_timestamped_line(server, wiki_workspace):
    wiki_root, bare = wiki_workspace
    _, payload = await _call(
        server,
        "wiki_log_append",
        wiki="test",
        entry="ingest | source-A",
    )
    assert payload["committed"] is True
    log_text = (wiki_root / "test" / "log.md").read_text()
    # Header preserved + new entry appended.
    assert log_text.startswith("# Log\n")
    assert "ingest | source-A" in log_text
    # Each entry line is `## [ISO-timestamp] <entry text>`.
    entry_lines = [ln for ln in log_text.splitlines() if ln.startswith("## [")]
    assert len(entry_lines) >= 1
    assert entry_lines[-1].endswith("ingest | source-A")
    # Pushed.
    log_oneline = _run(["git", "log", "--oneline", "main"], bare).stdout
    assert "log: append entry" in log_oneline


@pytest.mark.asyncio
async def test_wiki_log_append_sanitises_entry(server, wiki_workspace):
    wiki_root, _ = wiki_workspace
    await _call(
        server,
        "wiki_log_append",
        wiki="test",
        entry="ingest <!-- secret --> | normal source",
    )
    log_text = (wiki_root / "test" / "log.md").read_text()
    assert "secret" not in log_text
    assert "ingest" in log_text


@pytest.mark.asyncio
async def test_wiki_log_append_rejects_empty(server):
    with pytest.raises(Exception, match="empty_entry"):
        await _call(server, "wiki_log_append", wiki="test", entry="   ")


@pytest.mark.asyncio
async def test_wiki_log_append_fixes_missing_trailing_newline(server, wiki_workspace):
    wiki_root, _ = wiki_workspace
    wiki = wiki_root / "test"
    log_path = wiki / "log.md"
    # Commit a log.md whose last line has no trailing newline.
    log_path.write_text("# Log\n\n## [2026-05-01T00:00:00Z aaa] earlier")
    _run(["git", "add", "log.md"], wiki)
    _run(["git", "commit", "--quiet", "-m", "log without trailing newline"], wiki)
    _run(["git", "push", "--quiet"], wiki)

    await _call(server, "wiki_log_append", wiki="test", entry="later entry")

    text = log_path.read_text()
    # The new entry starts its own line — not glued onto "earlier".
    assert "] earlier\n## [" in text
    assert text.rstrip().endswith("later entry")
