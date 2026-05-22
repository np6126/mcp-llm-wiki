<div align="center">

<img src="assets/logo.svg" alt="mcp-llm-wiki logo" width="140" height="140">

# mcp-llm-wiki

A durable, shared knowledge layer for AI coding agents — a git-backed
[MCP](https://modelcontextprotocol.io/) server that exposes Karpathy's
[LLM-Wiki pattern](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f)
as a tool surface.

<p>
  <img src="https://img.shields.io/badge/license-MIT-2f7e8c" alt="License: MIT">
  <img src="https://img.shields.io/badge/protocol-MCP-2f7e8c" alt="Protocol: MCP">
  <img src="https://img.shields.io/badge/python-3.10%2B-2f7e8c" alt="Python 3.10+">
  <img src="https://img.shields.io/badge/status-pre--1.0-9a6b00" alt="Status: pre-1.0">
</p>

</div>

---

**Contents** &nbsp;·&nbsp;
[Design](#design) &nbsp;·&nbsp;
[Tools](#tools) &nbsp;·&nbsp;
[Companion skills](#companion-skills) &nbsp;·&nbsp;
[Operator CLIs](#operator-clis) &nbsp;·&nbsp;
[Setting up a wiki](#setting-up-a-wiki) &nbsp;·&nbsp;
[Clipping web sources](#clipping-web-sources-into-raw) &nbsp;·&nbsp;
[Editing by hand](#editing-a-wiki-by-hand) &nbsp;·&nbsp;
[Linting](#linting) &nbsp;·&nbsp;
[Status](#status) &nbsp;·&nbsp;
[Integration](#integration) &nbsp;·&nbsp;
[License](#license)

---

Your agents read and write a persistent Markdown wiki that compounds
over time. Each wiki is plain
Markdown stored in a Git repository — one git repo, one wiki — so the
knowledge is versioned, reviewable, and editable by hand. Multiple agent
VMs and humans can read/write the same wiki concurrently; conflicts are
mediated by ETag optimistic concurrency plus custom merge-drivers for the
two write-contended hotspots (`log.md`, `index.md`).

The server runs in its own container, isolated from the agent. The agent
talks to it over HTTP-MCP and never touches the working trees directly.

## Design

- **Persistent, compounding artifact.** Wiki pages are pre-synthesised
  Markdown, owned by the LLM agent layer — knowledge that accumulates
  across sessions, where RAG retrieves chunks fresh at query time.
- **Git as the source of truth.** Working trees in the container are
  ephemeral; the canonical bare repos live on the git host. Every write
  pushes; reads refresh the working tree with a TTL-debounced
  `git pull --rebase`.
- **Schema-blind.** The server validates nothing about page structure;
  conventions live in the agent's skills (see [Companion
  skills](#companion-skills)). The server's job is filesystem
  primitives + git mediation + safety.
- **Safety first.** All writes pass through a paranoid Markdown
  sanitiser (HTML comments, zero-width chars, bidi overrides, raw HTML,
  inline-style CSS, `data:` images). Path operations always go through
  `realpath()` + prefix-check; symlinks are rejected.

## Tools

| Tool | Mode | Purpose |
|---|---|---|
| `wiki_list` | read-only | List pages with frontmatter summary |
| `wiki_read` | read-only | Read a page (returns content + frontmatter + outgoing links + ETag) |
| `wiki_read_raw` | read-only | Read from the immutable `raw/` source layer (returns content + ETag) |
| `wiki_search` | read-only | Fixed-string search over wiki pages (ripgrep-backed) |
| `wiki_save` | write (idempotent) | Upsert a page; atomic write + commit + push, ETag-guarded |
| `wiki_log_append` | write (append) | Append a timestamped entry to `log.md` |
| `wiki_lint` | read-only | Drift report (orphans, broken links, unindexed pages, removed/changed sources) — never mutates |
| `wiki_graph` | read-only | Backlinks map (parses wikilinks + Markdown links) |

These tools are primitives. The *operations* that compose them —
ingest, query, and lint — are agent workflows; see [Companion
skills](#companion-skills).

## Companion skills

The Ingest, Query, and Lint operations ship in this repo as [Agent
Skills](https://code.claude.com/docs/en/skills) under
[`examples/skills/`](examples/skills/):

| Skill | Role |
|---|---|
| `llm-wiki` | Foundation — the wiki model, the eight tools, and the page-kind, naming, frontmatter, linking, and ETag-concurrency conventions. |
| `llm-wiki-ingest` | Ingest — turn a new `raw/` source into wiki pages. |
| `llm-wiki-query` | Query — answer a question from the wiki; optionally file the answer back. |
| `llm-wiki-lint` | Lint — health-check the wiki for drift and remedy it. |

The server does not load these — it is schema-blind and exposes only
tools. The skills belong to the *consuming* agent: copy the four
directories into the agent's skills directory (for Claude Code,
`~/.claude/skills/` or a project's `.claude/skills/`). The agent then
loads `llm-wiki` for the conventions and the matching operation skill
when it ingests, queries, or lints. Keep the copies in sync with this
repo — the server's tool surface is what they document.

## Operator CLIs

Two operator-side commands ship with this repo, separate from the MCP
server and never run in its container: `wiki-init` seeds a wiki's
structure (and resets an existing one), and `wiki-clip` clips web
sources into `raw/`.

Install with [pipx](https://pipx.pypa.io/) — it puts the commands on
your `PATH` in an isolated environment, sidestepping PEP 668 (the
`externally-managed-environment` error a plain `pip install` hits on
recent Debian/Ubuntu):

```bash
pipx install "."
```

That single command installs both `wiki-clip` and `wiki-init`, fully
working — `wiki-clip`'s dependencies
([markitdown](https://github.com/microsoft/markitdown) and PyYAML) are
ordinary requirements, not an optional extra. Run `pipx ensurepath`
once if `~/.local/bin` isn't on your `PATH`.

Both commands are repo-agnostic — they take the target wiki repo as an
argument (default: the current directory), so one install serves every
wiki. Without `-e` the package is copied into pipx, so the
`mcp-llm-wiki` checkout is disposable afterward; install editable
(`pipx install -e "."`) only to hack on the tools. Uninstall with
`pipx uninstall mcp-llm-wiki` — pipx keys on the package name, not the
command names.

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
needs. `wiki-init` (see [Operator CLIs](#operator-clis)) seeds that
whole layout — clone the repo, run it, commit, push:

```bash
git clone https://git.example.com/<org>/wiki-<topic>.git
cd wiki-<topic>
wiki-init                       # creates raw/.gitkeep, wiki/index.md, log.md, .gitattributes
git commit -m "seed wiki structure" && git push
```

Run on a wiki that already has content, `wiki-init` resets it to the
empty structure — listing what will be cleared and asking for
confirmation first, and never rewriting git history.

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

`wiki-clip` is that step — the command-line equivalent of Karpathy's
Obsidian Web Clipper (install it via [Operator CLIs](#operator-clis)):

```bash
cd wiki-<topic>
wiki-clip https://example.com/some/page
```

To clip into a wiki elsewhere, pass its path as the first argument:
`wiki-clip path/to/wiki-repo <url>`.

It fetches the URL, converts the HTML to Markdown with
[markitdown](https://github.com/microsoft/markitdown), and writes
`raw/<slug>.md` with provenance frontmatter (`source_url`, `title`,
`fetched`, `clipped_by`). It then `git add`s the file but does **not**
commit — the operator reviews the clipped Markdown and commits, keeping
`raw/` a curated layer. `--name` overrides the filename stem.

## Editing a wiki by hand

A wiki is a normal git repo — edit it like one. Clone it, edit the
Markdown in any editor, commit, and push; the server picks the change
up on its next read. Wikilinks (`[[page]]`) render natively in Obsidian
and in VS Code with Foam; most git-host web views show them as plain
text.

## Linting

Linting is the maintenance cycle that keeps a wiki from rotting: find
the drift, then fix it. The fix is the point — a lint that only reports
has done half the job.

Detection has two layers, because drift comes in two kinds.

**Mechanical drift** — pages nothing links to, links to pages renamed
away, pages missing from the catalog, raw sources removed or changed
under a page — is deterministically computable. `wiki_lint` finds it:

| Kind | Meaning |
|---|---|
| `orphan` | A content page no other page links to. |
| `broken_link` | An outgoing link whose target page does not exist. |
| `unindexed` | A content page not linked from `index.md`, the catalog. |
| `source_missing` | A raw source a page was synthesised from no longer exists. |
| `source_drift` | Such a raw source's bytes changed since the page was synthesised. |

(`index.md` and `log.md` are exempt from the `orphan` and `unindexed`
checks — they are structural, not content pages. A `broken_link` is
still reported against `index.md`: the catalog is exactly where links
to renamed-away pages collect. The report is `{"issues": [{kind,
path, message}, …], "clean": <bool>}`.)

The two `source_*` checks are provenance checks. A page opts in by
listing the raw sources it was built from in a `sources:` frontmatter
block — each entry a `{path, etag}` pair, where `etag` is the value
`wiki_read_raw` returned for that source. Pages with no `sources:`
block are simply not provenance-tracked. Because a page is usually
synthesised from several sources, `source_missing` is graded: its
message states how many sources survive, so a partial loss (re-derive
from the remainder) reads differently from a total loss.

**Semantic drift** — contradictions, claims a newer source has
superseded, concepts mentioned but lacking their own page, missing
cross-references — cannot be computed; it takes an agent reading and
judging the content.

The agent then **remedies** each finding with `wiki_save` — relink an
orphan, stub or drop a broken link, add a page to `index.md`,
re-synthesise a page whose source was removed or changed, rewrite a
superseded claim. `wiki_lint` is read-only by design: a fix is a
content change and belongs in `wiki_save` (sanitiser, ETag, commit),
with the agent deciding *how*. The full detect-and-remedy loop is the
agent's *Lint* operation, defined in the `llm-wiki-lint` skill (see
[Companion skills](#companion-skills)).

## Status

Implemented and tested: all 8 tools, the sanitiser, path-safety, ETag
optimistic concurrency, git mediation, and the `log.md` / `index.md`
merge-drivers are in place and covered by the test suite — unit tests,
git-integration tests, and a multi-VM end-to-end suite. Pre-1.0 —
interfaces may still shift.

## Integration

This server is designed to be consumed by
[tank-agent-os](https://github.com/np6126/tank-agent-os) as a Quadlet-managed MCP service, but
works standalone for any MCP client. The image is published to a
container registry; consumers pin by digest.

## License

MIT — see [LICENSE](LICENSE).
