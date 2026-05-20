"""wiki_io filesystem-operation tests.

We exercise list / read / search / graph / lint against a tiny
hand-built wiki tree. The git side is covered separately in
test_merge_drivers and (later) test_git_ops.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from mcp_llm_wiki import wiki_io
from mcp_llm_wiki.path_safety import PathSafetyError


@pytest.fixture
def wiki(tmp_path: Path) -> Path:
    """Build a small wiki tree with a couple of pages, a raw source,
    log.md, and index.md.
    """
    wiki_dir = tmp_path / "demo"
    (wiki_dir / "wiki" / "concepts").mkdir(parents=True)
    (wiki_dir / "wiki" / "entities").mkdir()
    (wiki_dir / "raw").mkdir()

    (wiki_dir / "wiki" / "concepts" / "optimistic_concurrency.md").write_text(
        "---\n"
        "title: Optimistic Concurrency\n"
        "kind: concept\n"
        "tags: [concurrency, etag]\n"
        "updated: 2026-05-01\n"
        "---\n"
        "Optimistic concurrency relies on ETags. See [[etag]] and [[gitea]].\n"
    )
    (wiki_dir / "wiki" / "entities" / "etag.md").write_text(
        "---\n"
        "title: ETag\n"
        "kind: entity\n"
        "updated: 2026-05-10\n"
        "---\n"
        "An entity tag is a content fingerprint. Refers to [[optimistic_concurrency]].\n"
    )
    (wiki_dir / "wiki" / "entities" / "gitea.md").write_text(
        "---\n"
        "title: Gitea\n"
        "kind: entity\n"
        "updated: 2020-01-01\n"  # deliberately stale
        "---\n"
        "Gitea is a git server. Standalone — no inbound link.\n"
    )
    (wiki_dir / "wiki" / "index.md").write_text("# Index\n\n## Concepts\n- [[optimistic_concurrency]]\n")
    (wiki_dir / "wiki" / "log.md").write_text("# Log\n\n")
    (wiki_dir / "raw" / "etag_paper.txt").write_text("RFC 7232 ETag definitions...")
    return wiki_dir


def test_list_pages_returns_summaries(wiki):
    summaries = wiki_io.list_pages(wiki)
    paths = {s.path for s in summaries}
    # Includes index.md and log.md (we don't filter those out at list level).
    assert "wiki/concepts/optimistic_concurrency.md" in paths
    assert "wiki/entities/etag.md" in paths
    assert "wiki/entities/gitea.md" in paths


def test_list_pages_parses_frontmatter(wiki):
    summaries = {s.path: s for s in wiki_io.list_pages(wiki)}
    s = summaries["wiki/concepts/optimistic_concurrency.md"]
    assert s.title == "Optimistic Concurrency"
    assert s.kind == "concept"
    assert "concurrency" in s.tags
    assert s.updated == "2026-05-01"


def test_read_page_full_shape(wiki):
    page = wiki_io.read_page(wiki, "concepts/optimistic_concurrency.md")
    assert page.path == "concepts/optimistic_concurrency.md"
    assert "Optimistic concurrency relies on ETags" in page.content
    assert page.frontmatter["title"] == "Optimistic Concurrency"
    assert set(page.outgoing_links) == {"etag", "gitea"}
    assert len(page.etag) == 64  # sha256 hex


def test_read_page_rejects_traversal(wiki):
    with pytest.raises(PathSafetyError):
        wiki_io.read_page(wiki, "../../etc/passwd")


def test_read_raw(wiki):
    data = wiki_io.read_raw(wiki, "etag_paper.txt")
    assert b"RFC 7232" in data


def test_read_raw_rejects_traversal(wiki):
    with pytest.raises(PathSafetyError):
        wiki_io.read_raw(wiki, "../wiki/concepts/optimistic_concurrency.md")


def test_search_finds_token(wiki):
    if shutil.which("rg") is None:
        pytest.skip("ripgrep not available")
    hits = wiki_io.search(wiki, "ETag")
    paths = {h.path for h in hits}
    assert "wiki/concepts/optimistic_concurrency.md" in paths
    assert "wiki/entities/etag.md" in paths


def test_search_empty_query_returns_empty(wiki):
    assert wiki_io.search(wiki, "") == []


def test_build_graph(wiki):
    graph = wiki_io.build_graph(wiki)
    # gitea has an incoming link from optimistic_concurrency
    assert "wiki/concepts/optimistic_concurrency.md" in graph["wiki/entities/gitea.md"]
    # etag has incoming from optimistic_concurrency (and not from itself)
    assert "wiki/concepts/optimistic_concurrency.md" in graph["wiki/entities/etag.md"]
    # optimistic_concurrency has incoming from etag.md and index.md
    inbound = set(graph["wiki/concepts/optimistic_concurrency.md"])
    assert "wiki/entities/etag.md" in inbound
    assert "wiki/index.md" in inbound


def test_lint_reports_stale_page(wiki):
    report = wiki_io.lint(wiki, stale_days=365)
    stales = [i for i in report.issues if i.kind == "stale"]
    paths = {i.path for i in stales}
    assert "wiki/entities/gitea.md" in paths


def test_lint_reports_broken_link(wiki):
    # Add a page that references a non-existent target.
    (wiki / "wiki" / "concepts" / "dangling.md").write_text(
        "---\ntitle: Dangling\nkind: concept\n---\nSee [[nonexistent_thing]].\n"
    )
    report = wiki_io.lint(wiki)
    brokens = [i for i in report.issues if i.kind == "broken_link"]
    assert any("nonexistent_thing" in i.message for i in brokens)


def test_lint_reports_orphan_for_pages_without_inbound(wiki):
    # Add a new page nobody links to.
    (wiki / "wiki" / "entities" / "lonely.md").write_text(
        "---\ntitle: Lonely\nkind: entity\nupdated: 2026-05-10\n---\nNobody refers to me.\n"
    )
    report = wiki_io.lint(wiki)
    orphans = {i.path for i in report.issues if i.kind == "orphan"}
    assert "wiki/entities/lonely.md" in orphans
    # index.md is excluded from the orphan check.
    assert "wiki/index.md" not in orphans
