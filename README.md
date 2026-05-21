# mcp-llm-wiki

A git-backed [MCP](https://modelcontextprotocol.io/) server that exposes
Karpathy's [LLM-Wiki pattern](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f)
as a tool surface for AI coding agents.

Wikis are plain Markdown stored in Git repositories. One git repo =
one wiki. Multiple agent VMs and humans can
read/write the same wiki concurrently; conflicts are mediated by ETag
optimistic concurrency plus custom merge-drivers for the two
write-contended hotspots (`log.md`, `index.md`).

The server runs in its own container, isolated from the agent. The agent
talks to it over HTTP-MCP and never touches the working trees directly.

## Design

- **Persistent, compounding artifact, not RAG.** Wiki pages are
  pre-synthesised Markdown, owned by the LLM agent layer.
- **Git as the source of truth.** Working trees in the container are
  ephemeral; the canonical bare repos live on the git host. Every write
  pushes; reads refresh the working tree with a TTL-debounced
  `git pull --rebase`.
- **Schema-blind.** The server validates nothing about page structure;
  conventions live in the agent's `SKILL.md`. The server's job is
  filesystem primitives + git mediation + safety.
- **Safety first.** All writes pass through a paranoid Markdown
  sanitiser (HTML comments, zero-width chars, bidi overrides, raw HTML,
  inline-style CSS, `data:` images). Path operations always go through
  `realpath()` + prefix-check; symlinks are rejected.

## Tools

| Tool | Mode | Purpose |
|---|---|---|
| `wiki_list` | read-only | List pages with frontmatter summary |
| `wiki_read` | read-only | Read a page (returns content + frontmatter + outgoing links + ETag) |
| `wiki_read_raw` | read-only | Read from the immutable `raw/` source layer |
| `wiki_search` | read-only | Fixed-string search over wiki pages (ripgrep-backed; FTS5 hybrid planned) |
| `wiki_save` | write (idempotent) | Upsert a page; atomic write + commit + push, ETag-guarded |
| `wiki_log_append` | write (append) | Append a timestamped entry to `log.md` |
| `wiki_lint` | read-only | Structural drift report (orphans, broken links, unindexed pages) — never mutates |
| `wiki_graph` | read-only | Backlinks map (parses wikilinks + Markdown links) |

These tools are primitives. The *workflows* that compose them — ingest,
query, and lint — live in the agent's skill, not the server.

## Setting up a wiki

A wiki is an ordinary git repository on any host that issues
per-repository access tokens — GitHub, GitLab, Gitea, Forgejo, or a
bare repo. One repo is one wiki; the convention is to name it
`wiki-<topic>` (e.g. `wiki-django`). Each repo starts with this
layout:

```
wiki-<topic>/
├── raw/            # operator-curated source documents
├── wiki/           # agent-owned Markdown pages
│   └── index.md    # category catalog
├── log.md          # append-only operations log
└── .gitattributes  # routes log.md / index.md through the merge-drivers
```

`log.md` lives at the repo root; pages and `index.md` live under
`wiki/`. The `.gitattributes` patterns carry no slash, so they match
`log.md` and `wiki/index.md` at any depth — that file routes the two
write-contended hotspots through the merge-drivers that coalesce
concurrent appends. The server installs the matching driver scripts
into every clone automatically, so `.gitattributes` is all the repo
needs. Seed a new wiki repo:

```bash
git clone https://git.example.com/<org>/wiki-<topic>.git
cd wiki-<topic>
mkdir -p raw wiki
touch raw/.gitkeep
printf '# Index\n\n' > wiki/index.md
printf '# Log\n\n' > log.md
printf 'log.md merge=llm-wiki-log\nindex.md merge=llm-wiki-index\n' > .gitattributes
git add -A && git commit -m "seed wiki structure" && git push
```

The server authenticates to the git host over HTTPS with an access
token — no SSH keys. Give each consumer (each agent VM or deployment)
its own machine account rather than a shared one: commits stay
attributable in `git log`, one account's token can be revoked without
disturbing the others, and per-repo collaborator permissions scope a
consumer to only the wikis it should reach. Grant **write** for
read-write wikis and **read** for read-only ones — the server enforces
the same split, and the git host enforces it again as defence in depth.

## Clipping web sources into `raw/`

`raw/` is the operator-curated source layer. The server only ever
*reads* it (`wiki_read_raw`); the synthesis agent ingests from `raw/`
but never writes it, so a web page must be turned into a local Markdown
file before the agent sees it.

`wiki-clip` is that step — a small operator CLI shipped with this repo,
the command-line equivalent of Karpathy's Obsidian Web Clipper:

```bash
pip install -e ".[clip]"          # pulls in markitdown
wiki-clip path/to/wiki-repo https://example.com/some/page
```

It fetches the URL, converts the HTML to Markdown with
[markitdown](https://github.com/microsoft/markitdown), and writes
`raw/<slug>.md` with provenance frontmatter (`source_url`, `title`, `fetched`,
`clipped_by`). It then `git add`s the file but does **not** commit — the
operator reviews the clipped Markdown and commits, keeping `raw/` a
curated layer. `--name` overrides the filename stem.

markitdown is an optional dependency (the `clip` extra), kept out of
the server container: clipping is an operator task, not a server
capability.

## Editing a wiki by hand

A wiki is a normal git repo — edit it like one. Clone it, edit the
Markdown in any editor, commit, and push. The server picks the changes
up on its next read: reads refresh the working tree with a
TTL-debounced `git pull --rebase`. Wikilinks (`[[page]]`) render
natively in Obsidian and in VS Code with Foam; most git-host web views
show them as plain text.

## Linting

Linting is the maintenance cycle that keeps a wiki from rotting: find
the drift, then fix it. The fix is the point — a lint that only reports
has done half the job.

Detection has two layers, because drift comes in two kinds.

**Structural drift** — pages nothing links to, links to pages renamed
away, pages missing from the catalog — is graph topology. `wiki_lint`
finds it deterministically:

| Kind | Meaning |
|---|---|
| `orphan` | A content page no other page links to. |
| `broken_link` | An outgoing link whose target page does not exist. |
| `unindexed` | A content page not linked from `index.md`, the catalog. |

(`index.md` and `log.md` are structural files, never flagged. The
report is `{"issues": [{kind, path, message}, …], "clean": <bool>}`.)

**Semantic drift** — contradictions, claims a newer source has
superseded, concepts mentioned but lacking their own page, missing
cross-references — cannot be computed; it takes an agent reading and
judging the content.

The agent then **remedies** each finding with `wiki_save` — relink an
orphan, stub or drop a broken link, add a page to `index.md`, rewrite a
superseded claim. `wiki_lint` is read-only by design: a fix is a
content change and belongs in `wiki_save` (sanitiser, ETag, commit),
with the agent deciding *how*. The full detect-and-remedy loop is the
agent's *Lint* operation, defined in the wiki skill.

## Status

Implemented and tested: all 8 tools, the sanitiser, path-safety, ETag
optimistic concurrency, git mediation, and the `log.md` / `index.md`
merge-drivers are in place and covered by the test suite (unit tests
plus a multi-VM end-to-end suite). Pre-1.0 — interfaces may still
shift. `wiki_search` is currently ripgrep-backed fixed-string matching;
an FTS5 hybrid is planned.

## Integration

This server is designed to be consumed by
[tank-agent-os](https://github.com/np6126/tank-agent-os) as a Quadlet-managed MCP service, but
works standalone for any MCP client. The image is published to a
container registry; consumers pin by digest.

## License

MIT — see [LICENSE](LICENSE).
