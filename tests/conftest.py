"""Shared pytest fixtures for the mcp-llm-wiki suite.

The two heavy-weights here:
  - `wiki_root` — a temp filesystem with a known empty wiki named "test"
  - `bare_remote` — a paired bare repo so push-style tests have somewhere
    to push to (lives next to the working tree, file:// scheme)
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from mcp_llm_wiki.config import Config


def _run(cmd: list[str], cwd: Path) -> None:
    subprocess.run(cmd, cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def wiki_root(tmp_path: Path) -> Path:
    """Empty filesystem root with a `test` wiki working tree pre-cloned
    from a same-tmp bare remote, so push-style operations in tests have
    a deterministic local target.
    """
    bare = tmp_path / "test.git"
    work = tmp_path / "test"
    _run(["git", "init", "--quiet", "--bare", "--initial-branch=main", str(bare)], tmp_path)
    _run(["git", "init", "--quiet", "--initial-branch=main", str(work)], tmp_path)
    _run(["git", "config", "user.email", "test@example.com"], work)
    _run(["git", "config", "user.name", "test"], work)
    # Seed an empty commit so subsequent push/pull operations don't trip
    # over the "you're on an unborn branch" rough edge.
    _run(["git", "commit", "--quiet", "--allow-empty", "-m", "init"], work)
    _run(["git", "remote", "add", "origin", str(bare)], work)
    _run(["git", "push", "--quiet", "origin", "main"], work)
    return tmp_path


@pytest.fixture
def config(wiki_root: Path) -> Config:
    return Config(
        root=wiki_root,
        wikis_rw=frozenset({"test"}),
        wikis_readonly=frozenset(),
        agent_identity="test-agent",
        port=3100,
    )


@pytest.fixture(autouse=True)
def _reset_refresh_state():
    """Clear the module-level TTL-refresh cache between tests so one
    test's pull timestamps never debounce a pull in the next."""
    from mcp_llm_wiki import server

    server._last_refresh.clear()
    yield
    server._last_refresh.clear()
