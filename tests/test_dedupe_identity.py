"""DD-01 (2026-08-31) — historical duplicate-abs_path doc rows collapse on import.

Pre-MS-01, `lbrain import <subdir>` keyed a file under a short rel_path while a
root import keyed the SAME file under its full rel_path: two doc rows, one file
(measured on the main brain: 196 groups). The stale twin is unreachable by any
root scan, never pruned (its file exists), and keeps serving stale chunks plus
pre-SUP-15 supersession rows no re-mint can reach. MS-01 stops NEW twins;
dedupe_identity retires the historical ones.

Twins are fabricated by direct SQL — the modern importer can no longer create
them, which is the point.
"""
from __future__ import annotations

import importlib

from click.testing import CliRunner

from lbrain.cli import main
from lbrain.config import Config
from lbrain.store import Store


def _setup(tmp_path, monkeypatch):
    src = tmp_path / "root"
    (src / "sub").mkdir(parents=True)
    (src / "sub" / "note.md").write_text("# Note\n\nlive body\n", encoding="utf-8")
    (src / "other.md").write_text("# Other\n\nbody\n", encoding="utf-8")
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


def _store():
    cfg = Config.load()
    return Store(cfg.db_path, embedding_dim=cfg.embedding_dim)


def _plant_twin(store, rel, abs_path, sup_tgt="stale-target"):
    store.db.execute(
        "INSERT INTO docs (rel_path, abs_path, title, doc_hash, mtime, is_priority,"
        " doc_type, metadata, disclosure, evidence, claim_date)"
        " VALUES (?, ?, 'Stale twin', 'deadbeef', 0, 0, '', '{}', '', '', '')",
        (rel, abs_path))
    store.db.execute(
        "INSERT INTO supersessions (src_path, tgt_slug) VALUES (?, ?)", (rel, sup_tgt))
    store.db.commit()


def test_historical_twin_collapses_on_import(tmp_path, monkeypatch):
    src, home = _setup(tmp_path, monkeypatch)
    s = _store()
    _plant_twin(s, "note.md", str(src / "sub" / "note.md"))
    s.close()
    res = CliRunner().invoke(main, ["import"])
    assert res.exit_code == 0, res.output
    assert "identity-dupes collapsed: 1" in res.output, res.output
    s = _store()
    rows = sorted(r["rel_path"] for r in s.db.execute("SELECT rel_path FROM docs"))
    assert rows == ["other.md", "sub/note.md"], rows
    sups = list(s.db.execute("SELECT * FROM supersessions"))
    assert sups == [], [dict(r) for r in sups]   # the twin's stale row went with it
    s.close()


def test_group_outside_scanned_roots_is_left_alone(tmp_path, monkeypatch):
    src, home = _setup(tmp_path, monkeypatch)
    outside = tmp_path / "elsewhere.md"
    outside.write_text("# Elsewhere\n", encoding="utf-8")
    s = _store()
    _plant_twin(s, "elsewhere.md", str(outside), sup_tgt="t1")
    _plant_twin(s, "old/elsewhere.md", str(outside), sup_tgt="t2")
    s.close()
    res = CliRunner().invoke(main, ["import"])
    assert res.exit_code == 0, res.output
    assert "identity-dupes collapsed" not in res.output, res.output
    s = _store()
    n = s.db.execute(
        "SELECT COUNT(*) c FROM docs WHERE abs_path = ?", (str(outside),)).fetchone()["c"]
    assert n == 2, "un-scanned group must not be guessed at"
    s.close()


def test_resolve_prefers_exact_key_over_stale_twin(tmp_path, monkeypatch):
    """The scan must refresh the CANONICAL row, not latch onto the twin."""
    src, home = _setup(tmp_path, monkeypatch)
    s = _store()
    _plant_twin(s, "aaa-note.md", str(src / "sub" / "note.md"))  # sorts BEFORE sub/
    eff, h = s.resolve_rel_path("sub/note.md", str(src / "sub" / "note.md"), "root")
    assert eff == "sub/note.md", eff
    assert h != "deadbeef"
    s.close()


def test_healthy_brain_is_a_noop(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    res = CliRunner().invoke(main, ["import"])
    assert res.exit_code == 0, res.output
    assert "identity-dupes" not in res.output, res.output
