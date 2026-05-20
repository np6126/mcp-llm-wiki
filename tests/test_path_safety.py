"""Path-safety + ETag tests.

Each known CVE-class injection has its own regression test so a future
contributor sees exactly which property we're preserving.
"""

from pathlib import Path

import pytest

from mcp_llm_wiki.path_safety import PathSafetyError, etag, resolve_within


def test_resolve_within_simple_relative_path(tmp_path: Path):
    (tmp_path / "subdir").mkdir()
    resolved = resolve_within(tmp_path, "subdir/page.md")
    assert resolved == tmp_path / "subdir" / "page.md"


def test_resolve_within_returns_absolute(tmp_path: Path):
    resolved = resolve_within(tmp_path, "page.md")
    assert resolved.is_absolute()


def test_rejects_empty_path(tmp_path: Path):
    with pytest.raises(PathSafetyError, match="empty"):
        resolve_within(tmp_path, "")
    with pytest.raises(PathSafetyError, match="empty"):
        resolve_within(tmp_path, "   ")


def test_rejects_absolute_path(tmp_path: Path):
    with pytest.raises(PathSafetyError, match="absolute"):
        resolve_within(tmp_path, "/etc/passwd")


def test_rejects_parent_traversal(tmp_path: Path):
    with pytest.raises(PathSafetyError, match="traversal"):
        resolve_within(tmp_path, "../etc/passwd")
    with pytest.raises(PathSafetyError, match="traversal"):
        resolve_within(tmp_path, "subdir/../../etc/passwd")


def test_rejects_current_dir_segments(tmp_path: Path):
    with pytest.raises(PathSafetyError, match="current-dir"):
        resolve_within(tmp_path, "./page.md")


def test_rejects_symlink_at_leaf(tmp_path: Path):
    (tmp_path / "target").write_text("x")
    (tmp_path / "link.md").symlink_to(tmp_path / "target")
    with pytest.raises(PathSafetyError, match="symlink"):
        resolve_within(tmp_path, "link.md")


def test_rejects_symlink_in_path(tmp_path: Path):
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "link").symlink_to(real)
    with pytest.raises(PathSafetyError, match="symlink"):
        resolve_within(tmp_path, "link/page.md")


def test_rejects_symlink_to_outside_root(tmp_path: Path):
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    try:
        (tmp_path / "escape").symlink_to(outside)
        with pytest.raises(PathSafetyError, match="symlink"):
            resolve_within(tmp_path, "escape/secret")
    finally:
        outside.rmdir()


def test_accepts_nested_subdirs(tmp_path: Path):
    nested = tmp_path / "concepts" / "deep"
    nested.mkdir(parents=True)
    resolved = resolve_within(tmp_path, "concepts/deep/topic.md")
    assert resolved == nested / "topic.md"


def test_resolves_under_symlinked_root(tmp_path: Path):
    """If the wiki root itself is a symlink (e.g. /wikis -> /var/lib/wikis),
    that's fine — we resolve the root once and validate against the
    resolved form. Symlinks inside the tree are still rejected.
    """
    real = tmp_path / "real"
    real.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(real)
    resolved = resolve_within(linked, "page.md")
    # Both forms should resolve to the same real location.
    assert resolved.resolve() == (real / "page.md").resolve()


def test_etag_is_deterministic():
    assert etag("hello world") == etag("hello world")
    assert etag(b"hello world") == etag("hello world")


def test_etag_changes_on_content_change():
    assert etag("hello") != etag("hello ")
    assert etag("hello") != etag("Hello")


def test_etag_length_is_sha256_hex():
    h = etag("anything")
    assert len(h) == 64
    assert all(c in "0123456789abcdef" for c in h)
