---
name: llm-wiki
description: >-
  Foundation for working with a git-backed knowledge wiki served by the
  llm-wiki MCP server: the wiki model, the eight tools, and the
  page-kind, naming, frontmatter, linking, and ETag-concurrency
  conventions. Start here for any wiki work — the ingest, query, and
  lint operations each have their own skill (llm-wiki-ingest,
  llm-wiki-query, llm-wiki-lint).
---

# llm-wiki

The `llm-wiki` MCP server exposes one or more git-backed Markdown wikis
— Karpathy's LLM-Wiki pattern: a persistent, compounding knowledge
artifact that an agent builds and maintains across sessions. This
skill is the shared foundation; the three operations build on it.

## The wiki model

Each wiki is one git repo. Its layout:

- `raw/` — operator-curated source documents (papers, transcripts,
  clippings). Immutable from the agent's side: read it with
  `wiki_read_raw`, never write it.
- `wiki/` — the Markdown pages you own and maintain, plus `index.md`,
  the category catalog.
- `log.md` — an append-only operations log at the repo root.

Every write commits and pushes; reads pull the latest first. Even so,
others edit the wiki concurrently, so what you read is a snapshot —
guard modifying writes with the
[ETag discipline](#concurrency--the-etag-discipline).

## The eight tools

| Tool | Mode | Use |
|---|---|---|
| `wiki_list` | read | All pages with a frontmatter summary |
| `wiki_read` | read | One page: body, frontmatter, outgoing links, ETag |
| `wiki_read_raw` | read | One `raw/` source: bytes + ETag |
| `wiki_search` | read | Fixed-string search over `wiki/` pages |
| `wiki_graph` | read | Backlinks map (who links to each page) |
| `wiki_save` | write | Upsert a page (sanitise, commit, push) |
| `wiki_log_append` | write | Append a timestamped `log.md` entry |
| `wiki_lint` | read | Deterministic drift report |

`wiki_search` is **fixed-string** — literal substring, ripgrep-backed,
no fuzzy or semantic matching. A no-match means the literal string is
absent, not that the topic is absent; try other terms before
concluding a page does not exist.

## File naming

`lowercase_snake_case.md` — no spaces, no capitals, no dots except the
`.md` extension. Examples: `error_propagation.md`,
`optimistic_concurrency.md`. Page paths passed to the tools are
relative to `wiki/`. Deterministic naming keeps wikilink resolution
unambiguous.

## Frontmatter

Every page opens with a YAML block:

```yaml
---
title: Optimistic Concurrency
kind: concept
sources:
  - path: etag_paper.pdf
    etag: 9f86d0818854c7d6...
created: 2026-05-20
updated: 2026-05-22
---
```

`sources:` is the provenance block, and its shape is load-bearing —
`wiki_lint` only checks entries that are **mappings with a `path`
key**. Each entry is a `{path, etag}` pair:

- `path` — the source location **relative to `raw/`**, exactly the
  string you passed to `wiki_read_raw` (e.g. `etag_paper.pdf` — not
  `raw/etag_paper.pdf`, and not a URL).
- `etag` — the `etag` value `wiki_read_raw` returned for that source.

A flat list of strings, or a URL, is silently ignored by the linter:
the page then looks provenance-tracked but is not. Pages with no
`sources:` block are simply not provenance-tracked — fine for a page
not derived from `raw/`.

## The four page kinds

Ask *what am I answering?* and set `kind` to match:

- **`entity`** — "What is X?" A concrete referent: a person, library,
  tool, standard. Self-contained. e.g. `gitea.md`.
- **`concept`** — "What does Y mean?" An abstract pattern, principle,
  or pitfall — definition plus examples. e.g. `prompt_injection.md`.
- **`summary`** — "How do A, B, C relate?" A map over a topic area;
  mostly links, not itself a knowledge store.
- **`synthesis`** — "Why did we decide Z?" Compiled from other wiki
  pages (the Query "file back" output). **Must** carry
  `derived-from: [page_a, page_b]` so its provenance stays traceable.

## Linking

Default to Obsidian-style wikilinks: `[[page_name]]`,
`[[page_name|alias]]`, `[[page_name#heading]]`. Standard Markdown links
also work — both forms are parsed by `wiki_graph` and `wiki_lint`.
Wikilinks render natively in Obsidian and in VS Code with Foam; most
git-host web views show them as plain text — a known limitation, not a
bug.

## Concurrency — the ETag discipline

`wiki_read` and `wiki_read_raw` return an `etag`. `wiki_save` takes an
optional `etag`:

- **With `etag`** — the write succeeds only if the page still matches
  what you read. If someone wrote in between, the call fails with
  `etag_mismatch`: re-read the page, reapply your change to the new
  content, and retry.
- **Without `etag`** — the write is unconditional and overwrites
  whatever is there, silently clobbering a concurrent edit.

Rule: whenever you *modify* an existing page, go read → `wiki_save`
with that page's `etag`. Omit `etag` only when creating a genuinely new
page.

## Discipline against error propagation

A wiki has no self-correction — a wrong synthesis propagates into every
page that cites it. Two rules:

- Treat pages you wrote with the same scrutiny as external sources;
  verify against `raw/` before re-synthesising from them.
- If a `synthesis` page's `derived-from` chain runs deeper than two
  levels, go back to `raw/` rather than synthesising from synthesis.

## The three operations

Each is its own skill — invoke the matching one:

- **Ingest** (`llm-wiki-ingest`) — a new `raw/` source becomes wiki
  pages.
- **Query** (`llm-wiki-query`) — a question becomes a synthesised,
  cited answer.
- **Lint** (`llm-wiki-lint`) — a periodic health-check finds and fixes
  drift.
