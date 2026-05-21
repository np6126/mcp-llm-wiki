"""Integration tests for the log.md / index.md merge drivers.

These hit real git: a shared bare repo plus two working trees (alice
and bob). Each working tree wires up the merge driver via repo-local
``git config merge.<name>.driver``. The tests verify the union /
sort / dedupe behaviour by triggering an actual merge.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

MERGE_DRIVERS = Path(__file__).resolve().parent.parent / "merge_drivers"


def _run(cmd: list[str], cwd: Path, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, check=check, capture_output=True, text=True)


@pytest.fixture
def two_clones(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Shared bare repo + alice/ + bob/ clones, both with merge drivers
    configured for log.md and index.md.
    """
    bare = tmp_path / "wiki.git"
    _run(["git", "init", "--quiet", "--bare", "--initial-branch=main", str(bare)], tmp_path)

    def make_clone(name: str) -> Path:
        wt = tmp_path / name
        _run(["git", "clone", "--quiet", str(bare), str(wt)], tmp_path)
        _run(["git", "config", "user.email", f"{name}@example.com"], wt)
        _run(["git", "config", "user.name", name], wt)
        # Wire merge drivers under repo-local names; .gitattributes
        # below maps log.md and index.md to those names.
        _run(
            [
                "git",
                "config",
                "merge.llm-wiki-log.driver",
                f"{MERGE_DRIVERS}/log_md_merge.sh %A %O %B",
            ],
            wt,
        )
        _run(
            [
                "git",
                "config",
                "merge.llm-wiki-index.driver",
                f"{MERGE_DRIVERS}/index_md_merge.sh %A %O %B",
            ],
            wt,
        )
        return wt

    alice = make_clone("alice")
    bob = make_clone("bob")

    # Alice seeds the repo: empty log.md + index.md plus .gitattributes
    # that routes log.md / index.md through the custom drivers.
    (alice / ".gitattributes").write_text(
        "log.md merge=llm-wiki-log\nindex.md merge=llm-wiki-index\n"
    )
    (alice / "log.md").write_text("# Log\n\n")
    (alice / "index.md").write_text("# Index\n\n")
    _run(["git", "add", "."], alice)
    _run(["git", "commit", "--quiet", "-m", "seed"], alice)
    _run(["git", "push", "--quiet"], alice)

    _run(["git", "pull", "--quiet"], bob)
    return bare, alice, bob


def test_log_md_merge_unions_and_sorts(two_clones):
    _, alice, bob = two_clones

    # Alice appends two entries.
    log_a = alice / "log.md"
    log_a.write_text(
        log_a.read_text()
        + "[2026-05-20T10:00:00Z] alice ingest | source A\n"
        + "[2026-05-20T10:30:00Z] alice query | question Q1\n"
    )
    _run(["git", "add", "log.md"], alice)
    _run(["git", "commit", "--quiet", "-m", "alice: append two"], alice)
    _run(["git", "push", "--quiet"], alice)

    # Bob, without pulling first, appends a different entry interleaved
    # in time. This is the conflict scenario the merge driver exists for.
    log_b = bob / "log.md"
    log_b.write_text(
        log_b.read_text() + "[2026-05-20T10:15:00Z] bob ingest | source B\n"
    )
    _run(["git", "add", "log.md"], bob)
    _run(["git", "commit", "--quiet", "-m", "bob: append one"], bob)

    # Pull triggers the merge driver. Should converge cleanly.
    result = _run(["git", "pull", "--no-rebase", "--quiet", "--no-edit"], bob, check=False)
    assert result.returncode == 0, result.stderr

    merged = log_b.read_text()
    # All three entries present, sorted ascending by timestamp prefix.
    lines = [line for line in merged.splitlines() if line.startswith("[")]
    assert lines == [
        "[2026-05-20T10:00:00Z] alice ingest | source A",
        "[2026-05-20T10:15:00Z] bob ingest | source B",
        "[2026-05-20T10:30:00Z] alice query | question Q1",
    ]


def test_log_md_merge_keeps_ancestor_entry_once(two_clones):
    # An entry both sides inherit through shared history is present in
    # the merge ancestor, so the 3-way union nets it to a single copy.
    # This is the legitimate dedupe the driver must still perform.
    _, alice, bob = two_clones

    shared = "## [2026-05-20T11:00:00Z] both ingest | same source"

    # Alice writes the entry and pushes; bob pulls it. It is now in
    # the common history of any later divergence.
    (alice / "log.md").write_text((alice / "log.md").read_text() + shared + "\n")
    _run(["git", "add", "log.md"], alice)
    _run(["git", "commit", "--quiet", "-m", "alice: shared entry"], alice)
    _run(["git", "push", "--quiet"], alice)
    _run(["git", "pull", "--quiet"], bob)

    # Each side now appends a distinct entry on top of the shared one.
    (alice / "log.md").write_text(
        (alice / "log.md").read_text() + "## [2026-05-20T12:00:00Z] alice only\n"
    )
    _run(["git", "add", "log.md"], alice)
    _run(["git", "commit", "--quiet", "-m", "alice: more"], alice)
    _run(["git", "push", "--quiet"], alice)

    (bob / "log.md").write_text(
        (bob / "log.md").read_text() + "## [2026-05-20T12:30:00Z] bob only\n"
    )
    _run(["git", "add", "log.md"], bob)
    _run(["git", "commit", "--quiet", "-m", "bob: more"], bob)
    _run(["git", "pull", "--no-rebase", "--quiet", "--no-edit"], bob, check=False)

    merged = (bob / "log.md").read_text()
    assert merged.count(shared) == 1


