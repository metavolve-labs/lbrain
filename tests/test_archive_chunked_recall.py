"""Chunked archive vectors (CSO minimal-brain engine ask 2, 2026-10-17).

One vector per archive embeds only what a 512-token embedder reads of a snapshot: the OPENING of a multi-MB
session. A fact deep in the session could never be recalled. Each snapshot now also gets overlapping chunk vectors;
recall scores an archive by its best vector and previews the winning chunk. Synthetic vectors keep each test
decisive; behaviour only (which txid, which preview, which counts).
"""
from __future__ import annotations

import hashlib
import sqlite3
import struct

import pytest

from lbrain import epoch_build
from lbrain.archive.storage import ArchiveStore, chunk_snapshot, reindex_chunks
from lbrain.epoch import EpochError

DIM = 8


def _vec(*xs):
    v = list(xs) + [0.0] * (DIM - len(xs))
    return struct.pack(f"{DIM}f", *v)


def _store(path):
    con = epoch_build._connect_vec(path)
    con.row_factory = sqlite3.Row
    return ArchiveStore(con, embedding_dim=DIM)


def _add(st, txid, title, snapshot, n_bytes, vec):
    st.insert_archive(txid=txid, namespace="private", title=title, snapshot=snapshot, tags={},
                      n_bytes=n_bytes, created=1.0, transport="local")
    st.write_archive_embedding(txid, vec)


def test_a_fact_deep_in_a_session_is_recalled_through_its_chunk(tmp_path):
    st = _store(tmp_path / "a.db")
    opening = "session opening about mounting the seat\n" * 40
    deep = "the kraken liquidity recheck found the fee offset\n"
    _add(st, "deep", "sess-D", opening + deep, 10_000, _vec(1, 0))           # whole vector = the opening
    _add(st, "other", "sess-O", "unrelated notes\n", 100, _vec(0.6, 0.8))
    st.write_archive_chunks("deep", ["session opening about mounting the seat\n", deep], [_vec(1, 0), _vec(0, 1)])
    got = st.search_archives(_vec(0, 1), k=2)
    assert got[0]["txid"] == "deep" and got[0]["dist"] < 1e-3 and "kraken" in got[0]["snapshot"]


def test_without_chunks_the_same_fact_is_not_first(tmp_path):
    """The control for the test above: the whole-snapshot vector alone ranks the deep session behind another."""
    st = _store(tmp_path / "a.db")
    _add(st, "deep", "sess-D", "opening\n" * 40 + "kraken fee offset\n", 10_000, _vec(1, 0))
    _add(st, "other", "sess-O", "unrelated notes\n", 100, _vec(0.6, 0.8))
    assert st.search_archives(_vec(0, 1), k=2)[0]["txid"] == "other"


def test_recall_on_an_older_epoch_without_chunk_tables_opened_read_only(tmp_path):
    """Recall opens epoch dbs immutable; an epoch built before chunk tables existed must still serve, not try to
    create tables on a read-only database."""
    p = tmp_path / "old.db"
    st = _store(p)
    _add(st, "a", "sess-A", "text\n", 10, _vec(1, 0))
    st.db.close()
    ro = epoch_build._connect_vec(p, ro=True) if "ro" in epoch_build._connect_vec.__code__.co_varnames else None
    if ro is None:
        import sqlite_vec
        ro = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
        ro.enable_load_extension(True); sqlite_vec.load(ro); ro.enable_load_extension(False)
    ro.row_factory = sqlite3.Row
    st2 = ArchiveStore(ro, embedding_dim=DIM)      # exactly as the recall path constructs it (runs ensure_schema)
    assert not st2.has_chunk_tables()
    assert [r["txid"] for r in st2.search_archives(_vec(1, 0), k=3)] == ["a"]


def _fake_embed(texts):
    out = []
    for t in texts:
        h = hashlib.sha256(t.encode()).digest()
        out.append(struct.pack(f"{DIM}f", *[b / 255 for b in h[:DIM]]))
    return out


def test_reindex_chunks_served_and_drops_superseded(tmp_path):
    st = _store(tmp_path / "a.db")
    _add(st, "old", "sess-A", "short\n" * 600, 100, _vec(1, 0))
    _add(st, "new", "sess-A", "longer\n" * 900, 200, _vec(1, 0))
    _add(st, "solo", "sess-B", "solo\n" * 50, 50, _vec(0, 1))
    st.write_archive_chunks("old", ["stale chunk"], _fake_embed(["stale chunk"]))   # chunks of a now-superseded row
    rep = reindex_chunks(st, _fake_embed)
    ids = {r[0]: r[1] for r in st.db.execute("SELECT txid, archive_id FROM archives")}
    chunked = st.chunked_archive_ids()
    assert ids["new"] in chunked and ids["solo"] in chunked and ids["old"] not in chunked
    assert rep["dropped_archives"] == 1 and rep["chunked"] == 2
    assert reindex_chunks(st, _fake_embed)["chunked"] == 0                         # idempotent


