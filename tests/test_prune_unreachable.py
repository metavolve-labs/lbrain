"""UNREACHABLE rows: on disk, under no configured source — no import refreshes them,
`prune_missing` (existence-based) never removes them, so they are served forever.
Measured 2026-09-07 on a seat home: 719 rows (658 lair docs outside the configured
sources + 61 of another seat's persona files) left by a wider import never listed in `sources`. This verb removes exactly them,
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


# ---- CSO/mac review of PR #57 (2026-09-07): A case-differing root, B dry run over the line,
# ---- C same-run import+prune. RED against 903eed8, GREEN after the fix.

def test_case_differing_root_is_still_reachable(monkeypatch):
    """A (unit): a root spelled in different case must count as the same root on a
    case-insensitive filesystem. Simulated by making normcase fold case, so the test
    is deterministic on Linux tmpfs too."""
    import os
    from lbrain import store as S
    monkeypatch.setattr(S.os.path, "normcase", str.lower)
    monkeypatch.setattr(S.os.path, "realpath", lambda p: str(p))
    roots = [S._norm_path("/Mnt/C/Users/x/Lairs")]
    assert S._under_roots("/mnt/c/users/x/lairs/a/b.md", roots)
    assert not S._under_roots("/mnt/c/users/x/other/b.md", roots)


def _fs_is_case_insensitive() -> bool:
    import tempfile, pathlib
    d = pathlib.Path(tempfile.mkdtemp())
    (d / "CaSe.txt").write_text("x")
    return (d / "case.txt").exists()


@pytest.mark.skipif(not _fs_is_case_insensitive(),
                    reason="filesystem is case-sensitive; the live reproduction needs DrvFs/APFS")
def test_case_differing_root_live(tmp_path):
    """A (live, only on a case-insensitive fs): the CSO's reproduction shape."""
    st, rootA, rootM = _build(tmp_path)
    swapped = str(rootA).swapcase()
    assert os.path.isdir(swapped)
    kept = st.prune_unreachable(source_roots=[swapped, rootM], dry_run=True)
    assert kept == []


def test_dry_run_lists_even_when_over_the_fraction_guard(tmp_path):
    """B: inspection must never need --force. 2 of 3 rows unreachable (>50%)."""
    st, rootA, rootM = _build(tmp_path)
    listed = st.prune_unreachable(source_roots=[rootM], dry_run=True)   # A's two rows are unreachable now
    assert sorted(listed) == ["a1.md", "a2.md"]
    assert _rels(st) == ["a1.md", "a2.md", "m1.md"]                      # nothing touched
    with pytest.raises(RuntimeError):
        st.prune_unreachable(source_roots=[rootM])                         # apply still refuses
    assert sorted(st.prune_unreachable(source_roots=[rootM], force=True)) == ["a1.md", "a2.md"]


def test_walked_dir_counts_as_reachable_for_the_same_run(tmp_path):
    """C: the import call site now passes cfg.sources + this run's paths. At store level:
    a root list that includes the just-walked dir keeps its docs."""
    st, rootA, rootM = _build(tmp_path)
    assert st.prune_unreachable(source_roots=[rootA, rootM], dry_run=True) == []
    assert st.prune_unreachable(source_roots=[rootA], dry_run=True) == ["m1.md"]


def test_prune_missing_also_clears_claim_spans(tmp_path):
    """CSO/mac note: prune_missing does not delete claim_spans explicitly. It does not
    need to: claim_spans.src_path REFERENCES docs(rel_path) ON DELETE CASCADE and the
    store opens with PRAGMA foreign_keys=ON. This test pins the cascade (GREEN on 903eed8)."""
    st, rootA, rootM = _build(tmp_path)
    st.db.execute("INSERT INTO claim_spans (src_path, claim_text, status, valid_to) VALUES (?,?,?,?)",
                  ("m1.md", "claim", "active", None))
    st.db.commit()
    (rootM / "m1.md").unlink()
    st.prune_missing(source_roots=[rootA, rootM], force=True)
    assert st.db.execute("SELECT COUNT(*) FROM claim_spans WHERE src_path='m1.md'").fetchone()[0] == 0