def test_log_md_merge_keeps_overlapping_independent_duplicate(two_clones):
    # When the driver *is* invoked — each side also carries a distinct
    # entry, so the merge is a genuine conflict — an identical entry
    # that both sides appended independently (absent from the ancestor)
    # must be kept twice. A blanket `sort -u` would drop one copy.
    #
    # Note: this does NOT cover two sides whose *only* change is the
    # same identical line. Git resolves that before any driver runs
    # (identical change on both sides / patch-id cherry-pick skip), so
    # that case cannot be fixed in the merge driver at all.
    _, alice, bob = two_clones

    dup = "## [2026-05-20T11:00:00Z] both ingest | same source"

    (alice / "log.md").write_text(
        (alice / "log.md").read_text()
        + dup + "\n"
        + "## [2026-05-20T11:30:00Z] alice unique\n"
    )
    _run(["git", "add", "log.md"], alice)
    _run(["git", "commit", "--quiet", "-m", "alice: append"], alice)
    _run(["git", "push", "--quiet"], alice)

    (bob / "log.md").write_text(
        (bob / "log.md").read_text()
        + dup + "\n"
        + "## [2026-05-20T12:00:00Z] bob unique\n"
    )
    _run(["git", "add", "log.md"], bob)
    _run(["git", "commit", "--quiet", "-m", "bob: append"], bob)
    _run(["git", "pull", "--no-rebase", "--quiet", "--no-edit"], bob, check=False)

    merged = (bob / "log.md").read_text()
    assert merged.count(dup) == 2
    assert "## [2026-05-20T11:30:00Z] alice unique" in merged
    assert "## [2026-05-20T12:00:00Z] bob unique" in merged


def test_log_md_merge_orders_mixed_format_by_timestamp(two_clones):
    # A mixed-format log must stay in timestamp order: the `## `
    # heading prefix must not sort a newer new-format entry ahead of
    # an older legacy bare entry (`#` 0x23 sorts before `[` 0x5B).
    _, alice, bob = two_clones

    # Alice: a legacy bare entry that is chronologically the earliest.
    (alice / "log.md").write_text(
        (alice / "log.md").read_text() + "[2026-05-19T09:00:00Z] legacy earliest\n"
    )
    _run(["git", "add", "log.md"], alice)
    _run(["git", "commit", "--quiet", "-m", "alice: legacy"], alice)
    _run(["git", "push", "--quiet"], alice)

    # Bob: a new-format heading entry that is chronologically latest.
    (bob / "log.md").write_text(
        (bob / "log.md").read_text() + "## [2026-05-22T09:00:00Z] heading latest\n"
    )
    _run(["git", "add", "log.md"], bob)
    _run(["git", "commit", "--quiet", "-m", "bob: heading"], bob)
    _run(["git", "pull", "--no-rebase", "--quiet", "--no-edit"], bob, check=False)

    merged = (bob / "log.md").read_text()
    entries = [
        ln for ln in merged.splitlines()
        if ln.startswith("[") or ln.startswith("## [")
    ]
    assert entries == [
        "[2026-05-19T09:00:00Z] legacy earliest",
        "## [2026-05-22T09:00:00Z] heading latest",
    ]


def test_log_md_merge_preserves_header(two_clones):
    _, alice, bob = two_clones

    (alice / "log.md").write_text(
        (alice / "log.md").read_text() + "[2026-05-20T12:00:00Z] alice ingest | A\n"
    )
    _run(["git", "add", "log.md"], alice)
    _run(["git", "commit", "--quiet", "-m", "alice"], alice)
    _run(["git", "push", "--quiet"], alice)

    (bob / "log.md").write_text(
        (bob / "log.md").read_text() + "[2026-05-20T12:05:00Z] bob ingest | B\n"
    )
    _run(["git", "add", "log.md"], bob)
    _run(["git", "commit", "--quiet", "-m", "bob"], bob)
    _run(["git", "pull", "--no-rebase", "--quiet", "--no-edit"], bob, check=False)

    merged = (bob / "log.md").read_text()
    # Header survives intact.
    assert merged.startswith("# Log\n")