def test_full_build_carry_keeps_the_chunks(tmp_path):
    prior = _store(tmp_path / "prior.db")
    _add(prior, "a", "sess-A", "x\n", 10, _vec(1, 0))
    prior.write_archive_chunks("a", ["c0", "c1"], [_vec(1, 0), _vec(0, 1)])
    prior.db.close()
    staging = tmp_path / "staging.db"
    epoch_build._connect_vec(staging).close()
    assert epoch_build._carry_archive_index(tmp_path / "prior.db", staging) == 1
    st = _store(staging)
    assert st.db.execute("SELECT count(*) FROM archive_chunks").fetchone()[0] == 2
    assert [r["txid"] for r in st.search_archives(_vec(0, 1), k=1)] == ["a"]


def test_carry_refuses_a_chunk_without_its_vector(tmp_path):
    prior = _store(tmp_path / "prior.db")
    _add(prior, "a", "sess-A", "x\n", 10, _vec(1, 0))
    prior.write_archive_chunks("a", ["c0"], [_vec(1, 0)])
    prior.db.execute("DELETE FROM vec_archive_chunks"); prior.db.commit(); prior.db.close()
    staging = tmp_path / "staging.db"
    epoch_build._connect_vec(staging).close()
    with pytest.raises(EpochError):
        epoch_build._carry_archive_index(tmp_path / "prior.db", staging)


def test_chunker_is_deterministic_overlapping_and_cuts_between_lines():
    text = "".join(f"line {i:04d} of the session transcript\n" for i in range(400))
    a, b = chunk_snapshot(text), chunk_snapshot(text)
    assert a == b and len(a) > 5
    assert all(len(c) <= 1800 for c in a)
    assert all(c.endswith("\n") or c is a[-1] for c in a[:-1])                     # cut at a newline
    assert a[1][:50] in a[0]                                                         # overlap carries context
    assert chunk_snapshot("") == [] and chunk_snapshot("short") == ["short"]


# ---- CSO PR #83 review (0330Z) ---------------------------------------------------------------------------------

def _chunk_counts(st, txid):
    aid = st.db.execute("SELECT archive_id FROM archives WHERE txid = ?", (txid,)).fetchone()[0]
    rows = st.db.execute("SELECT chunk_id FROM archive_chunks WHERE archive_id = ?", (aid,)).fetchall()
    vecs = sum(st.db.execute("SELECT count(*) FROM vec_archive_chunks WHERE rowid = ?", (r[0],)).fetchone()[0] for r in rows)
    return len(rows), vecs


@pytest.mark.parametrize("purge", [True, False])
def test_shred_removes_chunk_text_and_vectors(tmp_path, purge):
    """D1 (privacy): a HARD shred promises nothing readable survives locally; chunk text must go too. A soft shred
    is unsearchable like its whole-snapshot vector, so its chunks go as well."""
    st = _store(tmp_path / "a.db")
    _add(st, "a", "sess-A", "secret line one\nsecret line two\n", 10, _vec(1, 0))
    st.write_archive_chunks("a", ["secret line one\n", "secret line two\n"], [_vec(1, 0), _vec(0, 1)])
    st.mark_archive_shredded("a", purge_snapshot=purge)
    assert _chunk_counts(st, "a") == (0, 0)
    assert st.db.execute("SELECT count(*) FROM vec_archive_chunks").fetchone()[0] == 0   # no orphaned vector either (P1b)
    assert st.search_archives(_vec(0, 1), k=3) == []


def test_search_filters_a_shredded_archive_that_still_has_chunks(tmp_path):
    """The shredded filter in search is tested on its own: a legacy row (shredded flag set without its chunks being
    removed) must not be served through a chunk."""
    st = _store(tmp_path / "a.db")
    _add(st, "a", "sess-A", "x\n", 10, _vec(1, 0))
    st.write_archive_chunks("a", ["x\n"], [_vec(0, 1)])
    st.db.execute("UPDATE archives SET shredded = 1 WHERE txid = 'a'"); st.db.commit()
    assert st.search_archives(_vec(0, 1), k=3) == []


