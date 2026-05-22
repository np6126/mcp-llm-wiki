---
name: llm-wiki-ingest
description: >-
  Use to turn a new or changed source in a wiki's raw/ layer into wiki
  pages — read the source, create and update entity and concept pages,
  update index.md, cross-link, and log the ingest. The Ingest
  operation of the llm-wiki skill set; read the llm-wiki skill first
  for the wiki model and conventions.
---

# llm-wiki — Ingest

Turn a `raw/` source into wiki pages. The wiki model and the
conventions — naming, frontmatter, page kinds, the ETag discipline —
live in the `llm-wiki` skill; follow them here.

Karpathy's rule of thumb: a single source touches **10–15 wiki pages**.
Ingest is rarely "write one page".

## Steps

0. **Read and align.** `wiki_read_raw` the new source; keep the
   returned `etag` — every page you derive from it records that `etag`
   in `sources:` (see `llm-wiki` › Frontmatter). Discuss the key
   takeaways with the operator and confirm scope before writing any
   page.

1. **Find what already exists.** For each entity or concept the source
   covers, `wiki_search` for an existing page. Search is fixed-string,
   so try a few term variants before deciding a page is new.
   `wiki_list` gives the whole page set when you need the lay of the
   land.

2. **Create and update pages.** `wiki_save` the new pages and the ones
   the source changes. For an *update*, `wiki_read` the page first and
   pass its `etag` to `wiki_save`; for a brand-new page, omit `etag`.
   Set `kind` per the four page kinds. Put the source in each page's
   `sources:` block as a `{path, etag}` pair.

3. **Update `index.md`.** `wiki_read` `index.md`, add the new pages to
   the right category with a one-line summary each, then `wiki_save` it
   back with its `etag`. This is the most-forgotten step — skip it and
   the pages are unreachable from the catalog (`wiki_lint` flags them
   `unindexed`).

4. **Cross-link.** Add `[[wikilinks]]` between the new pages and the
   existing pages they relate to — in *both* directions. An unlinked
   page is an orphan.

5. **Log it.** `wiki_log_append` one entry: `ingest | <source title>`.

Run the **Lint** operation (`llm-wiki-lint`) at the end of any
substantive ingest session — it catches the pages you forgot to index
or link.
