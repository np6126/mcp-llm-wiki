# mcp-llm-wiki audit — 2026-05-20

**Target.** The `mcp-llm-wiki` MCP server (this repository).
**License.** MIT.
**Method.** Source-tree review against the CSA `mcpserver-audit` framework
([ModelContextProtocol-Security/mcpserver-audit](https://github.com/ModelContextProtocol-Security/mcpserver-audit)).
The server is first-party, so this is a self-audit; it is recorded in the
repository so the security review travels with the source and can be
re-run on every security-relevant change.

## Disposition

**Accept.** `mcp-llm-wiki` was built security-first: a paranoid
write-side sanitiser, symlink-aware path containment, hash-pinned Python
dependencies, and no secrets baked into the image. The 73-test suite
covers each of those properties. One informational finding (F-LW-002 —
the git token persisted in per-clone `.git/config`) is accepted with a
recommended follow-up; it does not block the release because the token
is a per-deployment scoped service-account credential and the
working-tree volume is not exposed to a consuming agent.

## Checks applied

| Check | Result |
|---|---|
| Credential management (CWE-798, CWE-522) | PASS — the git API token arrives only as the `AGENT_LLM_WIKI_TOKEN` environment variable. Nothing is baked into the image; no token in source. See F-LW-002 for the on-disk `.git/config` note. |
| Dynamic content execution (CWE-94) | PASS — no `eval`/`exec`/`vm` of any wiki content. The only subprocesses are `git` and `rg`, both invoked with fixed argv arrays (never a shell string), so wiki content cannot reach a command line. |
| Path traversal (CWE-22) | PASS — `path_safety.resolve_within` rejects absolute paths, `..`/`.`/empty segments (validated on the raw string before `PurePosixPath` normalises), and any symlink at any path component; it re-verifies containment after resolution. Covered by dedicated tests in `test_path_safety.py`, including leaf- and intermediate-component symlink traversal. |
| Write-side content sanitisation | PASS — every `wiki_save` and `wiki_log_append` strips HTML comments, zero-width characters, bidi overrides, raw HTML (outside a tiny structural whitelist), inline `style=` CSS, and `data:` image URLs. The strip count is mirrored into the commit message for operator visibility. Covered by `test_sanitizer.py`. |
| Telemetry / outbound analytics (CWE-200) | PASS — none. The server makes no outbound HTTP of its own; the only egress is `git` to the configured git host. |
| Network port binding (CWE-200) | INFO — the MCP HTTP transport binds `0.0.0.0:3100`. The server is intended to run on an isolated container network with no published host port; because the endpoint is unauthenticated (see below) a deployment must not expose the port publicly. |
| Authentication / authorization | INFO — the MCP endpoint itself is unauthenticated; it relies on network isolation. Per-wiki authorization is real: `AGENT_LLM_WIKIS_RW` vs `AGENT_LLM_WIKIS_READONLY` is enforced in the server, and git-host collaborator permissions enforce it again upstream. |
| Concurrency / data integrity | PASS — `wiki_save` uses ETag optimistic concurrency (sha256 of on-disk bytes); writes are atomic (tmp + fsync + `os.replace`); `git push` retries on non-fast-forward with a bounded loop; `log.md`/`index.md` use custom merge-drivers so concurrent appends from multiple clients converge. |
| Writable paths | PASS — the wiki working trees live in the server container's own named volume mounted at `/wikis`. The server writes only there; it has no reach into a consuming agent's filesystem. |
| Logging of sensitive data | INFO — the server logs tool names and wiki names, not page content or the token. `git` subprocess output is surfaced on error; modern git redacts in-URL credentials in its messages (see F-LW-002). |
| Supply chain — runtime deps | PASS — Python dependencies are installed with `pip install --require-hashes` from a `pip-compile --generate-hashes` lockfile. The build refuses to produce an unpinned image. |
| Pinning / supply chain | PASS — the published container image is intended to be consumed by digest pin; consumers pin the digest rather than a moving tag. |
| Container base | INFO — `python:3.12-slim` (Debian). A standard Debian-family trust surface. |

## Detailed findings

### F-LW-001 — Wiki pages are a prompt-injection-persistence surface

**Severity.** Informational — inherent to the feature, mitigated.
**Where.** The wiki content model itself.
**Risk.** A wiki is shared, long-lived content. A page poisoned in one
session — by a manipulated agent, by a human, or by ingesting a hostile
source — is read by the next session and by other clients.
**Mitigation.** Server-side: the write-side sanitiser strips the
established injection patterns before any content is committed.
Consumer-side (outside this server's control, but required for the
feature to be safe): the consuming agent must treat wiki pages as data,
not instructions — at the same trust level as any other workspace
content — and verify synthesis pages against the immutable `raw/` source
layer. Sanitisation is hardening, not a guarantee; the residual risk is
inherent to a persistent shared-knowledge feature and is accepted.

### F-LW-002 — git token persists in per-clone `.git/config`

**Severity.** Informational / low.
**Where.** `entrypoint.sh` clones each wiki with the token embedded in
the HTTPS remote URL (`https://user:token@host/...`). git stores
`remote.origin.url` verbatim in each clone's `.git/config`, so the token
lands on disk in each clone's `.git/config` inside the working-tree
volume.
**Risk.** The token is readable by a process inside the server container
or by a host user who can read the volume store. It is not exposed to a
consuming agent — that container does not mount the volume.
**Disposition.** **Accept with follow-up.** The token is a
per-deployment service-account credential, scoped by collaborator
permission to only that deployment's wikis, and individually revocable.
Recommended follow-up: set the remote URL tokenless and supply the
credential via `git -c http.extraheader=` or a credential helper, so it
never persists in `.git/config`. Tracked, not blocking.

## Recommended ongoing controls

- Regenerate the dependency hashes (`pip-compile --generate-hashes`) on
  every dependency bump; never hand-edit `requirements.txt`.
- Any change to `sanitizer.py`, `path_safety.py`, or the git-mediation
  code carries a security note in the PR and re-runs the test suite.
- Address F-LW-002 (tokenless remote URL) as a hardening follow-up.

## Test suite

`pytest --collect-only` (2026-05-20): **73 tests** across the unit suites
(sanitiser, path-safety, ETag / wiki I/O, config, merge-drivers, server
tools and writes) plus a multi-VM end-to-end suite.
