"""MS-01 (2026-08-31) — cross-source rel_path collision thrashed one row forever.

Three source plates each carry a root-level `_INDEX.md`. rel_path is computed
relative to the root that offered the file AND is the docs PRIMARY KEY, so all
three parsed to the same key: every `lbrain import` re-wrote the row, chunks and
vectors for two of them ("updated" forever), and `doctor` reported the losers
CHANGED no matter how many reconcile passes ran — the loop Artiswa hit on the
CCO brain. Row identity must be the FILE.

Each test asserts post-fix behaviour: RED before, GREEN after.
"""
from __future__ import annotations

import importlib

from click.testing import CliRunner

from lbrain import index_currency
from lbrain.cli import main
from lbrain.config import Config
from lbrain.store import Store

INDEX_A = "# Plate A index\n\nAlpha plate contents.\n"
INDEX_B = "# Plate B index\n\nBeta plate contents, different body.\n"


def _setup(tmp_path, monkeypatch):
    """Two source roots, each offering a root-level _INDEX.md plus one unique doc."""
    pa = tmp_path / "PLATE-A"
    pb = tmp_path / "PLATE-B"
    pa.mkdir(); pb.mkdir()
    (pa / "_INDEX.md").write_text(INDEX_A, encoding="utf-8")
    (pb / "_INDEX.md").write_text(INDEX_B, encoding="utf-8")
    (pa / "alpha.md").write_text("# Alpha\n\nbody a\n", encoding="utf-8")
    (pb / "beta.md").write_text("# Beta\n\nbody b\n", encoding="utf-8")

    home = tmp_path / "h"
    home.mkdir()
    (home / "config.toml").write_text(
        f'embedding_provider = "local"\nsources = ["{pa}", "{pb}"]\n', encoding="utf-8")
    monkeypatch.setenv("LBRAIN_HOME", str(home))
    import lbrain.config
    importlib.reload(lbrain.config)
    return pa, pb, home


def _import():
    res = CliRunner().invoke(main, ["import"])
    assert res.exit_code == 0, res.output
    return res.output


def _rows(home):
    cfg = Config.load()
    store = Store(cfg.db_path, embedding_dim=cfg.embedding_dim)
    try:
        return sorted(
            (r["rel_path"], r["abs_path"]) for r in
            store.db.execute("SELECT rel_path, abs_path FROM docs"))
    finally:
        store.close()


def _survey(home):
    cfg = Config.load()
    store = Store(cfg.db_path, embedding_dim=cfg.embedding_dim)
    try:
        return index_currency.survey(store, cfg.sources)
    finally:
        store.close()


def test_both_index_files_get_their_own_row(tmp_path, monkeypatch):
    pa, pb, home = _setup(tmp_path, monkeypatch)
    _import()
    rows = _rows(home)
    assert len(rows) == 4, rows
    abs_paths = {a for _, a in rows}
    assert str(pa / "_INDEX.md") in abs_paths
    assert str(pb / "_INDEX.md") in abs_paths
    rels = [r for r, _ in rows]
    assert len(set(rels)) == 4, f"rel_path collision survived: {rels}"


def test_second_import_converges_no_thrash(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    _import()
    out = _import()
    assert "updated: 0" in out, out
    assert "new: 0" in out, out
    assert "unchanged: 4" in out, out


def test_survey_is_current_after_import(tmp_path, monkeypatch):
    _, _, home = _setup(tmp_path, monkeypatch)
    _import()
    s = _survey(home)
    assert s.changed == [], s.changed
    assert s.unindexed == [], s.unindexed
    assert s.current == 4, (s.current, s.on_disk)


def test_row_identity_stable_across_reimports(tmp_path, monkeypatch):
    """The disambiguated key must not migrate between imports."""
    _, _, home = _setup(tmp_path, monkeypatch)
    _import()
    first = _rows(home)
    _import()
    assert _rows(home) == first


def test_edit_to_one_index_updates_only_its_row(tmp_path, monkeypatch):
    pa, _, home = _setup(tmp_path, monkeypatch)
    _import()
    (pa / "_INDEX.md").write_text(INDEX_A + "\nEdited.\n", encoding="utf-8")
    out = _import()
    assert "updated: 1" in out, out
    assert "unchanged: 3" in out, out


def test_takeover_when_colliding_file_is_gone(tmp_path, monkeypatch):
    """A key held by a DELETED file is taken over — moved-source behavior kept."""
    pa, pb, home = _setup(tmp_path, monkeypatch)
    _import()
    (pa / "_INDEX.md").unlink()
    # prune removes A's row; B keeps its (disambiguated or bare) row either way
    res = CliRunner().invoke(main, ["import", "--prune"])
    assert res.exit_code == 0, res.output
    rows = _rows(home)
    assert len(rows) == 3, rows
    assert str(pb / "_INDEX.md") in {a for _, a in rows}
