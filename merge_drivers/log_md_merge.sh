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
#   2. Merge the entry lines as a 3-way multiset union: each distinct
#      line is kept  current_count + other_count − ancestor_count
#      times. An entry inherited through shared history is counted on
#      both sides *and* in the ancestor, so it nets to a single copy;
#      two sides that each appended a byte-identical entry independently
#      are absent from the ancestor, so both copies survive. A blanket
#      `sort -u` cannot tell those apart and would silently drop one
#      side's append.
#   3. Sort entries by timestamp on a key that drops the optional `## `
#      heading prefix — otherwise `#` (0x23) sorts ahead of `[` (0x5B)
#      and every new-format entry jumps above every older legacy bare
#      entry regardless of time.
#   4. Emit header + sorted entries, overwriting current in place.
#
# Exit 0 = clean merge. We never report conflict; the union of two
# append-only streams is always defined.
set -euo pipefail

current="$1"
ancestor="$2"
other="$3"

# An entry line is a bare `[timestamp]` (legacy) or a `## [timestamp]`
# Markdown heading (current). The optional `## ` prefix covers both.
# Everything before the first entry line is header.
extract_header() {
  awk '/^(## )?\[/{exit} {print}' "$1"
}

# Emit one file's entry lines, each prefixed with `role<TAB>` so the
# merge step can tell the three inputs apart. Tagging by argument
# (rather than a positional line counter) stays correct when an input
# file is empty — e.g. log.md newly added with no common ancestor.
extract_entries() {
  awk -v role="$2" '
    /^(## )?\[/             { in_entries = 1 }
    in_entries && $0 != ""  { print role "\t" $0 }
  ' "$1"
}

header_a=$(extract_header "$current")
header_b=$(extract_header "$other")
if [ "${#header_b}" -gt "${#header_a}" ]; then
  header="$header_b"
else
  header="$header_a"
fi

# 3-way multiset union of the entry lines (strategy step 2), then sort
# on a timestamp key with the optional `## ` prefix stripped (step 3).
entries=$(
  {
    extract_entries "$current"  cur
    extract_entries "$ancestor" anc
    extract_entries "$other"    oth
  } | awk -F'\t' '
    {
      role = $1
      line = substr($0, index($0, "\t") + 1)
      count[role, line]++
      seen[line] = 1
    }
    END {
      for (line in seen) {
        n = count["cur", line] + count["oth", line] - count["anc", line]
        key = line
        sub(/^## /, "", key)
        while (n-- > 0) print key "\t" line
      }
    }
  ' | LC_ALL=C sort -t$'\t' -k1,1 | cut -f2-
)

{
  if [ -n "$header" ]; then
    printf '%s\n' "$header"
  fi
  printf '%s\n' "$entries"
} > "$current.merged"

mv "$current.merged" "$current"
