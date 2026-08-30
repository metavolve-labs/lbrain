"""A narrow import must not prune docs from sources it never walked.

CIO/keel brain, 2026-08-30: `lbrain import <experiment-subfolder>` pruned 38
`_COLLAB` inbox docs whose files were on disk the whole time. Root cause:
`prune_missing` used `source_roots` ONLY for the mount-gone guard, then pruned
against EVERY doc by on-disk existence — so a narrow import swept the whole brain.
The fix scopes prune eligibility to docs UNDER the imported roots.
"""
from lbrain.index import parse
from lbrain.store import Store


def _write(root, rel, txt):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(txt)
    return p


def _build(tmp_path):
    rootA = tmp_path / "A"
    rootB = tmp_path / "B"
    a1 = _write(rootA, "a1.md", "# A1\nalpha content one two three\n")
    b1 = _write(rootB, "b1.md", "# B1\nbeta content one two three\n")
    b2 = _write(rootB, "b2.md", "# B2\ngamma content one two three\n")
    st = Store(tmp_path / "brain.db")
    for root, p in [(rootA, a1), (rootB, b1), (rootB, b2)]:
        st.upsert_doc(parse(p, repo_root=root))
    st.db.commit()
    return st, rootA, rootB, b1


def test_narrow_prune_leaves_present_out_of_scope_docs(tmp_path):
    st, rootA, rootB, _ = _build(tmp_path)
    # keel's exact case: prune scoped to rootA, rootB files ALL still on disk.
    assert st.prune_missing(source_roots=[rootA]) == [], \
        "a rootA-scoped prune must not touch rootB docs whose files exist"


def test_narrow_prune_does_not_remove_gone_out_of_scope_doc(tmp_path):
    st, rootA, rootB, b1 = _build(tmp_path)
    b1.unlink()  # a rootB file genuinely vanishes
    # A rootA-scoped import/prune never walked rootB → must NOT prune b1.
    assert st.prune_missing(source_roots=[rootA]) == [], \
        "rootA-scoped prune must not prune a gone rootB doc it never walked"
    assert st.get_doc_hash("b1.md") is not None, "b1 must still be indexed"


def test_in_scope_prune_still_removes_gone_doc(tmp_path):   # NO-REGRESSION
    st, rootA, rootB, b1 = _build(tmp_path)
    b1.unlink()
    pruned = st.prune_missing(source_roots=[rootB])
    assert "b1.md" in pruned, "a rootB-scoped prune MUST remove the gone rootB doc"
    assert "b2.md" not in pruned, "a present doc is never pruned"
    assert "a1.md" not in pruned, "an out-of-scope doc is never pruned"


def test_whole_brain_sweep_unscoped_still_prunes_everything(tmp_path):   # NO-REGRESSION
    st, rootA, rootB, b1 = _build(tmp_path)
    b1.unlink()
    # source_roots=None → deliberate whole-brain sweep, every doc in scope as before.
    pruned = st.prune_missing(source_roots=None)
    assert "b1.md" in pruned
