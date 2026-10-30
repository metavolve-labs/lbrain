"""Path canonicalisation at import (2026-10-30, Prism/workhorse report).

Her repo path `~/golden_codex_pipeline` is a symlink to `~/golden`; two roots were listed
under the symlink spelling while the A-563 baseline added the real spelling. The engine keyed
docs by the path as written, so 121 documents were indexed twice (one row per spelling, same
doc_hash), the top-3 of a doctrine query showed the same record twice, and
`epoch build --prune-unreachable` removed nothing: the reachability guard resolves the real
path (so the alias row is reachable) while import had recorded the alias as its own doc.
Second symptom: the publish line said `docs: 871` while the published docs table held 992
rows, because the publish figure summed a byte-prefix inventory and the alias rows fell
outside every configured prefix.

Prior design this builds on, not against: store._norm_path (realpath + normcase, CSO/mac
review of PR #57, 2026-09-07) already governs reachability; A-626 names real source roots in
manifests; the 2026-09-22 anomaly ("canonicalise source roots before keying docs") and the
Keeper self-analysis of 2026-10-15 (A3: "canonicalise paths at ingest") asked for exactly this.

Identity is by realpath (inode-equivalent), never by spelling. Content is NOT identity: two
different files with identical bodies stay two docs (the negative control below).
"""
from __future__ import annotations

import importlib
import json
import os
import sqlite3
from pathlib import Path

import pytest
from click.testing import CliRunner

from lbrain.cli import main
from lbrain.config import Config
from lbrain.store import Store

pytestmark = pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlinks unavailable on this platform")

BODY = "# Doctrine\n\nVerify live state before asserting it. Three claims, three checks.\n"


