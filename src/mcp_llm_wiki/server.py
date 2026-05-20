"""MCP server entrypoint — 8 tools that operate on git-backed wikis.

The tools delegate to ``wiki_io`` for filesystem reads, ``path_safety``
for containment, ``sanitizer`` for paranoid write-side cleaning, and
``git_ops`` for git mediation on the two writing tools.
"""

from __future__ import annotations

import argparse
import base64
import logging
import sys
import threading
import time
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from mcp_llm_wiki import git_ops, wiki_io
from mcp_llm_wiki.config import Config, load_from_env
from mcp_llm_wiki.path_safety import etag as compute_etag
from mcp_llm_wiki.path_safety import resolve_within
from mcp_llm_wiki.sanitizer import sanitize

log = logging.getLogger("mcp_llm_wiki")

# TTL-debounced working-tree refresh state (see _refresh). Keyed by the
# absolute wiki-dir path, so distinct clones of the same wiki name in
# one process — e.g. the multi-VM tests — never share a timestamp.
_REFRESH_LOCK = threading.Lock()
_last_refresh: dict[str, float] = {}


class WikiToolError(Exception):
    """Raised inside a tool body to signal a structured failure.

    FastMCP turns Exceptions into MCP tool errors automatically; we
    keep a dedicated subclass so the message convention is consistent
    (`code: detail`).
    """


def _require_known(config: Config, wiki: str) -> None:
    if not config.is_known(wiki):
        raise WikiToolError(
            f"unknown_wiki: '{wiki}' is not in AGENT_LLM_WIKIS_RW/READONLY"
        )


def _require_writable(config: Config, wiki: str) -> None:
    _require_known(config, wiki)
    if not config.can_write(wiki):
        raise WikiToolError(
            f"read_only: wiki '{wiki}' is configured read-only for this VM"
        )


def _refresh(config: Config, wiki: str) -> None:
    """Refresh a wiki's working tree before a read, debounced by a TTL.

    Every read tool calls this. A `git pull --rebase` runs only when the
    tree has not been refreshed within `config.read_refresh_ttl_seconds`,
    so a burst of reads triggers at most one pull and the TTL bounds how
    stale a read can be relative to the git host.

    If the pull fails — git host briefly unreachable — the read still
    succeeds against the local working tree; bounded staleness beats a
    hard error. The timestamp is recorded even on failure, so a down
    host does not turn every following read into a fresh network call.

    Skips git entirely if `wiki_dir` is not a git working tree: that
    covers ad-hoc dev runs and the lighter unit tests. Production
    always has .git/ since `llm-wiki-init` clones on container start.
    """
    wiki_dir = config.wiki_path(wiki)
    if not wiki_dir.is_dir():
        raise WikiToolError(
            f"wiki_not_cloned: '{wiki}' configured but not present at {wiki_dir}"
        )
    if not (wiki_dir / ".git").exists():
        return
    key = str(wiki_dir)
    # A single process-wide lock is held across the pull: any concurrent
    # read waits for it, so no reader serves a tree mid-rebase. It also
    # serialises pulls of different wikis — fine here, since pulls are
    # rare (TTL-debounced) and the server is low-concurrency.
    with _REFRESH_LOCK:
        last = _last_refresh.get(key)
        if last is not None and time.monotonic() - last < config.read_refresh_ttl_seconds:
            return
        try:
            git_ops.pull_rebase(wiki_dir)
        except git_ops.GitOpsError as exc:
            log.warning(
                "refresh: pull failed for '%s'; serving local tree (%s)", wiki, exc
            )
        _last_refresh[key] = time.monotonic()


