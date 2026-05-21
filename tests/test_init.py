"""Tests for wiki-init — the wiki-structure seeder / reset tool.

Exercises the fresh-create path, the destructive re-init path with its
confirmation gate, the staging behaviour, and the guarantee that git
history is never touched.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from mcp_llm_wiki import init

_GITATTRIBUTES = "log.md merge=llm-wiki-log\nindex.md merge=llm-wiki-index\n"


class _FakeTTY:
    """Stand-in for an interactive stdin in confirmation tests."""

    def isatty(self) -> bool:
        return True


def _git(args: list[str], cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _staged(repo: Path) -> set[str]:
    out = subprocess.run(
        ["git", "diff", "--cached", "--name-only"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return set(out.split())


def _head(repo: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


@pytest.fixture
def fresh_repo(tmp_path: Path) -> Path:
    """A git working tree with no wiki structure yet."""
    repo = tmp_path / "wiki-fresh"
    repo.mkdir()
    _git(["init", "--quiet", "--initial-branch=main"], repo)
    _git(["config", "user.email", "t@example.com"], repo)
    _git(["config", "user.name", "t"], repo)
    _git(["commit", "--quiet", "--allow-empty", "-m", "init"], repo)
    return repo


@pytest.fixture
def populated_repo(fresh_repo: Path) -> Path:
    """A wiki with pages, a raw source, log entries, and a real index."""
    (fresh_repo / "wiki").mkdir()
    (fresh_repo / "raw").mkdir()
    (fresh_repo / "wiki" / "index.md").write_text("# Index\n\n## Pages\n- [[foo]]\n")
    (fresh_repo / "wiki" / "foo.md").write_text("---\ntitle: Foo\n---\nFoo body.\n")
    (fresh_repo / "raw" / "bar.txt").write_text("raw source bytes")
    (fresh_repo / "log.md").write_text("# Log\n\n## [2026-05-21T00:00:00Z abc] a thing\n")
    _git(["add", "-A"], fresh_repo)
    _git(["commit", "--quiet", "-m", "populate"], fresh_repo)
    return fresh_repo


def test_init_rejects_non_git_dir(tmp_path, capsys):
    rc = init.main([str(tmp_path)])
    assert rc == 2
    assert "not a git working tree" in capsys.readouterr().err


def test_init_fresh_creates_structure(fresh_repo):
    rc = init.main([str(fresh_repo)])
    assert rc == 0
    assert (fresh_repo / "wiki" / "index.md").read_text() == "# Index\n\n"
    assert (fresh_repo / "raw" / ".gitkeep").is_file()
    assert (fresh_repo / "log.md").read_text() == "# Log\n\n"
    assert (fresh_repo / ".gitattributes").read_text() == _GITATTRIBUTES
    assert _staged(fresh_repo) >= {
        "wiki/index.md",
        "raw/.gitkeep",
        "log.md",
        ".gitattributes",
    }


def test_init_fresh_stages_but_does_not_commit(fresh_repo):
    head_before = _head(fresh_repo)
    assert init.main([str(fresh_repo)]) == 0
    # The seed is staged for review — wiki-init never commits.
    assert _head(fresh_repo) == head_before


def test_init_on_seeded_wiki_does_not_prompt(fresh_repo):
    # A freshly seeded wiki holds only structural content, so a second
    # run treats it as not-populated: no prompt, no error.
    assert init.main([str(fresh_repo)]) == 0
    assert init.main([str(fresh_repo)]) == 0


def test_init_defaults_wiki_dir_to_cwd(fresh_repo, monkeypatch):
    # Invoked with no path, wiki-init seeds the current directory.
    monkeypatch.chdir(fresh_repo)
    rc = init.main([])
    assert rc == 0
    assert (fresh_repo / "wiki" / "index.md").read_text() == "# Index\n\n"
    assert (fresh_repo / ".gitattributes").read_text() == _GITATTRIBUTES


def test_init_clears_populated_with_yes(populated_repo):
    rc = init.main([str(populated_repo), "--yes"])
    assert rc == 0
    assert not (populated_repo / "wiki" / "foo.md").exists()
    assert not (populated_repo / "raw" / "bar.txt").exists()
    assert (populated_repo / "wiki" / "index.md").read_text() == "# Index\n\n"
    assert (populated_repo / "log.md").read_text() == "# Log\n\n"
    assert (populated_repo / "raw" / ".gitkeep").is_file()


def test_init_aborts_without_confirmation(populated_repo, capsys):
    # Non-interactive (pytest stdin is not a tty) and no --yes.
    rc = init.main([str(populated_repo)])
    assert rc == 1
    assert "refusing to clear without confirmation" in capsys.readouterr().err
    # Nothing was cleared.
    assert (populated_repo / "wiki" / "foo.md").exists()
    assert (populated_repo / "raw" / "bar.txt").exists()


def test_init_confirmation_accepts_yes(populated_repo, monkeypatch):
    monkeypatch.setattr(sys, "stdin", _FakeTTY())
    monkeypatch.setattr("builtins.input", lambda prompt="": "yes")
    rc = init.main([str(populated_repo)])
    assert rc == 0
    assert not (populated_repo / "wiki" / "foo.md").exists()


def test_init_confirmation_rejects_other_input(populated_repo, monkeypatch):
    monkeypatch.setattr(sys, "stdin", _FakeTTY())
    monkeypatch.setattr("builtins.input", lambda prompt="": "no")
    rc = init.main([str(populated_repo)])
    assert rc == 1
    # The page survives an aborted re-init.
    assert (populated_repo / "wiki" / "foo.md").exists()


def test_init_preserves_git_history(populated_repo):
    head_before = _head(populated_repo)
    rc = init.main([str(populated_repo), "--yes"])
    assert rc == 0
    # No commit, no rewrite: HEAD is exactly where it was — every prior
    # commit is still reachable.
    assert _head(populated_repo) == head_before


def test_init_clear_stages_the_deletions(populated_repo):
    assert init.main([str(populated_repo), "--yes"]) == 0
    # The removed page is staged as a deletion for the operator to commit.
    deleted = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=D"],
        cwd=populated_repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    assert "wiki/foo.md" in deleted
    assert "raw/bar.txt" in deleted


def test_init_reports_git_add_failure(fresh_repo, monkeypatch, capsys):
    real_run = init.subprocess.run

    def _run(cmd, *args, **kwargs):
        if cmd[:2] == ["git", "add"]:
            raise subprocess.CalledProcessError(1, cmd, stderr="git add boom")
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(init.subprocess, "run", _run)
    rc = init.main([str(fresh_repo)])
    assert rc == 1
    assert "git add failed" in capsys.readouterr().err
