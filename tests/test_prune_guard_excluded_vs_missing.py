"""Fortify step 14 (2026-10-16): the prune guards separate MISSING files from policy-EXCLUDED docs.

Live failure: delisting /claimed/ from the CRO brain removed 4,447 of 8,433 docs (53%). Every one was on disk
and excluded by the operator's own config, yet `import --prune` refused it as "probably unmounted", and
`epoch build` could not publish the delist. CSO review of PR #78 (0854Z) added two requirements:
  D1  missing files are measured against the docs that STAY, so a large delist cannot hide a mount-gone
      event in the kept corpus (CSO probe P1);
  (a) a delist over the threshold needs its own --confirm-delist, distinct from --force-prune, so a mis-set
      marker such as "/" cannot silently wipe a brain (CSO probe P2).
Every test asserts BEHAVIOUR (doc counts, the removed set), never message wording (CSO D2).
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


def _refused(store, root, **kw):
    """True iff prune raises AND leaves the index untouched (behaviour, not wording)."""
    before = _count(store)
    try:
        store.prune_missing(source_roots=[root], **kw)
    except RuntimeError:
        return _count(store) == before
    return False


def test_intended_delist_over_half_publishes_only_with_confirm(tmp_path):
    root, store = _brain(tmp_path)                       # 6 of 10 docs under /claimed/
    index.set_exclude_markers(["/claimed/"])
    assert _refused(store, root), "a >50% delist without --confirm-delist must refuse and change nothing"
    gone = store.prune_missing(source_roots=[root], confirm_delist=True)
    assert sorted(gone) == sorted(f"claimed/mail-{i}.md" for i in range(6)) and _count(store) == 4
    # the old guard refused this even when confirmed: this is the behaviour the PR exists for


def test_small_delist_needs_no_confirm(tmp_path):
    root, store = _brain(tmp_path, n_mail=2, n_live=8)   # 2 of 10 excluded: under the threshold
    index.set_exclude_markers(["/claimed/"])
    gone = store.prune_missing(source_roots=[root])
    assert len(gone) == 2 and _count(store) == 8


def test_missing_majority_still_refused_and_index_untouched(tmp_path):
    root, store = _brain(tmp_path)
    for p in (root / "claimed").iterdir():               # the unmounted shape: files vanish
        p.unlink()
    assert _refused(store, root)
    assert _refused(store, root, confirm_delist=True), "--confirm-delist must not override the missing guard"


def test_force_still_overrides_the_missing_guard(tmp_path):
    root, store = _brain(tmp_path)
    for p in (root / "claimed").iterdir():
        p.unlink()
    gone = store.prune_missing(source_roots=[root], force=True)
    assert len(gone) == 6 and _count(store) == 4


def test_P1_confirmed_delist_cannot_hide_a_missing_majority_of_the_kept_docs(tmp_path):
    """CSO P1 (denominator): 6 delisted, 3 of the 4 KEPT docs vanish. missing/rows = 30% but missing/kept =
    75%: the unmounted shape. Must refuse even with --confirm-delist, and remove nothing."""
    root, store = _brain(tmp_path)
    index.set_exclude_markers(["/claimed/"])
    for p in list((root / "lairs").iterdir())[:3]:
        p.unlink()
    assert _refused(store, root, confirm_delist=True)


def test_excluded_docs_do_not_mask_a_missing_majority_numerator_side(tmp_path):
    """Partner of P1: few excluded, a real missing majority among the rest."""
    root, store = _brain(tmp_path, n_mail=2, n_live=8)
    index.set_exclude_markers(["/claimed/"])
    for p in list((root / "lairs").iterdir())[:6]:
        p.unlink()
    assert _refused(store, root)


def test_P2_mis_set_root_marker_refuses_without_confirm(tmp_path):
    """CSO P2: marker "/" excludes everything. Without --confirm-delist nothing may be removed."""
    root, store = _brain(tmp_path)
    index.set_exclude_markers(["/"])
    assert _refused(store, root)
    assert _count(store) == 10


def test_epoch_stage_error_surfaces_the_failure_line_past_a_wall_of_warnings(tmp_path):
    """The staging error used to keep only the last 2,000 chars; 3 kB of warnings hid the refusal."""
    from lbrain.epoch import EpochError
    from lbrain.epoch_build import _run_cli
    fake = tmp_path / "fake-lbrain"
    fake.write_text("#!/bin/sh\necho '✗ prune would remove 9/10 docs — refusing.'\n"
                    "i=0; while [ $i -lt 60 ]; do echo '[lbrain] WARNING: frontmatter parse failed for some/long/path.md' >&2; "
                    "i=$((i+1)); done\nexit 1\n", encoding="utf-8")
    fake.chmod(0o755)
    with pytest.raises(EpochError) as e:
        _run_cli(["import", "--prune"], tmp_path, str(fake))
    assert "✗ prune would remove 9/10 docs" in str(e.value)
