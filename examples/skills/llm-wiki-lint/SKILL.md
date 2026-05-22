---
name: llm-wiki-lint
description: >-
  Use to health-check a wiki — run wiki_lint, read backlinks with
  wiki_graph, judge the semantic drift the linter cannot, remedy each
  finding with wiki_save, and surface coverage gaps for the operator.
  The Lint operation of the llm-wiki skill set; read the llm-wiki skill
  first for the wiki model and conventions.
---

# llm-wiki — Lint

Keep the wiki healthy as it grows: find the drift and fix it, then
surface what is missing. A lint that only reports has done half the
job — the fix is the point (§3) — and a healthy wiki also means
knowing what it still lacks (§4). Run it at the end of any substantive
ingest session, and periodically otherwise. The wiki model and
conventions live in the `llm-wiki` skill.

## 1. Mechanical drift — `wiki_lint`

`wiki_lint` is deterministic and never mutates. It returns
`{"issues": [{kind, path, message}, …], "clean": <bool>}` with five
issue kinds:

| Kind | Meaning |
|---|---|
| `orphan` | A content page no other page links to. |
| `broken_link` | An outgoing link whose target page does not exist. |
| `unindexed` | A content page not linked from `index.md`. |
| `source_missing` | A `raw/` source listed in a page's `sources:` no longer exists. |
| `source_drift` | Such a source's bytes changed since the page was synthesised. |

`index.md` and `log.md` are structural, not content — they are exempt
from `orphan` and `unindexed`. A `broken_link` *is* still reported
against `index.md`: the catalog is exactly where links to renamed-away
pages collect.

`wiki_graph` complements the report — it returns the backlinks map (for
each page, the pages that link to it). Use it to see *why* a page is an
orphan and to pick the right page to link it from.

The `source_*` checks apply only to pages with a `sources:` block of
`{path, etag}` mappings (see `llm-wiki` › Frontmatter). `source_missing`
is graded — its message says how many sources survive, so a partial
loss (re-derive from the rest) reads differently from a total loss.

## 2. Semantic drift — your judgement

The linter cannot compute these; reading the content can:

- contradictions between pages,
- claims a newer source has superseded,
- concepts mentioned but lacking their own page,
- missing cross-references between related pages.

## 3. Remedy

`wiki_lint` is read-only by design — a fix is a content change, so
every fix goes through `wiki_save`. `wiki_read` the page first and pass
its `etag` (see `llm-wiki` › Concurrency).

| Finding | Remedy |
|---|---|
| `orphan` | Link it from a related page or `index.md`; or delete it if dead. |
| `broken_link` | Repoint the link, stub the missing page, or drop the link. |
| `unindexed` | Add the page to `index.md` under the right category. |
| `source_missing` | Re-derive the page from the surviving sources or a replacement; drop the dead `sources:` entry. |
| `source_drift` | Re-read the source with `wiki_read_raw`, reconcile the page, update the entry's `etag`. |
| semantic | Rewrite the superseded claim; add the missing page or cross-reference. |

## 4. Gaps and next directions

Drift is what the wiki got *wrong*; gaps are what it is *missing*. The
same pass surfaces them — report these to the operator rather than
acting on them yourself, since sourcing stays the operator's job:

- thin spots — a topic the wiki only touches in passing that a web
  search or a new source could fill out;
- new questions the current pages raise but do not answer;
- sources worth seeking out to deepen or challenge the synthesis.

Log a substantive lint pass: `wiki_log_append` → `lint | <what changed>`.
