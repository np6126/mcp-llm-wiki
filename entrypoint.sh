#!/usr/bin/env bash
# llm-wiki-init: PID 1 inside the mcp-llm-wiki container.
#
# Responsibilities:
#   1. Configure git globals (user.email is required for commits).
#   2. For each wiki listed in AGENT_LLM_WIKIS_RW + AGENT_LLM_WIKIS_READONLY,
#      clone into $MCP_LLM_WIKI_ROOT/<name>/ if it isn't there yet.
#   3. Install custom merge-drivers (log.md, index.md) inside each clone
#      via repo-local `git config merge.<name>.driver = ...`. The
#      .gitattributes file ships inside each wiki repo and references
#      these driver names.
#   4. exec the Python MCP server.
#
# Failure modes:
#   - Git host unreachable on first clone: log and retry indefinitely with
#     backoff. Pod stays up so consumers see a clean MCP error, not a
#     crash-loop.
#   - Token missing: hard-fail (clearly misconfigured operator setup).

set -euo pipefail

: "${AGENT_LLM_WIKI_URL:?AGENT_LLM_WIKI_URL is required (set via sync-podman-secrets)}"
: "${AGENT_LLM_WIKI_USER:?AGENT_LLM_WIKI_USER is required (git-host service account name)}"
: "${AGENT_LLM_WIKI_TOKEN:?AGENT_LLM_WIKI_TOKEN is required (git-host API token)}"

# Owner namespace the wiki repos live under. This is usually a git-host org
# (e.g. a team), distinct from AGENT_LLM_WIKI_USER which is the service
# account used only for auth + commit identity — that account is just a
# collaborator on the wiki repos, not their owner. Defaults to
# AGENT_LLM_WIKI_USER for the simple case where the repos sit in the
# service account's own namespace.
AGENT_LLM_WIKI_ORG="${AGENT_LLM_WIKI_ORG:-$AGENT_LLM_WIKI_USER}"

MCP_LLM_WIKI_ROOT="${MCP_LLM_WIKI_ROOT:-/wikis}"
MCP_LLM_WIKI_MERGE_DRIVERS="${MCP_LLM_WIKI_MERGE_DRIVERS:-/opt/mcp-llm-wiki/merge_drivers}"
MCP_LLM_WIKI_PORT="${MCP_LLM_WIKI_PORT:-3100}"

git config --global user.email "${AGENT_LLM_WIKI_USER}@llm-wiki.local"
git config --global user.name "${AGENT_LLM_WIKI_USER}"
git config --global init.defaultBranch main
git config --global pull.rebase true

# Combine RW + READONLY wikis; the server enforces the capability
# distinction, the entrypoint just clones whatever is configured.
wikis="${AGENT_LLM_WIKIS_RW:-}"
if [ -n "${AGENT_LLM_WIKIS_READONLY:-}" ]; then
  wikis="${wikis:+${wikis},}${AGENT_LLM_WIKIS_READONLY}"
fi

if [ -z "${wikis}" ]; then
  echo "llm-wiki-init: no wikis configured (AGENT_LLM_WIKIS_RW + AGENT_LLM_WIKIS_READONLY both empty)" >&2
  echo "llm-wiki-init: server will start, but every tool call will return an empty-config error" >&2
fi

# Embed the token into the remote URL via the HTTPS basic-auth slot.
# Strip a trailing slash so concatenation is predictable.
base="${AGENT_LLM_WIKI_URL%/}"
authed_base="${base/https:\/\//https://${AGENT_LLM_WIKI_USER}:${AGENT_LLM_WIKI_TOKEN}@}"

clone_one() {
  local name="$1"
  local dir="${MCP_LLM_WIKI_ROOT}/${name}"
  if [ -d "${dir}/.git" ]; then
    return 0
  fi
  local remote="${authed_base}/${AGENT_LLM_WIKI_ORG}/${name}.git"
  local attempt=0
  until git clone --quiet "${remote}" "${dir}"; do
    attempt=$((attempt + 1))
    local delay=$((attempt < 6 ? attempt * 5 : 30))
    echo "llm-wiki-init: clone of ${name} failed (attempt ${attempt}); retrying in ${delay}s" >&2
    sleep "${delay}"
  done
  install_merge_drivers "${dir}"
}

install_merge_drivers() {
  local dir="$1"
  # Driver names match what wiki repos declare in their .gitattributes.
  # Installing them locally per clone avoids a global config that would
  # leak into unrelated repos. The driver scripts read three temp paths
  # (ancestor / current / other) and rewrite the current file in place.
  git -C "${dir}" config merge.llm-wiki-log.name "Append-only timestamp-sorted log merge"
  git -C "${dir}" config merge.llm-wiki-log.driver \
    "${MCP_LLM_WIKI_MERGE_DRIVERS}/log_md_merge.sh %A %O %B"
  git -C "${dir}" config merge.llm-wiki-index.name "Section-aware index.md merge"
  git -C "${dir}" config merge.llm-wiki-index.driver \
    "${MCP_LLM_WIKI_MERGE_DRIVERS}/index_md_merge.sh %A %O %B"
}

if [ -n "${wikis}" ]; then
  IFS=',' read -ra wiki_list <<< "${wikis}"
  for w in "${wiki_list[@]}"; do
    w_trimmed="$(echo "${w}" | tr -d '[:space:]')"
    if [ -n "${w_trimmed}" ]; then
      clone_one "${w_trimmed}"
    fi
  done
fi

exec python3 -m mcp_llm_wiki.server --port "${MCP_LLM_WIKI_PORT}"