def test_log_md_merge_unions_heading_format_entries(two_clones):
    # The wiki_log_append format: `## [timestamp] entry` heading lines.
    _, alice, bob = two_clones

    log_a = alice / "log.md"
    log_a.write_text(
        log_a.read_text()
        + "## [2026-05-21T10:00:00Z] ingest | source A\n"
        + "## [2026-05-21T10:30:00Z] query | question Q1\n"
    )
    _run(["git", "add", "log.md"], alice)
    _run(["git", "commit", "--quiet", "-m", "alice: append two"], alice)
    _run(["git", "push", "--quiet"], alice)

    log_b = bob / "log.md"
    log_b.write_text(
        log_b.read_text() + "## [2026-05-21T10:15:00Z] ingest | source B\n"
    )
    _run(["git", "add", "log.md"], bob)
    _run(["git", "commit", "--quiet", "-m", "bob: append one"], bob)

    result = _run(["git", "pull", "--no-rebase", "--quiet", "--no-edit"], bob, check=False)
    assert result.returncode == 0, result.stderr

    merged = log_b.read_text()
    lines = [line for line in merged.splitlines() if line.startswith("## [")]
    assert lines == [
        "## [2026-05-21T10:00:00Z] ingest | source A",
        "## [2026-05-21T10:15:00Z] ingest | source B",
        "## [2026-05-21T10:30:00Z] query | question Q1",
    ]


def test_log_md_merge_keeps_both_old_and_new_format(two_clones):
    # During the format transition a log.md holds both bare `[...]`
    # entries and new `## [...]` heading entries. The driver must
    # recognise both as entries and never silently drop the old ones.
    _, alice, bob = two_clones

    (alice / "log.md").write_text(
        (alice / "log.md").read_text()
        + "[2026-05-20T09:00:00Z] legacy ingest | old source\n"
    )
    _run(["git", "add", "log.md"], alice)
    _run(["git", "commit", "--quiet", "-m", "alice: legacy entry"], alice)
    _run(["git", "push", "--quiet"], alice)

    (bob / "log.md").write_text(
        (bob / "log.md").read_text()
        + "## [2026-05-21T09:00:00Z] ingest | new source\n"
    )
    _run(["git", "add", "log.md"], bob)
    _run(["git", "commit", "--quiet", "-m", "bob: new entry"], bob)
    _run(["git", "pull", "--no-rebase", "--quiet", "--no-edit"], bob, check=False)

    merged = (bob / "log.md").read_text()
    # Both formats survive — neither absorbed into the header or dropped.
    assert "[2026-05-20T09:00:00Z] legacy ingest | old source" in merged
    assert "## [2026-05-21T09:00:00Z] ingest | new source" in merged
    assert merged.startswith("# Log\n")


def test_index_md_merge_unions_unique_lines(two_clones):
    _, alice, bob = two_clones

    (alice / "index.md").write_text("# Index\n\n## Concepts\n- [[etag]]\n")
    _run(["git", "add", "index.md"], alice)
    _run(["git", "commit", "--quiet", "-m", "alice: concepts"], alice)
    _run(["git", "push", "--quiet"], alice)

    (bob / "index.md").write_text("# Index\n\n## Entities\n- [[gitea]]\n")
    _run(["git", "add", "index.md"], bob)
    _run(["git", "commit", "--quiet", "-m", "bob: entities"], bob)
    result = _run(["git", "pull", "--no-rebase", "--quiet", "--no-edit"], bob, check=False)
    assert result.returncode == 0, result.stderr

    merged = (bob / "index.md").read_text()
    # Both unique lines present.
    assert "- [[etag]]" in merged
    assert "- [[gitea]]" in merged


def test_index_md_merge_no_duplicate_lines(two_clones):
    _, alice, bob = two_clones

    shared_line = "- [[shared_entity]]"

    (alice / "index.md").write_text(f"# Index\n\n{shared_line}\n- [[only_alice]]\n")
    _run(["git", "add", "index.md"], alice)
    _run(["git", "commit", "--quiet", "-m", "alice"], alice)
    _run(["git", "push", "--quiet"], alice)

    (bob / "index.md").write_text(f"# Index\n\n{shared_line}\n- [[only_bob]]\n")
    _run(["git", "add", "index.md"], bob)
    _run(["git", "commit", "--quiet", "-m", "bob"], bob)
    _run(["git", "pull", "--no-rebase", "--quiet", "--no-edit"], bob, check=False)

    merged = (bob / "index.md").read_text()
    assert merged.count(shared_line) == 1
    assert "- [[only_alice]]" in merged
    assert "- [[only_bob]]" in merged


def test_merge_drivers_executable():
    """Sanity check that the scripts are executable in the source tree."""
    for name in ("log_md_merge.sh", "index_md_merge.sh"):
        path = MERGE_DRIVERS / name
        assert path.exists(), f"missing {path}"
        # Owner exec bit at minimum.
        assert path.stat().st_mode & 0o100
