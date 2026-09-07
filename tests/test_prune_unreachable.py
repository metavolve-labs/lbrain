"""UNREACHABLE rows: on disk, under no configured source — no import refreshes them,
`prune_missing` (existence-based) never removes them, so they are served forever.
Measured 2026-09-08 on a seat home: 719 rows from a memory directory imported once by
`lbrain import <subdir>` and never listed in `sources`. This verb removes exactly them,
with prune_missing's guards (mount-gone → nothing; >50% → refuse unless force)."""
import os

import pytest

from lbrain.index import parse
from lbrain.store import Store


def _write(root, rel, txt):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(txt)
    return p


def _build(tmp_path):
    rootA = tmp_path / "A"          # configured
    rootM = tmp_path / "memory"     # imported once, NOT in sources
    a1 = _write(rootA, "a1.md", "# A1\nalpha content one two three\n")
    a2 = _write(rootA, "a2.md", "# A2\nalpha content four five six\n")
    m1 = _write(rootM, "m1.md", "# M1\nmemory content one two three\n")
    st = Store(tmp_path / "brain.db")
    for root, p in [(rootA, a1), (rootA, a2), (rootM, m1)]:
        st.upsert_doc(parse(p, repo_root=root))
    st.db.commit()
    return st, rootA, rootM


def _rels(st):
    return sorted(r["rel_path"] for r in st.db.execute("SELECT rel_path FROM docs"))


def test_dry_run_lists_without_touching(tmp_path):
    st, rootA, rootM = _build(tmp_path)
    before = _rels(st)
    would = st.prune_unreachable(source_roots=[rootA], dry_run=True)
    assert would == ["m1.md"]
    assert _rels(st) == before


def test_prunes_only_rows_outside_every_configured_root(tmp_path):
    st, rootA, rootM = _build(tmp_path)
    pruned = st.prune_unreachable(source_roots=[rootA])
    assert pruned == ["m1.md"]
    assert _rels(st) == ["a1.md", "a2.md"]
    assert st.db.execute("SELECT COUNT(*) FROM chunks WHERE rel_path = ?", ("m1.md",)).fetchone()[0] == 0


def test_reachable_when_root_is_configured(tmp_path):
    st, rootA, rootM = _build(tmp_path)
    assert st.prune_unreachable(source_roots=[rootA, rootM]) == []
    assert len(_rels(st)) == 3


def test_missing_root_prunes_nothing(tmp_path):
    st, rootA, rootM = _build(tmp_path)
    gone_root = tmp_path / "unmounted"
    assert st.prune_unreachable(source_roots=[rootA, gone_root]) == []
    assert len(_rels(st)) == 3


def test_no_sources_refuses_to_guess(tmp_path):
    st, rootA, rootM = _build(tmp_path)
    assert st.prune_unreachable(source_roots=[]) == []
    assert len(_rels(st)) == 3


def test_fraction_guard_refuses_then_force(tmp_path):
    st, rootA, rootM = _build(tmp_path)
    # configure only the memory root: 2 of 3 rows (67%) become unreachable → refuse
    with pytest.raises(RuntimeError):
        st.prune_unreachable(source_roots=[rootM])
    assert len(_rels(st)) == 3
    assert sorted(st.prune_unreachable(source_roots=[rootM], force=True)) == ["a1.md", "a2.md"]
    assert _rels(st) == ["m1.md"]


def test_gone_file_is_not_this_verbs_business(tmp_path):
    """A row whose file vanished is prune_missing's (ORPHANED); this verb leaves it."""
    st, rootA, rootM = _build(tmp_path)
    os.remove(rootM / "m1.md")
    assert st.prune_unreachable(source_roots=[rootA]) == []