def build_server(config: Config) -> FastMCP:
    """Construct a FastMCP server with the 8 wiki tools registered.

    Kept as a function so tests can build an instance with a custom
    config without touching env vars.
    """
    mcp = FastMCP(
        name="mcp-llm-wiki",
        instructions=(
            "Karpathy-style LLM wikis, git-backed. Pages live under "
            "wiki/<page>.md, immutable sources under raw/. Writes are "
            "ETag-guarded. Every wiki_save call also commits and pushes."
        ),
        host="0.0.0.0",  # noqa: S104 — container-bound, port closed by firewall
        port=config.port,
    )

    @mcp.tool(
        name="wiki_list",
        annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
        description="List pages in a wiki with frontmatter summary.",
    )
    def wiki_list(wiki: str) -> list[dict[str, Any]]:
        _require_known(config, wiki)
        _refresh(config, wiki)
        return wiki_io.page_summary_dicts(wiki_io.list_pages(config.wiki_path(wiki)))

    @mcp.tool(
        name="wiki_read",
        annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
        description=(
            "Read a wiki page. Returns body, frontmatter, outgoing links, "
            "and an ETag the caller can quote in wiki_save."
        ),
    )
    def wiki_read(wiki: str, page: str) -> dict[str, Any]:
        _require_known(config, wiki)
        _refresh(config, wiki)
        return wiki_io.page_content_dict(wiki_io.read_page(config.wiki_path(wiki), page))

    @mcp.tool(
        name="wiki_read_raw",
        annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
        description=(
            "Read a source from a wiki's raw/ layer. The raw layer is "
            "immutable from this server's perspective — there is no "
            "write counterpart. Binary-safe (returns base64 + size)."
        ),
    )
    def wiki_read_raw(wiki: str, path: str) -> dict[str, Any]:
        _require_known(config, wiki)
        _refresh(config, wiki)
        data = wiki_io.read_raw(config.wiki_path(wiki), path)
        return {
            "path": path,
            "size": len(data),
            "content_base64": base64.b64encode(data).decode("ascii"),
        }

    @mcp.tool(
        name="wiki_search",
        annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
        description=(
            "Fixed-string search over wiki page bodies. Phase-1: "
            "ripgrep-backed; FTS5 hybrid lands later."
        ),
    )
    def wiki_search(wiki: str, query: str, limit: int = 50) -> list[dict[str, Any]]:
        _require_known(config, wiki)
        _refresh(config, wiki)
        return wiki_io.search_hit_dicts(
            wiki_io.search(config.wiki_path(wiki), query, limit=limit)
        )

    @mcp.tool(
        name="wiki_save",
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
        description=(
            "Upsert a page. Sanitises content (HTML comments, zero-width, "
            "bidi, raw HTML, inline styles, data: images) before write. "
            "If 'etag' is supplied and does not match the on-disk page, "
            "the call fails so the agent can re-read and retry. "
            "On success: atomic write, commit, push."
        ),
    )
    def wiki_save(
        wiki: str, page: str, content: str, etag: str | None = None
    ) -> dict[str, Any]:
        _require_writable(config, wiki)
        wiki_dir = config.wiki_path(wiki)
        target = resolve_within(wiki_dir / "wiki", page)

        cleaned, report = sanitize(content)
        cleaned_bytes = cleaned.encode("utf-8")

        # Refresh working tree before checking ETag — the agent's
        # snapshot must be compared against the latest upstream state.
        git_ops.pull_rebase(wiki_dir)

        current_etag = git_ops.read_file_etag(target)
        if etag is not None and etag != current_etag:
            raise WikiToolError(
                f"etag_mismatch: page changed since you read it. "
                f"current_etag={current_etag!r}; please re-read and retry."
            )

        git_ops.atomic_write(target, cleaned_bytes)
        new_etag = compute_etag(cleaned_bytes)

        rel = target.relative_to(wiki_dir).as_posix()
        message = f"wiki: update {page}\n\n{report.summary()}"
        result = git_ops.stage_commit_push(
            wiki_dir,
            [rel],
            author=config.agent_identity,
            message=message,
        )
        return {
            "path": page,
            "etag": new_etag,
            "committed": result.committed,
            "commit_sha": result.commit_sha,
            "pushed": result.pushed,
            "sanitize_report": report.summary(),
        }

    @mcp.tool(
        name="wiki_log_append",
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=False,
        ),
        description=(
            "Append one timestamped entry to log.md. Sanitises the entry "
            "first. Commit + push, same as wiki_save."
        ),
    )
    def wiki_log_append(wiki: str, entry: str) -> dict[str, Any]:
        _require_writable(config, wiki)
        wiki_dir = config.wiki_path(wiki)
        cleaned, report = sanitize(entry)
        cleaned = cleaned.strip()
        if not cleaned:
            raise WikiToolError("empty_entry: log entry is empty after sanitisation")

        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        line = f"[{timestamp}] {config.agent_identity} | {cleaned}\n"

        git_ops.pull_rebase(wiki_dir)
        log_path = wiki_dir / "log.md"
        existing = log_path.read_text(encoding="utf-8") if log_path.exists() else "# Log\n\n"
        # Ensure the file ends with a newline before we append; otherwise
        # our entry would glue onto the previous line.
        if not existing.endswith("\n"):
            existing += "\n"
        new_text = existing + line

        git_ops.atomic_write(log_path, new_text.encode("utf-8"))
        message = f"log: append entry\n\n{report.summary()}"
        result = git_ops.stage_commit_push(
            wiki_dir,
            ["log.md"],
            author=config.agent_identity,
            message=message,
        )
        return {
            "appended": line,
            "committed": result.committed,
            "commit_sha": result.commit_sha,
            "pushed": result.pushed,
            "sanitize_report": report.summary(),
        }

    @mcp.tool(
        name="wiki_lint",
        annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
        description=(
            "Heuristic drift report: orphan pages, broken wikilinks, "
            "stale-by-age. Never mutates."
        ),
    )
    def wiki_lint(wiki: str, stale_days: int = 180) -> dict[str, Any]:
        _require_known(config, wiki)
        _refresh(config, wiki)
        return wiki_io.lint_report_dict(
            wiki_io.lint(config.wiki_path(wiki), stale_days=stale_days)
        )

    @mcp.tool(
        name="wiki_graph",
        annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
        description=(
            "Backlinks map: for each page, the list of pages that link "
            "to it. Parses both [[wikilinks]] and Markdown md-links."
        ),
    )
    def wiki_graph(wiki: str) -> dict[str, list[str]]:
        _require_known(config, wiki)
        _refresh(config, wiki)
        return wiki_io.build_graph(config.wiki_path(wiki))

    return mcp


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="mcp-llm-wiki",
        description="MCP server exposing git-backed LLM wikis",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="TCP port (overrides MCP_LLM_WIKI_PORT)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    args = _parse_args(argv)
    config = load_from_env()
    if args.port is not None:
        config = replace(config, port=args.port)
    log.info(
        "mcp-llm-wiki starting on :%d wikis rw=%s readonly=%s",
        config.port,
        sorted(config.wikis_rw),
        sorted(config.wikis_readonly),
    )
    server = build_server(config)
    server.run(transport="streamable-http")
    return 0


if __name__ == "__main__":
    sys.exit(main())
