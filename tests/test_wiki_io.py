"""wiki_io filesystem-operation tests.

We exercise list / read / search / graph / lint against a tiny
hand-built wiki tree. The git side is covered separately in
test_merge_drivers and (later) test_git_ops.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from mcp_llm_wiki import wiki_io
from mcp_llm_wiki.path_safety import PathSafetyError, etag


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
        "updated: 2020-01-01\n"
        "---\n"
        "Gitea is a git server. Standalone — no inbound link.\n"
    )
    (wiki_dir / "wiki" / "index.md").write_text(
        "# Index\n\n## Concepts\n- [[optimistic_concurrency]]\n"
    )
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
    raw = wiki_io.read_raw(wiki, "etag_paper.txt")
    assert b"RFC 7232" in raw.content
    assert raw.size == len(raw.content)
    assert len(raw.etag) == 64  # sha256 hex
    assert raw.etag == etag((wiki / "raw" / "etag_paper.txt").read_bytes())


def test_read_raw_rejects_traversal(wiki):
    with pytest.raises(PathSafetyError):
        wiki_io.read_raw(wiki, "../wiki/concepts/optimistic_concurrency.md")


def test_read_page_missing_raises_file_not_found(wiki):
    with pytest.raises(FileNotFoundError):
        wiki_io.read_page(wiki, "concepts/ghost.md")


def test_read_raw_missing_raises_file_not_found(wiki):
    with pytest.raises(FileNotFoundError):
        wiki_io.read_raw(wiki, "ghost.txt")


def test_read_page_tolerates_malformed_frontmatter(wiki):
    # Broken YAML frontmatter must not raise — _parse_page falls back to
    # empty metadata and keeps the raw text as the body.
    (wiki / "wiki" / "concepts" / "broken.md").write_text(
        "---\ntitle: [unterminated\n---\nBody text.\n"
    )
    page = wiki_io.read_page(wiki, "concepts/broken.md")
    assert page.frontmatter == {}
    assert "Body text." in page.content


def test_list_pages_tolerates_malformed_frontmatter(wiki):
    (wiki / "wiki" / "concepts" / "broken.md").write_text(
        "---\ntitle: [unterminated\n---\nBody text.\n"
    )
    summaries = {s.path: s for s in wiki_io.list_pages(wiki)}
    assert summaries["wiki/concepts/broken.md"].title is None


def test_search_finds_token(wiki):
    if shutil.which("rg") is None:
        pytest.skip("ripgrep not available")
    hits = wiki_io.search(wiki, "ETag")
    paths = {h.path for h in hits}
    assert "wiki/concepts/optimistic_concurrency.md" in paths
    assert "wiki/entities/etag.md" in paths


def test_search_empty_query_returns_empty(wiki):
    assert wiki_io.search(wiki, "") == []


def test_search_raises_when_ripgrep_missing(wiki, monkeypatch):
    def _no_rg(*args, **kwargs):
        raise FileNotFoundError(2, "No such file or directory", "rg")

    monkeypatch.setattr(wiki_io.subprocess, "run", _no_rg)
    with pytest.raises(RuntimeError, match="ripgrep"):
        wiki_io.search(wiki, "ETag")


def test_search_raises_on_ripgrep_error(wiki, monkeypatch):
    def _rg_error(*args, **kwargs):
        return subprocess.CompletedProcess(args, returncode=2, stdout="", stderr="rg: boom")

    monkeypatch.setattr(wiki_io.subprocess, "run", _rg_error)
    with pytest.raises(RuntimeError, match="ripgrep failed"):
        wiki_io.search(wiki, "ETag")


def test_search_skips_malformed_ripgrep_lines(wiki, monkeypatch):
    # Defensive parsing: garbage lines in rg output are skipped, not fatal.
    good = wiki / "wiki" / "concepts" / "optimistic_concurrency.md"
    stdout = "\n".join(
        [
            "no_colon_here",                       # no ':' separator
            "one:colon",                           # too few fields to unpack
            "/outside/page.md:3:hit outside wiki",  # path not under wiki_dir
            f"{good}:notanumber:bad line number",   # non-integer line number
            f"{good}:7:a real hit",                 # the one well-formed line
        ]
    ) + "\n"

    def _rg(*args, **kwargs):
        return subprocess.CompletedProcess(args, returncode=0, stdout=stdout, stderr="")

    monkeypatch.setattr(wiki_io.subprocess, "run", _rg)
    hits = wiki_io.search(wiki, "anything")
    assert len(hits) == 1
    assert hits[0].path == "wiki/concepts/optimistic_concurrency.md"
    assert hits[0].line == 7


def test_search_respects_limit(wiki, monkeypatch):
    good = wiki / "wiki" / "concepts" / "optimistic_concurrency.md"
    stdout = "".join(f"{good}:{i}:hit {i}\n" for i in range(1, 6))
    captured: dict[str, list[str]] = {}

    def _rg(cmd, *args, **kwargs):
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, returncode=0, stdout=stdout, stderr="")

    monkeypatch.setattr(wiki_io.subprocess, "run", _rg)
    hits = wiki_io.search(wiki, "anything", limit=2)
    # `limit` reaches ripgrep as --max-count ...
    assert captured["cmd"][captured["cmd"].index("--max-count") + 1] == "2"
    # ... and the parse loop independently stops at `limit`.
    assert len(hits) == 2


def test_walk_pages_skips_non_utf8_files(wiki):
    # A page that is not valid UTF-8 is skipped by graph/lint, not fatal.
    (wiki / "wiki" / "concepts" / "binary.md").write_bytes(b"\xff\xfe not utf-8")
    wiki_io.lint(wiki)  # must not raise
    assert "wiki/concepts/binary.md" not in wiki_io.build_graph(wiki)


def test_list_pages_empty_when_no_wiki_dir(tmp_path):
    # A wiki_dir without a wiki/ subdirectory yields no pages, not a crash.
    assert wiki_io.list_pages(tmp_path) == []
    assert wiki_io.lint(tmp_path).is_clean


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


def test_build_graph_resolves_markdown_links(wiki):
    # wiki_graph / wiki_lint must parse [text](page.md) Markdown links,
    # not only [[wikilinks]].
    (wiki / "wiki" / "concepts" / "mdlink.md").write_text(
        "---\ntitle: MdLink\nkind: concept\n---\nSee [the ETag page](etag.md).\n"
    )
    graph = wiki_io.build_graph(wiki)
    assert "wiki/concepts/mdlink.md" in graph["wiki/entities/etag.md"]


def test_lint_reports_unindexed_page(wiki):
    # index.md lists only optimistic_concurrency; etag + gitea are not.
    report = wiki_io.lint(wiki)
    unindexed = {i.path for i in report.issues if i.kind == "unindexed"}
    assert "wiki/entities/etag.md" in unindexed
    assert "wiki/entities/gitea.md" in unindexed
    assert "wiki/concepts/optimistic_concurrency.md" not in unindexed
    # index.md and log.md are structural files — never flagged.
    assert "wiki/index.md" not in unindexed
    assert "wiki/log.md" not in unindexed


def test_lint_skips_unindexed_when_index_absent(wiki):
    # With no catalog there is nothing to check against — the absent
    # index.md must not make every page report as unindexed.
    (wiki / "wiki" / "index.md").unlink()
    report = wiki_io.lint(wiki)
    assert not [i for i in report.issues if i.kind == "unindexed"]


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


def _write_sourced_page(wiki: Path, name: str, sources_yaml: str) -> None:
    """Write a content page under wiki/concepts/ carrying a `sources:` list."""
    (wiki / "wiki" / "concepts" / name).write_text(
        f"---\ntitle: Sourced\nkind: concept\nsources:\n{sources_yaml}---\nBody.\n"
    )


def test_lint_ignores_pages_without_sources(wiki):
    # None of the fixture pages declare `sources:` — no provenance issues.
    report = wiki_io.lint(wiki)
    assert not [
        i for i in report.issues if i.kind in ("source_missing", "source_drift")
    ]


def test_lint_reports_source_missing(wiki):
    _write_sourced_page(
        wiki,
        "sourced.md",
        f"  - path: nonexistent_source.txt\n    etag: {'f' * 64}\n",
    )
    report = wiki_io.lint(wiki)
    missing = [i for i in report.issues if i.kind == "source_missing"]
    assert any(i.path == "wiki/concepts/sourced.md" for i in missing)
    assert any("raw/nonexistent_source.txt" in i.message for i in missing)


def test_lint_reports_source_drift(wiki):
    # Point at a real raw source but record a stale (wrong) etag.
    _write_sourced_page(
        wiki, "sourced.md", f"  - path: etag_paper.txt\n    etag: {'f' * 64}\n"
    )
    report = wiki_io.lint(wiki)
    drift = [i for i in report.issues if i.kind == "source_drift"]
    assert any("raw/etag_paper.txt" in i.message for i in drift)


def test_lint_no_source_issue_when_etag_matches(wiki):
    good = etag((wiki / "raw" / "etag_paper.txt").read_bytes())
    _write_sourced_page(
        wiki, "sourced.md", f"  - path: etag_paper.txt\n    etag: {good}\n"
    )
    report = wiki_io.lint(wiki)
    assert not [
        i for i in report.issues if i.kind in ("source_missing", "source_drift")
    ]


def test_lint_source_missing_is_graded(wiki):
    # Two sources, one live and one removed — the message states survivors.
    good = etag((wiki / "raw" / "etag_paper.txt").read_bytes())
    _write_sourced_page(
        wiki,
        "sourced.md",
        f"  - path: etag_paper.txt\n    etag: {good}\n"
        f"  - path: gone.txt\n    etag: {'f' * 64}\n",
    )
    report = wiki_io.lint(wiki)
    missing = [i for i in report.issues if i.kind == "source_missing"]
    assert len(missing) == 1
    assert "1 of 2 sources remain" in missing[0].message


def test_lint_tolerates_malformed_sources_frontmatter(wiki):
    # A `sources:` block that is not a clean list of {path, etag} dicts
    # must not crash the linter — malformed entries are skipped.
    _write_sourced_page(
        wiki,
        "messy.md",
        "  - not-a-mapping\n"
        "  - etag: deadbeef\n"
        f"  - path: etag_paper.txt\n    etag: {'f' * 64}\n",
    )
    report = wiki_io.lint(wiki)
    # The one well-formed entry (real source, stale etag) is still checked.
    assert any(i.kind == "source_drift" for i in report.issues)


def test_lint_treats_unsafe_source_path_as_missing(wiki):
    # A `sources:` path that escapes raw/ resolves to "missing" rather
    # than raising out of the linter.
    _write_sourced_page(
        wiki, "evil.md", f"  - path: ../../etc/passwd\n    etag: {'f' * 64}\n"
    )
    report = wiki_io.lint(wiki)
    assert any(
        i.kind == "source_missing" and i.path == "wiki/concepts/evil.md"
        for i in report.issues
    )
