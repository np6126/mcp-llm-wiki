"""End-to-end multi-VM scenario.

We model two agent VMs as two distinct FastMCP servers, each pointing
at its own clone of the same shared bare repository (the bare repo
stands in for a Gitea-hosted canonical store). The tests then walk
through the user-visible flows from the plan's verification section:

  - VM-A wiki_save → push → bare repo
  - VM-B wiki_read sees VM-A's edit (after a pull, which wiki_save and
    wiki_log_append do internally)
  - Concurrent ETag mismatch flow: VM-A and VM-B both wiki_read,
    VM-A wiki_save, VM-B wiki_save → ETag mismatch surfaced
  - Concurrent log appends from both VMs converge via the log.md
    merge driver

These tests exercise the full pipeline the way the agent will use it,
short of an actual Gitea HTTP endpoint — git push/pull over file:// is
fungible for the wire layer the way Gitea over HTTPS exposes it.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from mcp_llm_wiki import server as server_mod
from mcp_llm_wiki.config import Config

MERGE_DRIVERS = Path(__file__).resolve().parent.parent / "merge_drivers"


def _run(cmd: list[str], cwd: Path, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, check=check, capture_output=True, text=True)


@pytest.fixture(scope="session", autouse=True)
def require_git():
    if shutil.which("git") is None:
        pytest.skip("git binary not available in test env")


def _make_clone(parent: Path, vm_name: str, bare: Path) -> Path:
    """Set up a wiki working tree mimicking what entrypoint.sh produces."""
    root = parent / f"{vm_name}-wikis"
    root.mkdir()
    work = root / "test"
    _run(["git", "clone", "--quiet", str(bare), str(work)], parent)
    _run(["git", "config", "user.email", f"{vm_name}@gitea.local"], work)
    _run(["git", "config", "user.name", vm_name], work)
    _run(
        [
            "git", "config", "merge.llm-wiki-log.driver",
            f"{MERGE_DRIVERS}/log_md_merge.sh %A %O %B",
        ],
        work,
    )
    _run(
        [
            "git", "config", "merge.llm-wiki-index.driver",
            f"{MERGE_DRIVERS}/index_md_merge.sh %A %O %B",
        ],
        work,
    )
    return root


def _make_server(wiki_root: Path, vm_name: str):
    config = Config(
        root=wiki_root,
        wikis_rw=frozenset({"test"}),
        wikis_readonly=frozenset(),
        agent_identity=vm_name,
        port=3100,
    )
    return server_mod.build_server(config)


@pytest.fixture
def two_vms(tmp_path: Path):
    """Two MCP server instances on the same shared bare repo."""
    bare = tmp_path / "test.git"
    _run(["git", "init", "--quiet", "--bare", "--initial-branch=main", str(bare)], tmp_path)

    # Seed via a third throwaway clone so both VMs start from a non-empty
    # tree with the merge-driver-aware .gitattributes already in place.
    seed = tmp_path / "seed"
    _run(["git", "clone", "--quiet", str(bare), str(seed)], tmp_path)
    _run(["git", "config", "user.email", "seed@gitea.local"], seed)
    _run(["git", "config", "user.name", "seed"], seed)
    (seed / "wiki").mkdir()
    (seed / "raw").mkdir()
    (seed / ".gitattributes").write_text(
        "log.md merge=llm-wiki-log\nindex.md merge=llm-wiki-index\n"
    )
    (seed / "log.md").write_text("# Log\n\n")
    (seed / "wiki" / "index.md").write_text("# Index\n\n")
    _run(["git", "add", "."], seed)
    _run(["git", "commit", "--quiet", "-m", "seed"], seed)
    _run(["git", "push", "--quiet"], seed)

    vm_a_root = _make_clone(tmp_path, "wiki-bot-vmA", bare)
    vm_b_root = _make_clone(tmp_path, "wiki-bot-vmB", bare)
    return _make_server(vm_a_root, "wiki-bot-vmA"), _make_server(vm_b_root, "wiki-bot-vmB"), bare


async def _call(server, tool: str, **kwargs):
    return await server.call_tool(tool, kwargs)


@pytest.mark.asyncio
async def test_vmA_save_is_visible_to_vmB(two_vms):
    vm_a, vm_b, _ = two_vms
    await _call(
        vm_a,
        "wiki_save",
        wiki="test",
        page="concepts/etag.md",
        content="---\ntitle: ETag\n---\nContent fingerprint.\n",
    )
    # VM-B wiki_read pulls from origin first (via the save→read chain
    # in the agent's flow; here we drive it with a direct wiki_save to
    # force a pull, then wiki_read sees the file).
    await _call(
        vm_b,
        "wiki_save",
        wiki="test",
        page="entities/gitea.md",
        content="A git server.\n",
    )
    _, page = await _call(vm_b, "wiki_read", wiki="test", page="concepts/etag.md")
    assert page["frontmatter"]["title"] == "ETag"
    assert "fingerprint" in page["content"]


@pytest.mark.asyncio
async def test_concurrent_etag_mismatch(two_vms):
    """Both VMs read v1, both build v2 in their head. Last writer must
    be rejected by ETag mismatch so the agent can re-read and merge.
    """
    vm_a, vm_b, _ = two_vms

    # VM-A creates the page.
    _, first = await _call(
        vm_a,
        "wiki_save",
        wiki="test",
        page="concepts/conflict.md",
        content="v1\n",
    )
    initial_etag = first["etag"]

    # Both VMs read it and capture the same ETag.
    _, a_read = await _call(vm_a, "wiki_read", wiki="test", page="concepts/conflict.md")
    _, b_read = await _call(vm_b, "wiki_read", wiki="test", page="concepts/conflict.md")
    assert a_read["etag"] == b_read["etag"] == initial_etag

    # VM-A writes successfully.
    _, a_save = await _call(
        vm_a,
        "wiki_save",
        wiki="test",
        page="concepts/conflict.md",
        content="v2-from-A\n",
        etag=initial_etag,
    )
    assert a_save["committed"] is True

    # VM-B tries to write with the now-stale ETag. Save's internal pull
    # rebase brings A's commit down; the ETag check fails.
    with pytest.raises(Exception, match="etag_mismatch"):
        await _call(
            vm_b,
            "wiki_save",
            wiki="test",
            page="concepts/conflict.md",
            content="v2-from-B\n",
            etag=initial_etag,
        )

    # VM-B re-reads (now sees A's version + new ETag) and retries.
    _, b_reread = await _call(vm_b, "wiki_read", wiki="test", page="concepts/conflict.md")
    assert "v2-from-A" in b_reread["content"]
    _, b_save = await _call(
        vm_b,
        "wiki_save",
        wiki="test",
        page="concepts/conflict.md",
        content="v3-merged\n",
        etag=b_reread["etag"],
    )
    assert b_save["committed"] is True


@pytest.mark.asyncio
async def test_concurrent_log_appends_merge(two_vms):
    """Both VMs append to log.md without coordination. The merge driver
    should converge: both entries end up in the final log, sorted by
    timestamp, no duplicates.
    """
    vm_a, vm_b, bare = two_vms

    # Force the writes to land in time order: A first, then B.
    await _call(vm_a, "wiki_log_append", wiki="test", entry="ingest | source-A")
    await _call(vm_b, "wiki_log_append", wiki="test", entry="ingest | source-B")

    # Read the final log from the bare repo.
    log_text = _run(["git", "show", "main:log.md"], bare).stdout
    assert "source-A" in log_text
    assert "source-B" in log_text
    # The merge driver sorts entries by their ISO timestamp; A was
    # appended before B, so A's line precedes B's.
    assert log_text.index("source-A") < log_text.index("source-B")


@pytest.mark.asyncio
async def test_concurrent_identical_log_appends_both_survive(two_vms):
    """Both VMs append a log entry with byte-identical text. The
    per-entry nonce keeps the two lines distinct, so neither commit
    looks like a cherry-pick of the other and both appends survive the
    rebase. Without the nonce the lines would be identical, git would
    drop one commit as an already-applied patch, and one append would
    be silently lost.
    """
    vm_a, vm_b, bare = two_vms

    await _call(vm_a, "wiki_log_append", wiki="test", entry="ingest | same source")
    await _call(vm_b, "wiki_log_append", wiki="test", entry="ingest | same source")

    log_text = _run(["git", "show", "main:log.md"], bare).stdout
    entry_lines = [ln for ln in log_text.splitlines() if ln.startswith("## [")]
    # Both appends present — neither dropped as a duplicate patch.
    assert len(entry_lines) == 2
    # Same entry text, distinct lines: the nonce is what differs.
    assert entry_lines[0] != entry_lines[1]
    assert all(ln.endswith("ingest | same source") for ln in entry_lines)


@pytest.mark.asyncio
async def test_human_edit_visible_to_vm(two_vms, tmp_path):
    """Operator on their laptop git-clones, edits a page, pushes.
    Next wiki_save in a VM does a pull-rebase and the agent then sees
    the human's edit via wiki_read.
    """
    vm_a, _vm_b, bare = two_vms
    human_clone = tmp_path / "operator-laptop"
    _run(["git", "clone", "--quiet", str(bare), str(human_clone)], tmp_path)
    _run(["git", "config", "user.email", "operator@example.com"], human_clone)
    _run(["git", "config", "user.name", "Operator"], human_clone)

    page = human_clone / "wiki" / "entities" / "human_added.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(
        "---\ntitle: Human Added\nkind: entity\n---\nThis was added by a person.\n"
    )
    _run(["git", "add", "."], human_clone)
    _run(["git", "commit", "--quiet", "-m", "operator: add page"], human_clone)
    _run(["git", "push", "--quiet"], human_clone)

    # Trigger a pull in VM-A by doing any wiki_save (saves always start
    # with pull --rebase).
    await _call(
        vm_a,
        "wiki_save",
        wiki="test",
        page="concepts/placeholder.md",
        content="placeholder\n",
    )
    _, read = await _call(
        vm_a, "wiki_read", wiki="test", page="entities/human_added.md"
    )
    assert read["frontmatter"]["title"] == "Human Added"
    assert "added by a person" in read["content"]
