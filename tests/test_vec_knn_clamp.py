"""sqlite-vec refuses a KNN `k` above 4096. search() over-fetches k*8 vector candidates (k*4, doubled
while the abstraction cap is on), so any k above 512 used to raise
`sqlite3.OperationalError: k value in knn query too large` and the whole query failed.
Found in LME-1 stage 1 (2026-10-25): the A3 harness doubles -k to 640 when a brain has not yet
filled its context budget, and that one question failed every time.

These tests pin both halves of the fix: a large k no longer raises, and the clamp changes nothing
for any k whose candidate pool already fit (the study's other rows depend on that).
"""
from __future__ import annotations

import sqlite3

from lbrain import search as search_mod
from lbrain.search import VEC_KNN_MAX, search

from test_ranking import _brain


DOCS = {f"notes/n{i:03d}.md": f"# note {i}\n\nrollback flag alpha item {i} deploy window {i % 7}\n"
        for i in range(60)}


def test_k_above_512_does_not_raise(tmp_path):
    cfg, store, emb = _brain(tmp_path, DOCS)
    hits = search(cfg, store, emb, "rollback flag alpha", k=640)
    assert len(hits) > 0


def test_negative_control_sqlite_vec_still_refuses_k_above_the_limit(tmp_path):
    # Without the clamp the engine passes k*8 straight to vec0; prove the limit is real on this
    # build so the test above is not an accidental pass.
    cfg, store, emb = _brain(tmp_path, DOCS)
    q = emb.embed_one("rollback flag alpha")
    try:
        store.db.execute("SELECT v.rowid FROM vec_chunks v WHERE v.embedding MATCH ? AND k = ?",
                         (q, VEC_KNN_MAX + 1)).fetchall()
    except sqlite3.OperationalError as e:
        assert "knn" in str(e).lower() or "too large" in str(e).lower()
    else:
        raise AssertionError("sqlite-vec accepted k above VEC_KNN_MAX; the clamp constant is wrong")


def test_clamp_is_a_no_op_when_the_pool_fits(tmp_path, monkeypatch):
    # Same query, same brain: results with the clamp at its real value and with the clamp lifted out
    # of reach must be identical for every k whose pool (k*8) is within the limit.
    cfg, store, emb = _brain(tmp_path, DOCS)
    for k in (20, 80, 320, 512):
        a = [(h.rel_path, h.chunk_idx, round(h.score, 9)) for h in search(cfg, store, emb, "rollback flag alpha", k=k)]
        monkeypatch.setattr(search_mod, "VEC_KNN_MAX", 10**9)
        b = [(h.rel_path, h.chunk_idx, round(h.score, 9)) for h in search(cfg, store, emb, "rollback flag alpha", k=k)]
        monkeypatch.setattr(search_mod, "VEC_KNN_MAX", 4096)
        assert a == b, k
