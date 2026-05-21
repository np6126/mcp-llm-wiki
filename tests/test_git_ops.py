"""Integration tests for git_ops against real git repositories.

The happy path of stage_commit_push / pull_rebase is exercised
end-to-end by test_server_writes and test_e2e_multi_vm. This file
targets the branches those do not reach: the non-fast-forward push
retry loop and the pull-rebase failure path.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from mcp_llm_wiki import git_ops


def _run(cmd: list[str], cwd: Path) -> None:
    subprocess.run(cmd, cwd=cwd, check=True, capture_output=True)


def _clone(bare: Path, dest: Path) -> Path:
    """Clone `bare` into `dest` and give it a commit identity."""
    _run(["git", "clone", "--quiet", str(bare), str(dest)], dest.parent)
    _run(["git", "config", "user.email", "t@example.com"], dest)
    _run(["git", "config", "user.name", "t"], dest)
    return dest


@pytest.fixture
def bare(tmp_path: Path) -> Path:
    """A bare repo seeded with one commit on `main`."""
    bare = tmp_path / "wiki.git"
    _run(["git", "init", "--quiet", "--bare", "--initial-branch=main", str(bare)], tmp_path)
    seed = _clone(bare, tmp_path / "seed")
    (seed / "seed.md").write_text("seed\n")
    _run(["git", "add", "-A"], seed)
    _run(["git", "commit", "--quiet", "-m", "seed"], seed)
    _run(["git", "push", "--quiet", "origin", "main"], seed)
    return bare


def _bare_files(bare: Path) -> set[str]:
    out = subprocess.run(
        ["git", "ls-tree", "--name-only", "main"],
        cwd=bare,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return set(out.split())


def test_stage_commit_push_retries_on_non_fast_forward(bare, tmp_path):
    """A push that loses the race to a concurrent commit is not lost:
    stage_commit_push rebases onto the new tip and pushes again."""
    a = _clone(bare, tmp_path / "a")
    b = _clone(bare, tmp_path / "b")

    # B commits and pushes, advancing the bare's main.
    (b / "b.md").write_text("from b\n")
    b_result = git_ops.stage_commit_push(b, ["b.md"], author="vm-b", message="b: add")
    assert b_result.pushed

    # A commits on its now-stale tip; its first push is a non-fast-forward,
    # so stage_commit_push must pull --rebase and retry.
    (a / "a.md").write_text("from a\n")
    a_result = git_ops.stage_commit_push(a, ["a.md"], author="vm-a", message="a: add")
    assert a_result.committed and a_result.pushed

    # Both files reached the bare's main — neither write was dropped.
    assert {"a.md", "b.md"} <= _bare_files(bare)


def test_stage_commit_push_raises_after_exhausting_retries(bare, tmp_path):
    """When every push is rejected, stage_commit_push gives up after
    _MAX_PUSH_ATTEMPTS and raises rather than looping forever."""
    # A pre-receive hook on the bare rejects every push outright.
    hook = bare / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)

    a = _clone(bare, tmp_path / "a")
    (a / "a.md").write_text("from a\n")
    with pytest.raises(
        git_ops.GitOpsError,
        match=f"push_failed_after_{git_ops._MAX_PUSH_ATTEMPTS}_attempts",
    ):
        git_ops.stage_commit_push(a, ["a.md"], author="vm-a", message="a: add")


def test_stage_commit_push_aborts_when_retry_rebase_conflicts(bare, tmp_path):
    """If the pull --rebase used to recover from a non-fast-forward push
    itself hits a conflict, stage_commit_push aborts that rebase and
    raises — it never leaves a half-finished rebase behind."""
    a = _clone(bare, tmp_path / "a")
    b = _clone(bare, tmp_path / "b")

    # B edits seed.md and pushes, advancing the bare.
    (b / "seed.md").write_text("from b\n")
    git_ops.stage_commit_push(b, ["seed.md"], author="vm-b", message="b: edit")

    # A edits the SAME file: its push is non-fast-forward, and the retry's
    # pull --rebase then conflicts on seed.md.
    (a / "seed.md").write_text("from a\n")
    with pytest.raises(git_ops.GitOpsError, match="rebase_failed_during_push_retry"):
        git_ops.stage_commit_push(a, ["seed.md"], author="vm-a", message="a: edit")

    assert not (a / ".git" / "rebase-merge").exists()
    assert not (a / ".git" / "rebase-apply").exists()


def test_pull_rebase_raises_and_aborts_on_conflict(bare, tmp_path):
    """A pull --rebase that cannot replay cleanly is aborted — leaving
    the working tree sane — and surfaces as a GitOpsError."""
    a = _clone(bare, tmp_path / "a")
    b = _clone(bare, tmp_path / "b")

    # B changes seed.md and pushes.
    (b / "seed.md").write_text("from b\n")
    git_ops.stage_commit_push(b, ["seed.md"], author="vm-b", message="b: edit")

    # A makes a conflicting change to the same file and commits locally.
    (a / "seed.md").write_text("from a\n")
    _run(["git", "add", "-A"], a)
    _run(["git", "commit", "--quiet", "-m", "a: edit"], a)

    with pytest.raises(git_ops.GitOpsError, match="pull_failed"):
        git_ops.pull_rebase(a)

    # The failed rebase was aborted: no rebase is left in progress.
    assert not (a / ".git" / "rebase-merge").exists()
    assert not (a / ".git" / "rebase-apply").exists()
