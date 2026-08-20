"""Issue #34 — `doctor` reported clean on a stale index.

Every check `doctor` performed compared the index to the CONFIG: stored vectors
vs live embedding settings, chunker fingerprint, inert keys. None of them could
see that the FILES had changed. So after a corpus regeneration — four records
superseded or deleted — `doctor` still said clean, which is the answer a user
reads as *my brain is current*.

These tests hold two lines. The first is the obvious one: a changed corpus must
be reported. The second matters more and is easier to lose in a refactor — the
survey must never report "current" for a corpus it did not actually look at.
A green light that means "I found no problems in the half I read" is the same
defect wearing the fix's clothes.
"""
from __future__ import annotations

import importlib

from click.testing import CliRunner

from lbrain import index_currency
from lbrain.cli import main
from lbrain.config import Config
from lbrain.store import Store

DOC = "---\nname: Alpha\ntype: decision\n---\n\n# Alpha\n\nThe body of alpha.\n"
DOC_B = "---\nname: Beta\n---\n\n# Beta\n\nThe body of beta.\n"


def _setup(tmp_path, monkeypatch):
    """A real corpus, imported through the real pipeline, sources in config."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.md").write_text(DOC, encoding="utf-8")
    (src / "b.md").write_text(DOC_B, encoding="utf-8")

    home = tmp_path / "h"
    home.mkdir()
    (home / "config.toml").write_text(
        f'embedding_provider = "local"\nsources = ["{src}"]\n', encoding="utf-8")
    monkeypatch.setenv("LBRAIN_HOME", str(home))
    import lbrain.config
    importlib.reload(lbrain.config)

    res = CliRunner().invoke(main, ["import"])
    assert res.exit_code == 0, res.output
    return src, home


def _survey(home):
    cfg = Config.load()
    store = Store(cfg.db_path, embedding_dim=cfg.embedding_dim)
    try:
        return index_currency.survey(store, cfg.sources)
    finally:
        store.close()


def _doctor():
    return CliRunner().invoke(main, ["doctor"])


# --- the index agrees with its sources -------------------------------------

def test_freshly_imported_index_is_current(tmp_path, monkeypatch):
    src, home = _setup(tmp_path, monkeypatch)
    s = _survey(home)
    assert s.is_current, s.as_dict()
    assert s.on_disk == 2 and s.indexed == 2 and s.current == 2
    assert s.divergent == 0


def test_touching_a_file_without_editing_it_is_not_a_divergence(tmp_path, monkeypatch):
    """mtime is not the question being asked.

    An archiver, a checkout, or a `touch` moves mtime without changing content.
    Reporting those as stale would train an operator to ignore the warning, so
    the survey hashes rather than trusting the timestamp — in both directions.
    """
    src, home = _setup(tmp_path, monkeypatch)
    p = src / "a.md"
    p.write_text(p.read_text(encoding="utf-8"), encoding="utf-8")  # new mtime, same bytes
    assert _survey(home).is_current


# --- each way a corpus can move away from its index ------------------------

def test_edited_body_is_CHANGED(tmp_path, monkeypatch):
    src, home = _setup(tmp_path, monkeypatch)
    (src / "a.md").write_text(DOC + "\nA new paragraph nobody indexed.\n", encoding="utf-8")
    s = _survey(home)
    assert s.changed == ["a.md"], s.as_dict()
    assert not s.is_current


def test_edited_frontmatter_alone_is_METADATA(tmp_path, monkeypatch):
    """The A-401 blind spot, surfaced rather than repeated.

    `doc_hash` covers the body only, so a `type:` or `verify_by:` edit changes no
    hash. A currency check built on the body hash alone would report clean while
    the doc_type filter and the staleness tier both read stale values.
    """
    src, home = _setup(tmp_path, monkeypatch)
    (src / "a.md").write_text(
        DOC.replace("type: decision", "type: reference"), encoding="utf-8")
    s = _survey(home)
    assert s.metadata == ["a.md"], s.as_dict()
    assert s.changed == []
    assert not s.is_current


def test_new_file_is_UNINDEXED(tmp_path, monkeypatch):
    src, home = _setup(tmp_path, monkeypatch)
    (src / "c.md").write_text("# Gamma\n\nNever imported.\n", encoding="utf-8")
    s = _survey(home)
    assert s.unindexed == ["c.md"], s.as_dict()
    assert not s.is_current


def test_deleted_file_is_ORPHANED(tmp_path, monkeypatch):
    src, home = _setup(tmp_path, monkeypatch)
    (src / "b.md").unlink()
    s = _survey(home)
    assert s.orphaned == ["b.md"], s.as_dict()
    assert not s.is_current


def test_doc_indexed_before_the_backup_exclusion_is_ORPHANED(tmp_path, monkeypatch):
    """Matches `prune_missing` exactly: not-indexable is not the same as not-there.

    The discriminating case is narrow and easy to miss. Moving a file INTO a
    backup tree also removes it from its indexed path, so an existence-only
    check catches it by luck and the exclusion clause is never exercised — the
    first version of this test passed with `is_backup_path` deleted. The case
    that actually needs the clause is a brain built by a version that did not
    exclude backup trees: the row points at a file that is still exactly where
    the index says it is, and `discover()` no longer returns it. Verified live
    in prune 2026-07-28 — "the exclusion shipped without this and changed
    nothing a user would see."
    """
    src, home = _setup(tmp_path, monkeypatch)
    backup = src / "backups-pre-apply"
    backup.mkdir()
    stale = backup / "old.md"
    stale.write_text("# Old\n\nIndexed by a build with no exclusion.\n", encoding="utf-8")

    from lbrain.index import parse
    cfg = Config.load()
    store = Store(cfg.db_path, embedding_dim=cfg.embedding_dim)
    with store.transaction():
        store.upsert_doc(parse(stale, repo_root=src))
    store.close()

    s = _survey(home)
    assert stale.exists()  # the row is not orphaned by absence
    assert s.orphaned == ["backups-pre-apply/old.md"], s.as_dict()
    assert s.unreachable == []


def test_doc_outside_every_configured_source_is_UNREACHABLE_not_orphaned(tmp_path, monkeypatch):
    """Indexed, still on disk, and nothing will ever maintain it.

    `import` cannot refresh it (discover never reaches it) and `--prune` will
    never remove it (the file exists). Calling it ORPHANED would send the user
    looking for a missing file that is right there; saying nothing would leave a
    served record permanently frozen with no way to notice.
    """
    src, home = _setup(tmp_path, monkeypatch)
    other = tmp_path / "other"
    other.mkdir()
    (other / "z.md").write_text("# Zeta\n\nElsewhere.\n", encoding="utf-8")
    CliRunner().invoke(main, ["import", str(other)])  # indexed, not in cfg.sources

    s = _survey(home)
    assert s.unreachable == ["z.md"], s.as_dict()
    assert s.orphaned == []


# --- "I did not look" must never read as "nothing is wrong" ----------------

def test_missing_source_root_does_not_report_the_whole_corpus_orphaned(tmp_path, monkeypatch):
    """The unmounted-drive false alarm `prune_missing` already refuses to make.

    Every file under an unmounted root is absent, so a naive existence check
    reports the entire corpus ORPHANED. An alarm that loud, that wrong, is how
    an operator learns to ignore the real one.
    """
    src, home = _setup(tmp_path, monkeypatch)
    for p in src.iterdir():
        p.unlink()
    src.rmdir()  # the whole root is gone, as if unmounted

    s = _survey(home)
    assert s.orphaned == [], s.as_dict()
    assert s.roots_missing and str(src) in s.roots_missing[0]
    assert s.unchecked == 2


def test_unchecked_docs_block_the_all_clear(tmp_path, monkeypatch):
    """divergent == 0 is not sufficient to claim current.

    This is the whole defect of issue #34 stated as an invariant: the survey
    found nothing wrong because it never looked. If `is_current` is ever
    simplified to "no divergence", this is the test that fails.
    """
    src, home = _setup(tmp_path, monkeypatch)
    for p in src.iterdir():
        p.unlink()
    src.rmdir()

    s = _survey(home)
    assert s.divergent == 0
    assert s.unchecked > 0
    assert not s.is_current


def test_no_sources_configured_reports_not_checked_not_current(tmp_path, monkeypatch):
    src, home = _setup(tmp_path, monkeypatch)
    (home / "config.toml").write_text('embedding_provider = "local"\n', encoding="utf-8")
    import lbrain.config
    importlib.reload(lbrain.config)

    s = _survey(home)
    assert s.ran is False
    assert s.is_current is False

    out = _doctor().output
    assert "NOT checked" in out
    assert "index is current" not in out


# --- what the operator actually sees ---------------------------------------

def test_doctor_no_longer_reports_clean_on_a_stale_index(tmp_path, monkeypatch):
    """The regression test for the issue as filed.

    The reporter's case: files superseded and deleted in one regeneration, and
    `doctor` said clean either way — supplying no evidence the import had even
    run. The output must now name the divergence and the fix.
    """
    src, home = _setup(tmp_path, monkeypatch)
    (src / "a.md").write_text(DOC + "\nedited\n", encoding="utf-8")
    (src / "b.md").unlink()

    out = _doctor().output
    assert "INDEX NOT CURRENT" in out
    assert "CHANGED" in out and "ORPHANED" in out
    assert "lbrain import" in out


def test_doctor_says_current_when_it_is(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    out = _doctor().output
    assert "index is current with its sources" in out
    assert "INDEX NOT CURRENT" not in out


def test_doctor_json_carries_the_survey(tmp_path, monkeypatch):
    import json as _json
    src, home = _setup(tmp_path, monkeypatch)
    (src / "a.md").write_text(DOC + "\nedited\n", encoding="utf-8")

    res = CliRunner().invoke(main, ["doctor", "--json"])
    payload = _json.loads(res.output)["index_currency"]
    assert payload["is_current"] is False
    assert payload["changed"] == ["a.md"]


def test_stale_index_does_not_change_the_exit_code(tmp_path, monkeypatch):
    """A corpus edited between imports is the NORMAL state of a working brain.

    `doctor` exits non-zero only when the stored vectors cannot be TRUSTED.
    Currency is the weaker claim — stale, not wrong, and `import` repairs it —
    so it gets the same treatment chunker drift already gets. Widening the
    contract here would start failing every script that gates on `doctor`, which
    is how a useful gate gets commented out. Scripts read `--json`.
    """
    src, home = _setup(tmp_path, monkeypatch)
    (src / "b.md").unlink()
    assert _doctor().exit_code == 0
