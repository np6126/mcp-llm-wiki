"""Git mediation for the wiki tools.

Read-side tools refresh the working tree with a TTL-debounced
`git pull --rebase` before serving (see `server._refresh`); they share
the `pull_rebase` primitive below with the write path. Write-side tools
follow the full contract laid out in the plan:

  1. path safety (resolve_within)
  2. sanitisation
  3. git pull --rebase   ← refresh working tree
  4. ETag check vs. post-pull disk state   ← optimistic concurrency
  5. atomic write (tmp + fsync + rename)
  6. git add + commit (`--author=<agent-id>`)
  7. git push, retry on non-fast-forward by looping back to step 3

The retry loop is bounded; a stubborn non-FF after N attempts is
surfaced to the agent as a tool error rather than spinning forever.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from mcp_llm_wiki.path_safety import etag


class GitOpsError(Exception):
    """Surfaced as a tool error. Message is suitable for direct return."""


_MAX_PUSH_ATTEMPTS = 5


@dataclass(frozen=True)
class CommitResult:
    """Outcome of a successful write-and-push."""

    committed: bool
    """False when there was no on-disk change to commit (no-op)."""
    commit_sha: str | None
    pushed: bool


def _run(
    cmd: list[str],
    cwd: Path,
    *,
    check: bool = True,
    timeout: int = 30,
) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        cwd=cwd,
        check=check,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def pull_rebase(wiki_dir: Path) -> None:
    """Fetch + rebase onto origin/main. Raises GitOpsError on failure."""
    try:
        _run(["git", "pull", "--rebase", "--quiet"], wiki_dir)
    except subprocess.CalledProcessError as exc:
        # Abort any half-finished rebase so the working tree is back
        # in a sane state for the next call.
        _run(["git", "rebase", "--abort"], wiki_dir, check=False)
        raise GitOpsError(f"pull_failed: {exc.stderr.strip() or exc.stdout.strip()}") from exc


def atomic_write(target: Path, data: bytes) -> None:
    """Write `data` to `target` atomically via tmp-file + fsync + rename.

    Creates parent directories as needed.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, target)


def _index_dirty(wiki_dir: Path, paths: list[str]) -> bool:
    """Return True if the staged index differs from HEAD for `paths`.

    We stage first, then ask. This correctly reports "new file" as
    a diff (unstaged untracked files do not register against
    `git diff HEAD`).
    """
    result = _run(
        ["git", "diff", "--quiet", "--cached", "HEAD", "--", *paths],
        wiki_dir,
        check=False,
    )
    # exit 0 = no diff, 1 = diff present.
    return result.returncode != 0


def stage_commit_push(
    wiki_dir: Path,
    paths: list[str],
    *,
    author: str,
    message: str,
) -> CommitResult:
    """Stage `paths`, commit if changed, push with retry on non-FF.

    Paths are relative to `wiki_dir`. `author` is the in-VM agent
    identity; we render it as `name <name@llm-wiki.local>` for the
    commit-author header (git hosting web UIs key author identity on
    email).
    """
    _run(["git", "add", "--", *paths], wiki_dir)
    if not _index_dirty(wiki_dir, paths):
        return CommitResult(committed=False, commit_sha=None, pushed=False)

    author_token = f"{author} <{author}@llm-wiki.local>"
    _run(
        ["git", "commit", f"--author={author_token}", "-m", message],
        wiki_dir,
    )
    sha = _run(["git", "rev-parse", "HEAD"], wiki_dir).stdout.strip()

    last_err = ""
    for _ in range(_MAX_PUSH_ATTEMPTS):
        push = _run(["git", "push", "--quiet"], wiki_dir, check=False)
        if push.returncode == 0:
            return CommitResult(committed=True, commit_sha=sha, pushed=True)
        last_err = push.stderr.strip() or push.stdout.strip()
        # Non-fast-forward: rebase our commit on top of upstream and
        # try the push again.
        try:
            _run(["git", "pull", "--rebase", "--quiet"], wiki_dir)
        except subprocess.CalledProcessError as exc:
            _run(["git", "rebase", "--abort"], wiki_dir, check=False)
            raise GitOpsError(
                f"rebase_failed_during_push_retry: {exc.stderr.strip() or exc.stdout.strip()}"
            ) from exc
        # `git rev-parse HEAD` may have moved after the rebase; refresh
        # so the returned sha reflects the on-disk reality.
        sha = _run(["git", "rev-parse", "HEAD"], wiki_dir).stdout.strip()

    raise GitOpsError(
        f"push_failed_after_{_MAX_PUSH_ATTEMPTS}_attempts: {last_err}"
    )


def read_file_etag(target: Path) -> str | None:
    """Return the ETag of `target`'s current bytes, or None if missing."""
    if not target.exists():
        return None
    return etag(target.read_bytes())
