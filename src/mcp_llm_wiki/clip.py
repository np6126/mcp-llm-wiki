"""wiki-clip — fetch a web source, convert it to Markdown, and stage it
into a wiki's raw/ layer.

The Karpathy LLM-wiki model keeps raw/ operator-curated: the synthesis
agent reads raw/ (wiki_read_raw) but never writes it. Bringing in a web
source therefore needs a "clip" step done outside the MCP server —
convert the page to a local Markdown file under raw/ — before the agent
ingests it. wiki-clip is the command-line equivalent of Karpathy's
Obsidian Web Clipper.

It writes raw/<slug>.md with provenance frontmatter and `git add`s it.
It deliberately does NOT commit or push: the operator reviews the
clipped Markdown and commits, keeping raw/ a curated layer.
"""

from __future__ import annotations

import argparse
import io
import re
import subprocess
import sys
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import yaml

_USER_AGENT = "Mozilla/5.0 (compatible; wiki-clip; +mcp-llm-wiki)"
_FETCH_TIMEOUT = 30
_MAX_FETCH_BYTES = 25 * 1024 * 1024  # 25 MiB — generous for any HTML page
_MAX_SLUG_LEN = 60


def slugify(text: str) -> str:
    """Reduce arbitrary text to a lowercase_snake_case filename stem.

    Non-ASCII letters are folded to ASCII (ü -> u); the result is
    restricted to [a-z0-9_], so it is always a safe single path
    component — no traversal tokens, no separators. Over-long input is
    truncated on a word boundary.
    """
    folded = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    words = [w for w in re.split(r"[^a-z0-9]+", folded.lower()) if w]
    slug = ""
    for word in words:
        candidate = f"{slug}_{word}" if slug else word
        if len(candidate) > _MAX_SLUG_LEN:
            break
        slug = candidate
    if not slug and words:
        slug = words[0][:_MAX_SLUG_LEN]  # a single over-long word: hard cut
    return slug or "source"


def fetch(url: str) -> bytes:
    """Fetch `url` over http(s) and return the response bytes (size-capped)."""
    if urllib.parse.urlsplit(url).scheme not in ("http", "https"):
        raise ValueError(f"only http(s) URLs are supported: {url!r}")
    # The scheme is validated to http(s) above; the urlopen scheme audit
    # (ruff S310) is therefore satisfied.
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})  # noqa: S310
    with urllib.request.urlopen(request, timeout=_FETCH_TIMEOUT) as response:  # noqa: S310
        data = response.read(_MAX_FETCH_BYTES + 1)
    if len(data) > _MAX_FETCH_BYTES:
        raise ValueError(f"response larger than {_MAX_FETCH_BYTES} bytes: {url!r}")
    return data


def to_markdown(html: bytes, url: str) -> tuple[str, str]:
    """Convert fetched HTML to (markdown_body, title) via markitdown."""
    # markitdown is a heavy import — load it at point of use so `wiki-clip
    # --help` and argument errors stay fast.
    from markitdown import MarkItDown

    result = MarkItDown().convert_stream(io.BytesIO(html), file_extension=".html", url=url)
    body = (result.markdown or result.text_content or "").strip()
    return body, (result.title or "").strip()


def build_raw_document(body: str, *, source_url: str, title: str, fetched: str) -> str:
    """Assemble a raw/ file: provenance frontmatter followed by the body."""
    front = {
        "source_url": source_url,
        "title": title or source_url,
        "fetched": fetched,
        "clipped_by": "wiki-clip",
    }
    frontmatter = yaml.safe_dump(front, sort_keys=False, allow_unicode=True).strip()
    return f"---\n{frontmatter}\n---\n\n{body}\n"


def clip(wiki_dir: Path, url: str, *, name: str | None = None) -> Path:
    """Fetch `url`, convert it, and stage it as raw/<slug>.md in `wiki_dir`.

    Returns the written path. Does not commit — that stays the operator's
    deliberate, reviewed step.
    """
    body, title = to_markdown(fetch(url), url)
    slug = slugify(name or title or url)
    fetched = datetime.now(timezone.utc).isoformat(timespec="seconds")
    document = build_raw_document(body, source_url=url, title=title, fetched=fetched)

    target = wiki_dir / "raw" / f"{slug}.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(document, encoding="utf-8")
    rel = target.relative_to(wiki_dir).as_posix()
    subprocess.run(
        ["git", "add", "--", rel], cwd=wiki_dir, check=True, capture_output=True, text=True
    )
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="wiki-clip",
        description="Clip a web source into a wiki's raw/ layer (Karpathy LLM-wiki).",
    )
    parser.add_argument(
        "wiki_dir",
        type=Path,
        nargs="?",
        default=Path.cwd(),
        help="path to a local wiki working tree (default: current directory)",
    )
    parser.add_argument("url", help="http(s) source URL to clip")
    parser.add_argument("--name", help="override the raw/<name>.md filename stem")
    args = parser.parse_args(argv)

    if not (args.wiki_dir / ".git").is_dir():
        print(f"wiki-clip: not a git working tree: {args.wiki_dir}", file=sys.stderr)
        return 2

    try:
        target = clip(args.wiki_dir, args.url, name=args.name)
    except ModuleNotFoundError as exc:
        print(
            f"wiki-clip: missing dependency '{exc.name}' — the mcp-llm-wiki "
            "install is incomplete; reinstall it.",
            file=sys.stderr,
        )
        return 1
    except (ValueError, urllib.error.URLError) as exc:
        print(f"wiki-clip: {exc}", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or "").strip() or str(exc)
        print(f"wiki-clip: git add failed: {detail}", file=sys.stderr)
        return 1

    rel = target.relative_to(args.wiki_dir).as_posix()
    print(f"clipped -> {rel}  (staged, not committed)")
    print("review it, then commit + push to publish the source.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
