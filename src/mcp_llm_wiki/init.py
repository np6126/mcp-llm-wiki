"""wiki-init — create a wiki's initial structure, or reset one to it.

A wiki repository needs four things: a `raw/` source layer, a `wiki/`
page layer with an `index.md` catalog, a root `log.md`, and a
`.gitattributes` that routes `log.md` and `index.md` through the
merge-drivers. This seeds exactly that layout.

Run against a fresh clone it creates the structure. Run against a
populated wiki it CLEARS it — every page under `wiki/`, every source
under `raw/`, and the log are removed and the skeleton recreated. That
is destructive, so a populated wiki requires confirmation (type `yes`,
or pass `--yes`).

It never touches `.git/`: git history is preserved. The reset is left
as a staged change for the operator to review (`git status`) and
commit — the same review discipline as wiki-clip.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

# Seed content for the four structural files. The `.gitattributes`
# driver names must match those configured per-clone by the container
# entrypoint (`merge.llm-wiki-log`, `merge.llm-wiki-index`).
_INDEX_SEED = "# Index\n\n"
_LOG_SEED = "# Log\n\n"
_GITATTRIBUTES_SEED = "log.md merge=llm-wiki-log\nindex.md merge=llm-wiki-index\n"

# Staged together so `git add` covers exactly what wiki-init manages and
# nothing else in the working tree.
_MANAGED_PATHS = ["raw", "wiki", "log.md", ".gitattributes"]


@dataclass
class Survey:
    """What content a wiki working tree already holds."""

    pages: list[str]
    """Content pages under wiki/ (index.md excluded — it is structural)."""
    raw_files: list[str]
    """Sources under raw/ (.gitkeep excluded)."""
    log_has_entries: bool
    index_customised: bool

    @property
    def is_populated(self) -> bool:
        """True when re-initialising would destroy operator content."""
        return bool(
            self.pages
            or self.raw_files
            or self.log_has_entries
            or self.index_customised
        )


def _read_stripped(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace").strip() if path.is_file() else ""


def survey(wiki_dir: Path) -> Survey:
    """Inspect `wiki_dir` for existing wiki content."""
    wiki = wiki_dir / "wiki"
    raw = wiki_dir / "raw"

    pages = (
        sorted(
            p.relative_to(wiki_dir).as_posix()
            for p in wiki.rglob("*.md")
            if p.is_file() and p.name != "index.md"
        )
        if wiki.is_dir()
        else []
    )
    raw_files = (
        sorted(
            p.relative_to(wiki_dir).as_posix()
            for p in raw.rglob("*")
            if p.is_file() and p.name != ".gitkeep"
        )
        if raw.is_dir()
        else []
    )
    log_has_entries = _read_stripped(wiki_dir / "log.md") not in ("", "# Log")
    index_customised = _read_stripped(wiki / "index.md") not in ("", "# Index")
    return Survey(pages, raw_files, log_has_entries, index_customised)


def seed(wiki_dir: Path) -> None:
    """Reset `wiki_dir` to the initial wiki structure.

    Removes everything under `wiki/` and `raw/`, then recreates the
    empty skeleton. `.git/` is never touched — history is preserved.
    """
    wiki = wiki_dir / "wiki"
    raw = wiki_dir / "raw"
    if wiki.exists():
        shutil.rmtree(wiki)
    if raw.exists():
        shutil.rmtree(raw)
    wiki.mkdir(parents=True)
    raw.mkdir(parents=True)
    (raw / ".gitkeep").write_text("", encoding="utf-8")
    (wiki / "index.md").write_text(_INDEX_SEED, encoding="utf-8")
    (wiki_dir / "log.md").write_text(_LOG_SEED, encoding="utf-8")
    (wiki_dir / ".gitattributes").write_text(_GITATTRIBUTES_SEED, encoding="utf-8")


def _confirm_clear() -> bool:
    """Prompt the operator to confirm a destructive re-init. Returns True
    only on an explicit `yes` typed at an interactive prompt."""
    if not sys.stdin.isatty():
        print(
            "wiki-init: refusing to clear without confirmation; "
            "pass --yes for non-interactive use.",
            file=sys.stderr,
        )
        return False
    if input("Type 'yes' to clear and re-initialise: ").strip() != "yes":
        print("wiki-init: aborted; nothing changed.", file=sys.stderr)
        return False
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="wiki-init",
        description="Create a wiki's initial structure, or reset an existing wiki to it.",
    )
    parser.add_argument(
        "wiki_dir",
        type=Path,
        nargs="?",
        default=Path.cwd(),
        help="path to a local wiki working tree (default: current directory)",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="skip the confirmation prompt when clearing an existing wiki",
    )
    args = parser.parse_args(argv)
    wiki_dir = args.wiki_dir

    if not (wiki_dir / ".git").is_dir():
        print(f"wiki-init: not a git working tree: {wiki_dir}", file=sys.stderr)
        return 2

    state = survey(wiki_dir)
    if state.is_populated:
        print(f"wiki-init: {wiki_dir} already contains a wiki:", file=sys.stderr)
        if state.pages:
            print(f"  - {len(state.pages)} page(s) under wiki/", file=sys.stderr)
        if state.raw_files:
            print(f"  - {len(state.raw_files)} source(s) under raw/", file=sys.stderr)
        if state.log_has_entries:
            print("  - log.md has entries", file=sys.stderr)
        if state.index_customised:
            print("  - index.md has content", file=sys.stderr)
        print(
            "Re-initialising CLEARS all of it. Git history is preserved; "
            "the reset is staged for you to review and commit.",
            file=sys.stderr,
        )
        if not args.yes and not _confirm_clear():
            return 1

    seed(wiki_dir)

    try:
        subprocess.run(
            ["git", "add", "-A", "--", *_MANAGED_PATHS],
            cwd=wiki_dir,
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or "").strip() or str(exc)
        print(f"wiki-init: git add failed: {detail}", file=sys.stderr)
        return 1

    verb = "re-initialised" if state.is_populated else "initialised"
    print(f"wiki {verb} at {wiki_dir}  (staged, not committed)")
    print("review with `git status`, then commit + push.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
