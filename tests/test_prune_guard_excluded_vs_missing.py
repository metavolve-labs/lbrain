"""Fortify step 14 (2026-10-16): the prune guard counts MISSING files, not policy-EXCLUDED ones.

Live failure: delisting /claimed/ from the CRO brain removed 4,447 of 8,433 docs (53%). Every one of them was
on disk and excluded by the operator's own config, yet `import --prune` refused it as "probably unmounted",
and `epoch build` (which exposes no override) could not publish the delist at all. The guard exists for the
unmounted-source failure, where files LOOK gone; a delist is a config decision, not a vanished mount.
Each behaviour is paired with its opposite, so the tests fail in both directions.
"""
import pytest

from lbrain import index
from lbrain.index import parse
from lbrain.store import Store


@pytest.fixture(autouse=True)
def _reset_markers():
    index.set_exclude_markers(())
    yield
    index.set_exclude_markers(())


def _brain(tmp_path, n_mail=6, n_live=4):
    root = tmp_path / "corpus"
    paths = []
    for i in range(n_mail):
        p = root / "claimed" / f"mail-{i}.md"; p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"# Mail {i}\n\nA processed mail number {i}.\n", encoding="utf-8"); paths.append(p)
    for i in range(n_live):
        p = root / "lairs" / f"live-{i}.md"; p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"# Live {i}\n\nA live record number {i}.\n", encoding="utf-8"); paths.append(p)
    store = Store(tmp_path / "brain.db", embedding_dim=8)
    for p in paths:
        store.upsert_doc(parse(p, repo_root=root))
    store.db.commit()
    return root, store


def _count(store):
    return store.db.execute("SELECT count(*) FROM docs").fetchone()[0]


def test_delist_over_half_the_corpus_is_not_refused(tmp_path):
    root, store = _brain(tmp_path)               # 6 of 10 docs live under /claimed/
    index.set_exclude_markers(["/claimed/"])
    gone = store.prune_missing(source_roots=[root])
    assert len(gone) == 6 and _count(store) == 4, "a policy delist of 60% must publish without --force-prune"
    assert all("claimed" in g for g in gone), "only the excluded docs are removed"


def test_missing_files_over_half_are_still_refused(tmp_path):
    root, store = _brain(tmp_path)
    for p in (root / "claimed").iterdir():       # the unmounted-source shape: files vanish
        p.unlink()
    with pytest.raises(RuntimeError, match="MISSING"):
        store.prune_missing(source_roots=[root])
    assert _count(store) == 10, "the guard still protects the index from vanished files"


def test_force_still_overrides_the_missing_guard(tmp_path):
    root, store = _brain(tmp_path)
    for p in (root / "claimed").iterdir():
        p.unlink()
    gone = store.prune_missing(source_roots=[root], force=True)
    assert len(gone) == 6 and _count(store) == 4


def test_excluded_docs_do_not_mask_a_real_missing_majority(tmp_path):
    """Negative control on the split: a few excluded docs must not dilute a real missing majority."""
    root, store = _brain(tmp_path, n_mail=2, n_live=8)
    index.set_exclude_markers(["/claimed/"])                 # 2 excluded
    for p in list((root / "lairs").iterdir())[:6]:           # 6 of 10 genuinely missing
        p.unlink()
    with pytest.raises(RuntimeError, match="MISSING"):
        store.prune_missing(source_roots=[root])


def test_small_mixed_prune_removes_both_kinds(tmp_path):
    root, store = _brain(tmp_path, n_mail=2, n_live=8)
    index.set_exclude_markers(["/claimed/"])
    (root / "lairs" / "live-0.md").unlink()
    gone = store.prune_missing(source_roots=[root])
    assert len(gone) == 3 and _count(store) == 7
