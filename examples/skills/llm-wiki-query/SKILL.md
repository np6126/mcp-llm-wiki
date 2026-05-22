---
name: llm-wiki-query
description: >-
  Use to answer a question from a wiki — search the pages, read the top
  matches, synthesise a cited answer, and optionally file a reusable
  answer back as a synthesis page. The Query operation of the llm-wiki
  skill set; read the llm-wiki skill first for the wiki model and
  conventions.
---

# llm-wiki — Query

Answer a question from the wiki. The wiki model and conventions live in
the `llm-wiki` skill.

## Steps

1. **Find candidates.** `wiki_search` for the question's key terms.
   Search is fixed-string — try several term variants. `wiki_list`
   gives the whole page set with frontmatter when a search is too
   narrow or you need an overview.

2. **Read.** `wiki_read` the top matches. Follow their outgoing links
   (returned by `wiki_read`) to reach pages the search missed.

3. **Synthesise.** Write the answer yourself from the pages you read.
   **Cite** the pages you used — name them, so the answer is traceable
   the same way a wiki page cites its `raw/` sources.

4. **File it back — if it is worth keeping.** A good answer that future
   questions will hit again becomes a new page: `wiki_save` it with
   `kind: synthesis` and a `derived-from: [<pages used>]` list, then
   add it to `index.md` and cross-link it, exactly as Ingest steps
   3–4. A one-off or trivial answer is not worth a page — do not file
   every answer.

A `synthesis` page is compiled from other wiki pages, not from `raw/`;
keep its `derived-from` chain shallow (see `llm-wiki` › Discipline
against error propagation).
