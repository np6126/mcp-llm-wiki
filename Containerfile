# mcp-llm-wiki: MCP server that exposes git-backed LLM wikis as MCP tools.
#
# Trust posture:
#   - pip is fed only via lockfile + --require-hashes (no transitive
#     supply-chain surprises). See requirements.txt for digest pins.
#   - The container has no secrets at build time. Token + operator config
#     arrive as ENV at run time via podman secrets (see
#     tank-agent-os/sync-podman-secrets pattern).
#   - Working trees live on a host-mounted volume (/wikis); the image
#     itself is read-only at runtime.

ARG FEDORA_CONTAINER_IMAGE=quay.io/fedora/fedora
ARG FEDORA_CONTAINER_REF=44

# UID/GID of the in-container service user. Matches the host clawx user
# in tank-agent-os (1000) so a bind-mounted ~/.clawx/llm-wiki/ stays
# writable without keep-id mapping shenanigans.
ARG WIKI_UID=1000
ARG WIKI_GID=1000

FROM ${FEDORA_CONTAINER_IMAGE}:${FEDORA_CONTAINER_REF}

ARG WIKI_UID
ARG WIKI_GID

# Python 3.12 ships with Fedora 44. The MCP SDK requires 3.10+.
# git is needed for all mediation: clone, pull --rebase, commit, push.
# ripgrep backs wiki_search (Phase-1 fixed-string search; an FTS5
# hybrid is planned but not yet implemented).
RUN set -eux; \
    dnf -y install \
      ca-certificates \
      git \
      python3 \
      python3-pip \
      ripgrep; \
    dnf clean all; \
    rm -rf /var/cache/dnf /var/log/dnf*

# Non-root service user. The home dir is where the entrypoint will
# configure global git defaults (user.email, init.defaultBranch).
RUN groupadd -g ${WIKI_GID} wiki && \
    useradd -u ${WIKI_UID} -g ${WIKI_GID} -m -s /usr/sbin/nologin wiki && \
    install -d -o wiki -g wiki -m 0755 /wikis

# Install Python deps with hash verification. requirements.txt is
# regenerated via `pip-compile --generate-hashes requirements.in`;
# never hand-edit.
COPY requirements.txt /tmp/requirements.txt
RUN python3 -m pip install --no-cache-dir --require-hashes \
      -r /tmp/requirements.txt && \
    rm /tmp/requirements.txt

COPY src/ /opt/mcp-llm-wiki/src/
COPY entrypoint.sh /usr/local/bin/llm-wiki-init
COPY merge_drivers/ /opt/mcp-llm-wiki/merge_drivers/
RUN chmod 0755 /usr/local/bin/llm-wiki-init && \
    chmod 0755 /opt/mcp-llm-wiki/merge_drivers/*.sh

ENV PYTHONPATH=/opt/mcp-llm-wiki/src \
    PYTHONUNBUFFERED=1 \
    MCP_LLM_WIKI_MERGE_DRIVERS=/opt/mcp-llm-wiki/merge_drivers \
    MCP_LLM_WIKI_ROOT=/wikis

USER wiki
WORKDIR /wikis

EXPOSE 3100

# llm-wiki-init clones any missing wikis listed in AGENT_LLM_WIKIS_*
# env vars, installs merge-drivers in each working tree, then exec's
# the MCP server. The server listens on 0.0.0.0:3100/mcp and is reached
# from peer containers on the clawx-isolated bridge by container name.
ENTRYPOINT ["/usr/local/bin/llm-wiki-init"]
