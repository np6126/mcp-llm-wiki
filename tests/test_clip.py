"""Tests for wiki-clip — the raw/ web-source clipper.

The network fetch and the markitdown conversion are mocked; the suite
exercises slugging, provenance frontmatter, the write + git-stage step,
and the CLI guard rails. markitdown itself is not a test dependency —
clip.to_markdown imports it lazily and the one conversion test injects
a fake module.
"""

from __future__ import annotations

import subprocess
import sys
import types
from datetime import datetime
from pathlib import Path

import frontmatter
import pytest

from mcp_llm_wiki import clip


def _git(args: list[str], cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def wiki_repo(tmp_path: Path) -> Path:
    """A minimal git working tree with the raw/ + wiki/ layout."""
    repo = tmp_path / "wiki-test"
    (repo / "raw").mkdir(parents=True)
    (repo / "wiki").mkdir()
    _git(["init", "--quiet", "--initial-branch=main"], repo)
    _git(["config", "user.email", "t@example.com"], repo)
    _git(["config", "user.name", "t"], repo)
    _git(["commit", "--quiet", "--allow-empty", "-m", "init"], repo)
    return repo


# --- slugify ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("App architecture recommendations", "app_architecture_recommendations"),
        ("  Mixed CASE / punct! ", "mixed_case_punct"),
        ("https://developer.android.com/x", "https_developer_android_com_x"),
        ("Empfehlungen für Ältere Geräte", "empfehlungen_fur_altere_gerate"),
        ("___", "source"),
        ("", "source"),
    ],
)
def test_slugify(raw, expected):
    assert clip.slugify(raw) == expected


def test_slugify_truncates_and_trims():
    slug = clip.slugify("word " * 50)
    assert len(slug) <= 60
    assert not slug.startswith("_")
    assert not slug.endswith("_")


# --- build_raw_document ----------------------------------------------------


def test_build_raw_document_carries_provenance():
    doc = clip.build_raw_document(
        "# Body\n\ntext",
        source_url="https://example.com/page",
        title="Example Page",
        fetched="2026-05-21T00:00:00+00:00",
    )
    assert doc.startswith("---\n")
    assert "source_url: https://example.com/page" in doc
    assert "clipped_by: wiki-clip" in doc
    assert doc.rstrip().endswith("text")


def test_build_raw_document_frontmatter_is_valid_yaml():
    # A title with YAML metacharacters must be escaped, not break the file.
    doc = clip.build_raw_document(
        "body",
        source_url="https://x.com",
        title='Tricky: "quoted" — and: colons',
        fetched="2026-05-21",
    )
    post = frontmatter.loads(doc)
    assert post["source_url"] == "https://x.com"
    assert post["title"] == 'Tricky: "quoted" — and: colons'
    assert post.content.strip() == "body"


def test_build_raw_document_falls_back_to_url_title():
    doc = clip.build_raw_document(
        "b", source_url="https://x.com/p", title="", fetched="2026-05-21"
    )
    assert frontmatter.loads(doc)["title"] == "https://x.com/p"


# --- clip (fetch + convert mocked) -----------------------------------------


def test_clip_writes_and_stages_but_does_not_commit(wiki_repo, monkeypatch):
    monkeypatch.setattr(clip, "fetch", lambda url: b"<html>...</html>")
    monkeypatch.setattr(
        clip,
        "to_markdown",
        lambda html, url: ("# Recs\n\nbody", "App architecture recommendations"),
    )
    target = clip.clip(
        wiki_repo, "https://developer.android.com/topic/architecture/recommendations"
    )

    assert target == wiki_repo / "raw" / "app_architecture_recommendations.md"
    post = frontmatter.loads(target.read_text())
    assert post.content.strip() == "# Recs\n\nbody"
    assert post["source_url"] == (
        "https://developer.android.com/topic/architecture/recommendations"
    )
    datetime.fromisoformat(post["fetched"])  # provenance timestamp is well-formed

    staged = subprocess.run(
        ["git", "diff", "--cached", "--name-only"],
        cwd=wiki_repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    assert "raw/app_architecture_recommendations.md" in staged

    commits = subprocess.run(
        ["git", "log", "--oneline"],
        cwd=wiki_repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    assert len(commits) == 1  # only the seed commit — wiki-clip made none


def test_clip_name_override_wins_over_title(wiki_repo, monkeypatch):
    monkeypatch.setattr(clip, "fetch", lambda url: b"x")
    monkeypatch.setattr(clip, "to_markdown", lambda html, url: ("body", "Some Page Title"))
    target = clip.clip(wiki_repo, "https://x.com", name="custom name")
    assert target.name == "custom_name.md"


# --- to_markdown (markitdown injected) -------------------------------------


def _install_fake_markitdown(monkeypatch, *, markdown, text_content, title):
    """Inject a fake `markitdown` module; return the convert_stream kwargs dict."""
    captured: dict = {}

    class _Result:
        pass

    _Result.markdown = markdown
    _Result.text_content = text_content
    _Result.title = title

    class _MarkItDown:
        def convert_stream(self, stream, **kwargs):
            captured["kwargs"] = kwargs
            return _Result()

    fake = types.ModuleType("markitdown")
    fake.MarkItDown = _MarkItDown
    monkeypatch.setitem(sys.modules, "markitdown", fake)
    return captured


def test_to_markdown_prefers_markdown_and_passes_html_hint(monkeypatch):
    captured = _install_fake_markitdown(
        monkeypatch,
        markdown="# from markdown",
        text_content="# from text_content",
        title="Converted Title",
    )
    body, title = clip.to_markdown(b"<html></html>", "https://x.com")
    assert body == "# from markdown"  # markdown wins over text_content
    assert title == "Converted Title"
    assert captured["kwargs"]["url"] == "https://x.com"
    assert captured["kwargs"]["file_extension"] == ".html"


def test_to_markdown_falls_back_to_text_content(monkeypatch):
    _install_fake_markitdown(
        monkeypatch, markdown="", text_content="# fallback body", title="T"
    )
    body, _ = clip.to_markdown(b"<html></html>", "https://x.com")
    assert body == "# fallback body"


# --- fetch scheme guard ----------------------------------------------------


@pytest.mark.parametrize("bad", ["file:///etc/passwd", "ftp://host/x", "/local/path"])
def test_fetch_rejects_non_http_scheme(bad):
    with pytest.raises(ValueError, match="http"):
        clip.fetch(bad)


# --- main ------------------------------------------------------------------


def test_main_rejects_non_git_dir(tmp_path, capsys):
    rc = clip.main([str(tmp_path), "https://x.com"])
    assert rc == 2
    assert "not a git working tree" in capsys.readouterr().err


def test_main_reports_fetch_error(wiki_repo, monkeypatch, capsys):
    def _boom(url):
        raise ValueError("only http(s) URLs are supported")

    monkeypatch.setattr(clip, "fetch", _boom)
    rc = clip.main([str(wiki_repo), "file:///etc/passwd"])
    assert rc == 1
    assert "wiki-clip:" in capsys.readouterr().err


def test_main_reports_missing_markitdown(wiki_repo, monkeypatch, capsys):
    monkeypatch.setattr(clip, "fetch", lambda url: b"<html></html>")

    def _no_markitdown(html, url):
        raise ModuleNotFoundError("No module named 'markitdown'", name="markitdown")

    monkeypatch.setattr(clip, "to_markdown", _no_markitdown)
    rc = clip.main([str(wiki_repo), "https://x.com"])
    assert rc == 1
    assert "clip extra" in capsys.readouterr().err