def test_carry_never_copies_a_shredded_archives_chunk_text(tmp_path):
    prior = _store(tmp_path / "prior.db")
    _add(prior, "live", "sess-L", "live\n", 10, _vec(1, 0))
    _add(prior, "gone", "sess-G", "gone\n", 10, _vec(0, 1))
    prior.write_archive_chunks("live", ["live chunk"], [_vec(1, 0)])
    prior.write_archive_chunks("gone", ["SHREDDED CLEARTEXT"], [_vec(0, 1)])
    prior.db.execute("UPDATE archives SET shredded = 1 WHERE txid = 'gone'"); prior.db.commit(); prior.db.close()
    staging = tmp_path / "staging.db"
    epoch_build._connect_vec(staging).close()
    epoch_build._carry_archive_index(tmp_path / "prior.db", staging)
    st = _store(staging)
    texts = [r[0] for r in st.db.execute("SELECT text FROM archive_chunks")]
    assert texts == ["live chunk"]


def test_every_chunk_after_the_first_starts_on_a_line():
    """D2: a chunk starting mid-word previews badly ('el in `a545…'); the next chunk starts on a line boundary."""
    text = "".join(f"line {i:04d} with a few words of session text here\n" for i in range(500))
    chunks = chunk_snapshot(text)
    assert len(chunks) > 5 and all(c.startswith("line ") for c in chunks)



def test_reindex_without_an_embedder_still_drops_and_reports_the_skip(tmp_path):
    """CI-cold / no-embedder installs: the reindex must still DROP superseded and shredded chunks (the privacy half
    needs no embedder) and report the chunking it skipped, never an error."""
    st = _store(tmp_path / "a.db")
    _add(st, "old", "sess-A", "short\n" * 600, 100, _vec(1, 0))
    _add(st, "new", "sess-A", "longer\n" * 900, 200, _vec(1, 0))
    st.write_archive_chunks("old", ["stale chunk"], _fake_embed(["stale chunk"]))
    rep = reindex_chunks(st, None)
    ids = {r[0]: r[1] for r in st.db.execute("SELECT txid, archive_id FROM archives")}
    assert ids["old"] not in st.chunked_archive_ids() and rep["dropped_archives"] == 1
    assert rep["chunked"] == 0 and rep["skipped_no_embedder"] == 1



def test_long_lines_never_start_a_chunk_mid_word():
    """CSO 0419Z #1: with lines longer than the 200-char overlap, the only newline in the overlap is often the
    previous chunk's own terminator; the next start must fall back to a word start, never mid-word."""
    import random
    rnd = random.Random(7)
    words = ["provenance", "archive", "session", "snapshot", "kraken", "liquidity", "recheck", "fee", "offset", "a545"]
    text = "".join(" ".join(rnd.choice(words) for _ in range(rnd.randint(40, 80))) + "\n" for _ in range(120))
    chunks = chunk_snapshot(text)
    assert len(chunks) > 10
    starts_ok = [text[text.index(c) - 1] in "\n " if text.index(c) > 0 else True for c in chunks]
    assert all(starts_ok), f"{starts_ok.count(False)} of {len(chunks)} chunks start mid-word"
    assert all(c.endswith(("\n", " ")) for c in chunks[:-1])


def test_carry_does_not_abort_on_a_shredded_archives_vectorless_chunk(tmp_path):
    """CSO 0419Z #4: shredded rows are filtered BEFORE the missing-vector check, so a legacy shredded archive with a
    vectorless chunk does not abort the build."""
    prior = _store(tmp_path / "prior.db")
    _add(prior, "live", "sess-L", "live\n", 10, _vec(1, 0))
    _add(prior, "gone", "sess-G", "gone\n", 10, _vec(0, 1))
    prior.write_archive_chunks("live", ["live chunk"], [_vec(1, 0)])
    prior.write_archive_chunks("gone", ["gone chunk"], [_vec(0, 1)])
    gid = prior.db.execute("SELECT c.chunk_id FROM archive_chunks c JOIN archives a ON a.archive_id = c.archive_id "
                           "WHERE a.txid = 'gone'").fetchone()[0]
    prior.db.execute("DELETE FROM vec_archive_chunks WHERE rowid = ?", (gid,))
    prior.db.execute("UPDATE archives SET shredded = 1 WHERE txid = 'gone'"); prior.db.commit(); prior.db.close()
    staging = tmp_path / "staging.db"
    epoch_build._connect_vec(staging).close()
    epoch_build._carry_archive_index(tmp_path / "prior.db", staging)
    assert [r[0] for r in _store(staging).db.execute("SELECT text FROM archive_chunks")] == ["live chunk"]