def _write_cfg(home: Path, sources: list[str]) -> None:
    lines = ['embedding_provider = "local"', "sources = ["] + [f'  "{s}",' for s in sources] + ["]"]
    (home / "config.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _home(tmp_path, monkeypatch, sources: list[str]) -> Path:
    home = tmp_path / "h"
    home.mkdir()
    _write_cfg(home, sources)
    monkeypatch.setenv("LBRAIN_HOME", str(home))
    import lbrain.config
    importlib.reload(lbrain.config)
    return home


def _import(*args):
    res = CliRunner().invoke(main, ["import", *args])
    assert res.exit_code == 0, res.output
    return res.output


def _store() -> Store:
    cfg = Config.load()
    return Store(cfg.db_path, embedding_dim=cfg.embedding_dim)


def _rows():
    s = _store()
    try:
        return [dict(r) for r in s.db.execute(
            "SELECT rel_path, abs_path, doc_hash, metadata FROM docs ORDER BY rel_path")]
    finally:
        s.close()


def _dup_hashes():
    s = _store()
    try:
        return [r[0] for r in s.db.execute(
            "SELECT doc_hash FROM docs GROUP BY doc_hash HAVING COUNT(*) > 1")]
    finally:
        s.close()


def _fixture(tmp_path):
    """real/lairs/doctrine.md reached three ways: the real root, a symlinked spelling of the
    same root (Prism's `golden_codex_pipeline -> golden`), and a wider root that contains it."""
    real = tmp_path / "golden"
    (real / "lairs").mkdir(parents=True)
    (real / "lairs" / "doctrine.md").write_text(BODY, encoding="utf-8")
    (real / "lairs" / "other.md").write_text("# Other\n\nA second, unrelated record.\n", encoding="utf-8")
    link = tmp_path / "golden_codex_pipeline"
    os.symlink(real, link, target_is_directory=True)
    assert os.path.realpath(link / "lairs" / "doctrine.md") == str(real / "lairs" / "doctrine.md")
    return real, link


# ---------- the report, end to end ----------

def test_symlinked_and_overlapping_roots_yield_one_doc(tmp_path, monkeypatch):
    real, link = _fixture(tmp_path)
    # the symlink spelling FIRST (as in Prism's config), then the real spelling (the A-563 baseline),
    # then a wider root that contains both
    _home(tmp_path, monkeypatch, [str(link / "lairs"), str(real / "lairs"), str(real)])
    out = _import()

    rows = _rows()
    assert len(rows) == 2, rows                      # doctrine + other, once each
    assert _dup_hashes() == [], _rows()              # zero duplicate doc_hash values
    doc = next(r for r in rows if r["rel_path"].endswith("doctrine.md"))
    # one rel_path, under the FIRST configured root that reaches the file
    assert doc["rel_path"] == "doctrine.md", doc
    # abs_path is the realpath, whatever spelling the root used
    assert doc["abs_path"] == str(real / "lairs" / "doctrine.md"), doc
    # nothing lost: the alias spellings are recorded in metadata
    aliases = json.loads(doc["metadata"]).get("lbrain_path_aliases")
    # the real spelling, as offered by root 2 and root 3 (one entry: the alias set is a set)
    assert aliases == [str(real / "lairs" / "doctrine.md")], aliases
    assert "aliases" in out, out                     # the summary says it happened


def test_alias_visit_is_counted_once_in_the_summary(tmp_path, monkeypatch):
    real, link = _fixture(tmp_path)
    _home(tmp_path, monkeypatch, [str(link / "lairs"), str(real / "lairs")])
    out = _import()
    assert "new: 2," in out, out                     # not 4: the second root's visits are aliases
    out2 = _import()
    assert "unchanged: 2," in out2 and "new: 0," in out2, out2


def test_prune_unreachable_leaves_the_canonical_doc(tmp_path, monkeypatch):
    real, link = _fixture(tmp_path)
    _home(tmp_path, monkeypatch, [str(link / "lairs"), str(real / "lairs")])
    _import("--prune-unreachable")
    assert len(_rows()) == 2
    s = _store()
    try:
        would = s.prune_unreachable(source_roots=[str(link / "lairs"), str(real / "lairs")], dry_run=True)
        assert would == [], would
        # and the reachable set the guard sees IS the docs table: every row is under a root
        from lbrain.store import _norm_path, _under_roots
        roots = [_norm_path(r) for r in (str(link / "lairs"), str(real / "lairs"))]
        assert all(_under_roots(r["abs_path"], roots) for r in s.db.execute("SELECT abs_path FROM docs"))
    finally:
        s.close()


def test_historical_alias_twins_collapse_on_one_import(tmp_path, monkeypatch):
    """The brain Prism has today: rows under both spellings, same hash, different rel_path. One import
    after this change canonicalises abs_path and DD-01 collapses the twin; no data is lost (the file
    is on disk and the kept row is the one the scan refreshes)."""
    real, link = _fixture(tmp_path)
    _home(tmp_path, monkeypatch, [str(link / "lairs"), str(real / "lairs")])
    _import()
    s = _store()
    try:
        # plant the alias twin the old engine wrote: rel_path disambiguated by src_name, abs_path spelled
        # through the symlink (the row the real-spelling root could never match as a string)
        s.db.execute(
            "INSERT INTO docs (rel_path, abs_path, title, doc_hash, mtime) VALUES (?, ?, ?, ?, ?)",
            ("lairs/doctrine.md", str(link / "lairs" / "doctrine.md"), "Doctrine",
             s.get_doc_hash("doctrine.md"), 0.0))
        s.db.commit()
        assert len(_dup_hashes()) == 1
    finally:
        s.close()
    out = _import()
    assert "identity-dupes collapsed: 1" in out, out
    assert _dup_hashes() == [], _rows()
    assert [r["rel_path"] for r in _rows()] == ["doctrine.md", "other.md"], _rows()


def test_alias_record_is_dropped_when_the_alias_root_goes_away(tmp_path, monkeypatch):
    real, link = _fixture(tmp_path)
    home = _home(tmp_path, monkeypatch, [str(link / "lairs"), str(real / "lairs")])
    _import()
    assert json.loads(next(r for r in _rows() if r["rel_path"] == "doctrine.md")["metadata"]).get("lbrain_path_aliases")
    _write_cfg(home, [str(link / "lairs")])
    _import()
    meta = json.loads(next(r for r in _rows() if r["rel_path"] == "doctrine.md")["metadata"])
    assert "lbrain_path_aliases" not in meta, meta
    assert "meta-refreshed" not in _import(), "a stable alias set must not read as a frontmatter change"


# ---------- negative control: content is not identity ----------

def test_two_different_files_with_identical_content_stay_two_docs(tmp_path, monkeypatch):
    a = tmp_path / "root-a"; a.mkdir()
    b = tmp_path / "root-b"; b.mkdir()
    (a / "same.md").write_text(BODY, encoding="utf-8")
    (b / "same.md").write_text(BODY, encoding="utf-8")
    _home(tmp_path, monkeypatch, [str(a), str(b)])
    _import()
    rows = _rows()
    assert len(rows) == 2, rows
    assert len({r["doc_hash"] for r in rows}) == 1              # same hash ...
    assert len({r["abs_path"] for r in rows}) == 2              # ... different realpath => two docs
    assert len(_dup_hashes()) == 1                                # the duplicate hash is legitimate here
    assert all("lbrain_path_aliases" not in json.loads(r["metadata"]) for r in rows), rows


# ---------- publish count equals the table count ----------

def test_publish_count_equals_docs_table_count_with_symlinked_roots(tmp_path, monkeypatch):
    from lbrain import epoch, epoch_build
    from _coldembed import DIM, seed_brain

    real, link = _fixture(tmp_path)
    home = tmp_path / "home"; home.mkdir()
    srcs = [str(link / "lairs"), str(real / "lairs")]
    (home / "config.toml").write_text(
        'embedding_provider = "local"\nembedding_model = "BAAI/bge-small-en-v1.5"\n'
        f'embedding_dim = {DIM}\ndb_path = "{home / "brain.db"}"\n'
        "sources = [\n" + "".join(f'  "{s}",\n' for s in srcs) + "]\n", encoding="utf-8")
    seed_brain(home, DIM)
    # plant a legacy alias twin in the legacy brain so the delta build inherits it
    con = sqlite3.connect(str(home / "brain.db"))
    try:
        h = con.execute("SELECT doc_hash FROM docs WHERE rel_path='doctrine.md'").fetchone()[0]
        con.execute("INSERT INTO docs (rel_path, abs_path, title, doc_hash, mtime) VALUES (?, ?, ?, ?, ?)",
                    ("lairs/doctrine.md", str(link / "lairs" / "doctrine.md"), "Doctrine", h, 0.0))
        con.commit()
        assert con.execute("SELECT COUNT(*) FROM docs").fetchone()[0] == 3
    finally:
        con.close()

    from types import SimpleNamespace
    cfg = SimpleNamespace(sources=srcs, embedding_dim=DIM, db_path=home / "brain.db")
    report = epoch_build.build(home, cfg, prune_unreachable=True)
    assert report["published"]
    db = epoch.resolve_db_path(home, home / "brain.db")
    con = sqlite3.connect(str(db))
    try:
        table = con.execute("SELECT COUNT(*) FROM docs").fetchone()[0]
        dups = con.execute("SELECT COUNT(*) FROM (SELECT doc_hash FROM docs GROUP BY doc_hash HAVING COUNT(*) > 1)").fetchone()[0]
    finally:
        con.close()
    assert report["docs"] == table == 2, (report["docs"], table)
    assert dups == 0
    manifest = json.loads((db.parent / "build-manifest.json").read_text())
    assert manifest["docs"] == table
    assert manifest["docs_in_sources"] == table
