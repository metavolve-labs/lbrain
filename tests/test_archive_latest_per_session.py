"""Archive recall serves the NEWEST snapshot of each session (CSO minimal-brain design v0, engine ask 1, 2026-10-17).

CSO 2026-10-16T2308Z: 104 archives on her home were 8 sessions captured whole again and again; deep recall
returned 3 snapshots of one session at the same distance. Non-destructive: superseded rows stay in the table and
stay retrievable by txid; recall stops serving them. Behaviour only: which txids come back.
"""
from __future__ import annotations

import sqlite3
import struct

from lbrain import epoch_build
from lbrain.archive.storage import ArchiveStore

DIM = 8


def _store(tmp_path):
    con = epoch_build._connect_vec(tmp_path / "a.db")
    con.row_factory = sqlite3.Row
    return ArchiveStore(con, embedding_dim=DIM)


def _vec(*xs):
    v = list(xs) + [0.0] * (DIM - len(xs))
    return struct.pack(f"{DIM}f", *v)


def _add(st, txid, title, n_bytes, created, vec, namespace="private"):
    st.insert_archive(txid=txid, namespace=namespace, title=title, snapshot=f"snap {txid}", tags={},
                      n_bytes=n_bytes, created=created, transport="local")
    st.write_archive_embedding(txid, vec)


def _txids(rows):
    return [r["txid"] for r in rows]


def test_one_session_captured_three_times_is_one_result_the_newest(tmp_path):
    st = _store(tmp_path)
    _add(st, "t-small", "sess-A", 100, 1.0, _vec(1, 0))
    _add(st, "t-mid", "sess-A", 200, 2.0, _vec(1, 0.01))
    _add(st, "t-big", "sess-A", 300, 3.0, _vec(1, 0.02))
    got = _txids(st.search_archives(_vec(1, 0), k=5))
    assert got == ["t-big"]


def test_distinct_sessions_are_not_merged(tmp_path):
    st = _store(tmp_path)
    _add(st, "a1", "sess-A", 100, 1.0, _vec(1, 0))
    _add(st, "b1", "sess-B", 50, 1.0, _vec(1, 0.1))
    assert sorted(_txids(st.search_archives(_vec(1, 0), k=5))) == ["a1", "b1"]


def test_shredded_newest_falls_back_to_the_next_newest(tmp_path):
    st = _store(tmp_path)
    _add(st, "old", "sess-A", 100, 1.0, _vec(1, 0))
    _add(st, "new", "sess-A", 200, 2.0, _vec(1, 0))
    st.mark_archive_shredded("new", purge_snapshot=False)
    assert _txids(st.search_archives(_vec(1, 0), k=5)) == ["old"]


def test_namespaces_are_separate_sessions(tmp_path):
    st = _store(tmp_path)
    _add(st, "p", "sess-A", 100, 1.0, _vec(1, 0), namespace="private")
    _add(st, "q", "sess-A", 200, 1.0, _vec(1, 0), namespace="shared")
    assert sorted(_txids(st.search_archives(_vec(1, 0), k=5))) == ["p", "q"]


def test_superseded_rows_stay_retrievable_and_opt_in_shows_them(tmp_path):
    st = _store(tmp_path)
    _add(st, "old", "sess-A", 100, 1.0, _vec(1, 0))
    _add(st, "new", "sess-A", 200, 2.0, _vec(1, 0))
    assert st.get_archive("old") is not None                                 # non-destructive
    assert sorted(_txids(st.search_archives(_vec(1, 0), k=5, include_superseded=True))) == ["new", "old"]


def test_superseded_snapshots_do_not_starve_k(tmp_path):
    """Twenty snapshots of one session nearest the query (more than the 15-row candidate pool) must not push a
    distinct session out of k=2."""
    st = _store(tmp_path)
    for i in range(20):
        _add(st, f"a{i}", "sess-A", 100 + i, float(i), _vec(1, 0.001 * i))
    _add(st, "b", "sess-B", 10, 1.0, _vec(1, 0.5))
    assert sorted(_txids(st.search_archives(_vec(1, 0), k=2))) == ["a19", "b"]
