# mcp-llm-wiki

A git-backed [MCP](https://modelcontextprotocol.io/) server that exposes
Karpathy's [LLM-Wiki pattern](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f)
as a tool surface for AI coding agents.

Wikis are plain Markdown stored in Git repositories (typically a Gitea
instance). One git repo = one wiki. Multiple agent VMs and humans can
read/write the same wiki concurrently; conflicts are mediated by ETag
optimistic concurrency plus custom merge-drivers for the two
write-contended hotspots (`log.md`, `index.md`).

The server runs in its own container, isolated from the agent. The agent
talks to it over HTTP-MCP and never touches the working trees directly.

## Design

- **Persistent, compounding artifact, not RAG.** Wiki pages are
  pre-synthesised Markdown, owned by the LLM agent layer.
- **Git as the source of truth.** Working trees in the container are
  ephemeral; the canonical bare repos live in Gitea. Every write
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

| Tool | Read-only | Purpose |
|---|---|---|
| `wiki_list` | ✓ | List pages with frontmatter summary |
| `wiki_read` | ✓ | Read a page (returns content + frontmatter + outgoing links + ETag) |
| `wiki_read_raw` | ✓ | Read from the immutable `raw/` source layer |
| `wiki_search` | ✓ | Fixed-string search over page bodies (ripgrep-backed; FTS5 hybrid planned) |
| `wiki_save` | idempotent | Upsert a page; atomic write + commit + push, ETag-guarded |
| `wiki_log_append` | non-idempotent | Append a timestamped entry to `log.md` |
| `wiki_lint` | ✓ | Heuristics-only drift report (orphans, broken links, stale-by-age) — never mutates |
| `wiki_graph` | ✓ | Backlinks map (parses wikilinks + Markdown links) |

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
