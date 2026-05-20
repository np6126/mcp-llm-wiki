"""Runtime configuration for mcp-llm-wiki.

All knobs come from environment variables that match the
tank-agent-os/sync-podman-secrets contract. Nothing is read from disk
besides the wiki working trees themselves.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _split_csv(value: str | None) -> list[str]:
    """Split a comma-separated env var into a clean list of names."""
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


@dataclass(frozen=True)
class Config:
    """Resolved server configuration.

    Two capability tiers per wiki:
      - wikis_rw: writes allowed (wiki_save, wiki_log_append)
      - wikis_readonly: reads only; writes return a tool error
    """

    root: Path
    """Filesystem root holding one subdirectory per wiki working tree."""

    wikis_rw: frozenset[str] = field(default_factory=frozenset)
    """Names of wikis the server is permitted to write to."""

    wikis_readonly: frozenset[str] = field(default_factory=frozenset)
    """Names of wikis the server may read but not write."""

    agent_identity: str = "mcp-llm-wiki"
    """Used as `git commit --author` for write operations."""

    port: int = 3100
    """TCP port the HTTP-MCP server listens on."""

    read_refresh_ttl_seconds: int = 30
    """How long a read may reuse the working tree before a git pull.

    Read tools refresh the tree with `git pull --rebase`, but at most
    once per this window — a burst of reads triggers a single pull.
    The value is the upper bound on how stale a read can be relative
    to the git host. 0 means pull on every read.
    """

    @property
    def known_wikis(self) -> frozenset[str]:
        return self.wikis_rw | self.wikis_readonly

    def can_write(self, wiki: str) -> bool:
        return wiki in self.wikis_rw

    def is_known(self, wiki: str) -> bool:
        return wiki in self.known_wikis

    def wiki_path(self, wiki: str) -> Path:
        """Return the working-tree path for `wiki`. Does not validate."""
        return self.root / wiki


def load_from_env() -> Config:
    """Resolve config from the AGENT_LLM_WIKI_* + MCP_LLM_WIKI_* env vars."""
    return Config(
        root=Path(os.environ.get("MCP_LLM_WIKI_ROOT", "/wikis")),
        wikis_rw=frozenset(_split_csv(os.environ.get("AGENT_LLM_WIKIS_RW"))),
        wikis_readonly=frozenset(_split_csv(os.environ.get("AGENT_LLM_WIKIS_READONLY"))),
        agent_identity=os.environ.get("AGENT_LLM_WIKI_USER", "mcp-llm-wiki"),
        port=int(os.environ.get("MCP_LLM_WIKI_PORT", "3100")),
        read_refresh_ttl_seconds=int(os.environ.get("MCP_LLM_WIKI_READ_TTL", "30")),
    )
