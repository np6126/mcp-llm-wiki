#!/usr/bin/env bash
# index_md_merge.sh — git custom merge driver for index.md.
#
# Invoked by git with: %A (current/ours) %O (ancestor) %B (other/theirs).
# Convention: index.md is a Category-TOC where each entry is a bullet
# line. Two agents typically add new entries to different categories
# (or different lines within the same category).
#
# Strategy: union the unique lines, preserving the order of current
# first, then appending lines from other that don't already appear.
# This always converges. The lint pass can flag the rare malformed
# result (duplicate section headings etc.); a clean merge is the
# common case.
#
# Exit 0 = clean merge.
set -euo pipefail

current="$1"
# ancestor="$2"   # unused; union of both sides is well-defined.
other="$3"

awk '
  NR==FNR { seen[$0]=1; print; next }
  !($0 in seen) { print; seen[$0]=1 }
' "$current" "$other" > "$current.merged"

mv "$current.merged" "$current"
