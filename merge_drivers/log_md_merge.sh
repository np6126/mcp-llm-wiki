#!/usr/bin/env bash
# log_md_merge.sh — git custom merge driver for log.md.
#
# Invoked by git with: %A (current/ours) %O (ancestor) %B (other/theirs).
# Convention: log.md is append-only. Each entry is one line — a
# `## [timestamp]` Markdown heading (older logs may still carry bare
# `[timestamp]` lines; both are recognised). ISO-8601 timestamps sort
# lexicographically, which is what we rely on here.
#
# Strategy:
#   1. Pull any non-entry header lines (those before the first entry)
#      from the start of each side. Use whichever side has the longer
#      header; tie goes to current.
#   2. Concatenate the entry lines from both sides, sort them, dedupe
#      identical lines.
#   3. Emit header + sorted entries, overwriting current in place.
#
# Exit 0 = clean merge. We never report conflict; the union of two
# append-only streams is always defined.
set -euo pipefail

current="$1"
# ancestor="$2"   # unused: union of both sides is well-defined for
                  # append-only logs without consulting the base.
other="$3"

# An entry line is a bare `[timestamp]` (legacy) or a `## [timestamp]`
# Markdown heading (current). The optional `## ` prefix covers both.
extract_header() {
  awk '/^(## )?\[/{exit} {print}' "$1"
}
extract_entries() {
  awk '/^(## )?\[/{found=1} found{print}' "$1"
}

header_a=$(extract_header "$current")
header_b=$(extract_header "$other")
if [ "${#header_b}" -gt "${#header_a}" ]; then
  header="$header_b"
else
  header="$header_a"
fi

entries=$(printf '%s\n%s\n' \
  "$(extract_entries "$current")" \
  "$(extract_entries "$other")" \
  | grep -v '^$' \
  | sort -u)

{
  if [ -n "$header" ]; then
    printf '%s\n' "$header"
  fi
  printf '%s\n' "$entries"
} > "$current.merged"

mv "$current.merged" "$current"
