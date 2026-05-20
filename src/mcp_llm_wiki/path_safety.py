"""Filesystem path safety + content ETag helpers.

These two responsibilities live in the same module because the MCP
tool surface is so small: every read or write path through the server
calls `resolve_within` first, then either reads (and computes `etag`)
or writes.

Path safety contract — `resolve_within(root, relative)`:

  - Reject absolute paths.
  - Reject any path containing `..` or empty segments.
  - Walk each path component; reject if any intermediate or final
    component is a symlink. This is the CVE-2025-53109 fix model:
    don't just rely on .resolve() (which follows the link) to detect
    escape — check is_symlink at each level.
  - After the symlink-free walk, sanity-check that the resolved path
    sits under the resolved root (catches edge cases like a
    bind-mounted root or a TOCTOU race where root itself was
    swapped underneath us).

ETag contract — `etag(content)`:

  - sha256 over the exact bytes that will be on disk (or are on disk
    when read). Sanitisation runs *before* etag computation in the
    write path, so the value an agent sees from `wiki_read` matches
    what it would get from `wiki_save(..., etag=etag(content))`.
"""

from __future__ import annotations

import hashlib
from pathlib import Path, PurePosixPath


class PathSafetyError(Exception):
    """Raised when a requested path would escape its wiki root,
    contain a traversal token, or pass through a symlink.

    Callers should surface this as a tool-level error to the agent —
    never silently fall through. The message is suitable for direct
    inclusion in an MCP tool error.
    """


def resolve_within(root: Path, relative: str) -> Path:
    """Return the safe absolute Path for `relative` inside `root`.

    Raises `PathSafetyError` if the path is absolute, contains
    traversal segments, passes through any symlink, or would land
    outside `root` after resolution.
    """
    if not relative.strip():
        raise PathSafetyError("empty path")
    if PurePosixPath(relative).is_absolute():
        raise PathSafetyError(f"absolute paths not allowed: {relative!r}")

    # Validate the raw input string before letting PurePosixPath normalise.
    # `PurePosixPath("./x").parts` strips the `.` segment, so checks done
    # after normalisation would silently accept "./../etc/x".
    raw_parts = relative.split("/")
    if any(part == ".." for part in raw_parts):
        raise PathSafetyError(f"path traversal not allowed: {relative!r}")
    if any(part == "." for part in raw_parts):
        raise PathSafetyError(f"current-dir segments not allowed: {relative!r}")
    if "" in raw_parts:
        raise PathSafetyError(f"empty segment in path: {relative!r}")

    parts = PurePosixPath(relative).parts

    root_resolved = root.resolve()
    cursor = root_resolved
    for part in parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise PathSafetyError(f"symlink in path: {cursor}")

    # Belt-and-braces: even if no symlink was found, a relative path
    # with a tricky Unicode-normalised parent could theoretically
    # escape. .resolve() with strict=False catches this for missing
    # leaf files; we then re-verify containment.
    target = cursor.resolve()
    if target != root_resolved and root_resolved not in target.parents:
        raise PathSafetyError(f"path escapes root: {target}")

    return cursor


def etag(content: bytes | str) -> str:
    """Return the canonical ETag for `content`: hex sha256 of the exact
    bytes that will be on disk.

    Strings are encoded as UTF-8. No normalisation; the caller has
    already run the sanitiser.
    """
    if isinstance(content, str):
        content = content.encode("utf-8")
    return hashlib.sha256(content).hexdigest()
