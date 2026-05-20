"""Pure filesystem operations on a wiki working tree.

The git side lives in `git_ops`; this module only reads and writes
files within an already-cloned working tree. All callers go through
`path_safety.resolve_within` before reaching here.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import frontmatter  # type: ignore[import-untyped]

from mcp_llm_wiki.path_safety import etag, resolve_within

# Match Obsidian-style wikilinks: [[page]] or [[page|alias]] or
# [[page#heading]]. Strips alias / heading for the link target.
_WIKILINK_RE = re.compile(r"\[\[([^\]\|#]+)(?:[\|#][^\]]*)?\]\]")

# Match Markdown links to local .md files: [text](relative/page.md).
# Excludes http/https/mailto/data schemes.
_MD_LINK_RE = re.compile(r"\[[^\]]+\]\((?!\w+:)([^)#]+?\.md)(?:#[^)]*)?\)")


@dataclass
class PageSummary:
    """Bookkeeping projection for `wiki_list`."""

    path: str
    """Relative path within the wiki, POSIX-style separators."""

    title: str | None = None
    kind: str | None = None
    tags: list[str] = field(default_factory=list)
    updated: str | None = None


@dataclass
class PageContent:
    """Full return shape for `wiki_read`."""

    path: str
    content: str
    """Markdown body (frontmatter already extracted into `frontmatter`)."""
    frontmatter: dict
    outgoing_links: list[str]
    """De-duplicated targets of [[wikilinks]] and Markdown md-links."""
    etag: str


@dataclass
class SearchHit:
    """One result row from `wiki_search`."""

    path: str
    line: int
    snippet: str


@dataclass
class LintIssue:
    kind: str
    path: str
    message: str


@dataclass
class LintReport:
    issues: list[LintIssue] = field(default_factory=list)

    @property
    def is_clean(self) -> bool:
        return not self.issues


def _iter_pages(wiki_dir: Path) -> list[Path]:
    """Return all .md files under wiki/<wiki_dir>/wiki/ , sorted."""
    base = wiki_dir / "wiki"
    if not base.is_dir():
        return []
    return sorted(p for p in base.rglob("*.md") if p.is_file())


def _rel(wiki_dir: Path, path: Path) -> str:
    return path.relative_to(wiki_dir).as_posix()


def _parse_page(text: str) -> tuple[dict, str]:
    """Split YAML frontmatter from body. On any parse failure fall back
    to empty metadata and the whole text as body."""
    try:
        post = frontmatter.loads(text)
        return dict(post.metadata or {}), post.content
    except Exception:
        return {}, text


def list_pages(wiki_dir: Path) -> list[PageSummary]:
    """Walk `wiki_dir/wiki/`, return one summary per page."""
    out: list[PageSummary] = []
    for p in _iter_pages(wiki_dir):
        try:
            post = frontmatter.load(p)
            fm = post.metadata or {}
        except Exception:
            fm = {}
        out.append(
            PageSummary(
                path=_rel(wiki_dir, p),
                title=fm.get("title"),
                kind=fm.get("kind"),
                tags=list(fm.get("tags") or []),
                updated=str(fm.get("updated")) if fm.get("updated") else None,
            )
        )
    return out


def read_page(wiki_dir: Path, page_rel: str) -> PageContent:
    """Read a page from `wiki_dir/wiki/<page_rel>`.

    `page_rel` is provided relative to the wiki/ subdirectory (the
    caller doesn't need to know about the wiki/ prefix).
    """
    safe = resolve_within(wiki_dir / "wiki", page_rel)
    raw = safe.read_bytes()
    fm, body = _parse_page(raw.decode("utf-8"))
    links = _extract_outgoing_links(body)
    return PageContent(
        path=page_rel,
        content=body,
        frontmatter=fm,
        outgoing_links=links,
        etag=etag(raw),
    )


def read_raw(wiki_dir: Path, path_rel: str) -> bytes:
    """Read a source from `wiki_dir/raw/<path_rel>`.

    No frontmatter parsing, no decoding — the raw/ layer holds whatever
    the operator placed there (PDFs, transcripts, etc.).
    """
    safe = resolve_within(wiki_dir / "raw", path_rel)
    return safe.read_bytes()


def search(wiki_dir: Path, query: str, limit: int = 50) -> list[SearchHit]:
    """ripgrep over `wiki_dir/wiki/`. Phase-1 search; FTS5 lands later.

    `query` is treated as a fixed string (no regex) for predictable
    agent-facing semantics.
    """
    base = wiki_dir / "wiki"
    if not base.is_dir() or not query:
        return []
    result = subprocess.run(
        [
            "rg",
            "--fixed-strings",
            "--no-heading",
            "--with-filename",
            "--line-number",
            "--max-count",
            str(limit),
            "--",
            query,
            str(base),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode not in (0, 1):
        # rg returns 1 on no-match; 2+ on real error.
        raise RuntimeError(f"ripgrep failed: {result.stderr}")
    hits: list[SearchHit] = []
    for line in result.stdout.splitlines():
        if ":" not in line:
            continue
        try:
            path_str, lineno_str, snippet = line.split(":", 2)
        except ValueError:
            continue
        path = Path(path_str)
        try:
            rel = path.relative_to(wiki_dir).as_posix()
        except ValueError:
            continue
        try:
            lineno = int(lineno_str)
        except ValueError:
            continue
        hits.append(SearchHit(path=rel, line=lineno, snippet=snippet))
        if len(hits) >= limit:
            break
    return hits


def _extract_outgoing_links(body: str) -> list[str]:
    """Return de-duplicated wikilink + md-link targets in document order."""
    seen: set[str] = set()
    out: list[str] = []
    for match in _WIKILINK_RE.finditer(body):
        target = match.group(1).strip()
        if target and target not in seen:
            seen.add(target)
            out.append(target)
    for match in _MD_LINK_RE.finditer(body):
        target = match.group(1).strip()
        if target and target not in seen:
            seen.add(target)
            out.append(target)
    return out


@dataclass
class _PageRecord:
    """One wiki page, read and parsed exactly once."""

    rel: str
    stem: str
    metadata: dict
    outgoing_links: list[str]


def _walk_pages(wiki_dir: Path) -> list[_PageRecord]:
    """Read + parse every page under wiki/ exactly once.

    This is the single expensive pass that `build_graph` and `lint`
    both build on, so neither re-walks the tree or re-parses files.
    """
    records: list[_PageRecord] = []
    for p in _iter_pages(wiki_dir):
        try:
            text = p.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        fm, body = _parse_page(text)
        records.append(
            _PageRecord(
                rel=_rel(wiki_dir, p),
                stem=p.stem,
                metadata=fm,
                outgoing_links=_extract_outgoing_links(body),
            )
        )
    return records


def _link_maps(
    records: list[_PageRecord],
) -> tuple[dict[str, str], dict[str, list[str]]]:
    """From parsed records, return (stem -> rel) and the backlink map
    (rel -> list of rels that link to it). Page identity is the stem,
    since `[[foo]]` resolves to `foo.md` regardless of subdirectory.
    """
    by_stem = {r.stem: r.rel for r in records}
    backlinks: dict[str, list[str]] = {r.rel: [] for r in records}
    for r in records:
        for target in r.outgoing_links:
            target_rel = by_stem.get(Path(target).stem)
            if target_rel is not None:
                backlinks[target_rel].append(r.rel)
    return by_stem, backlinks


def build_graph(wiki_dir: Path) -> dict[str, list[str]]:
    """Return `{page_path: [pages_that_link_to_it]}` (backlinks)."""
    _, backlinks = _link_maps(_walk_pages(wiki_dir))
    return backlinks


def lint(wiki_dir: Path, stale_days: int = 180) -> LintReport:
    """Heuristic-only drift checks. Never mutates.

    Reports three kinds of issues:
      - `orphan`: a page no other page links to (excluding `index.md`)
      - `broken_link`: outgoing wikilink whose target page does not exist
      - `stale`: page's `updated:` is older than `stale_days`
    """
    report = LintReport()
    records = _walk_pages(wiki_dir)
    by_stem, backlinks = _link_maps(records)
    now = datetime.now(timezone.utc)

    for r in records:
        if Path(r.rel).name != "index.md" and not backlinks.get(r.rel):
            report.issues.append(
                LintIssue(kind="orphan", path=r.rel, message=f"no inbound links to {r.rel}")
            )

        for target in r.outgoing_links:
            if Path(target).stem not in by_stem:
                report.issues.append(
                    LintIssue(
                        kind="broken_link",
                        path=r.rel,
                        message=f"link target not found: [[{target}]]",
                    )
                )

        updated = r.metadata.get("updated")
        if updated:
            try:
                dt = _parse_date(str(updated))
            except ValueError:
                continue
            if (now - dt).days > stale_days:
                report.issues.append(
                    LintIssue(
                        kind="stale",
                        path=r.rel,
                        message=f"not updated for {(now - dt).days} days",
                    )
                )
    return report


def _parse_date(value: str) -> datetime:
    """Parse common ISO date / datetime forms; assume UTC if naïve."""
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S"):
        try:
            dt = datetime.strptime(value, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            continue
    raise ValueError(f"unrecognised date format: {value!r}")


def page_summary_dicts(items: list[PageSummary]) -> list[dict]:
    """Serialisation helper for MCP tool returns."""
    return [asdict(item) for item in items]


def page_content_dict(item: PageContent) -> dict:
    return asdict(item)


def search_hit_dicts(items: list[SearchHit]) -> list[dict]:
    return [asdict(item) for item in items]


def lint_report_dict(report: LintReport) -> dict:
    return {"issues": [asdict(i) for i in report.issues], "clean": report.is_clean}
