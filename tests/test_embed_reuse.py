"""Hash-reuse on rebuild (CSO 2026-08-30): copy embeddings by chunk_hash from a source
brain so a de-wholesale rebuild re-embeds only genuinely-new chunks (~89% reuse measured).
Fingerprint-guarded: same text under a different embedder is a different vector space.
"""
import struct

import pytest

from lbrain.index import chunk as chunk_doc
from lbrain.index import parse
from lbrain.store import Store

DIM = 4


def _vec(seed):
    return struct.pack("<4f", *(float(seed + i) for i in range(DIM)))


def _fingerprint(st, model="test-model", provider="local"):
    st.set_meta("embedding_model", model)
    st.set_meta("embedding_provider", provider)
    st.set_meta("embedding_dim", DIM)


def _add(st, corpus, rel, txt):
    p = corpus / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    if not p.exists():
        p.write_text(txt)
    doc = parse(p, repo_root=corpus)
    st.upsert_doc(doc)
    st.insert_chunks(chunk_doc(doc))


@pytest.fixture()
def brains(tmp_path, monkeypatch):
    monkeypatch.setenv("LBRAIN_HOME", str(tmp_path / "home"))
    corpus = tmp_path / "c"
    # SOURCE: a.md + b.md, fully embedded with fake vectors.
    src = Store(tmp_path / "src.db", embedding_dim=DIM)
    _add(src, corpus, "a.md", "# A\napple banana cherry.\n")
    _add(src, corpus, "b.md", "# B\ndog elephant frog.\n")
    rows = src.db.execute("SELECT chunk_id FROM chunks").fetchall()
    src.write_embeddings([r["chunk_id"] for r in rows], [_vec(i) for i in range(len(rows))])
    _fingerprint(src)
    src.db.commit()
    return tmp_path, corpus


def test_reuse_copies_matching_leaves_new(brains):
    tmp_path, corpus = brains
    # TARGET rebuild: byte-identical a.md (same chunk_hash) + brand-new c.md, none embedded.
    tgt = Store(tmp_path / "tgt.db", embedding_dim=DIM)
    _add(tgt, corpus, "a.md", "")                       # existing file → same chunk_hash as source
    _add(tgt, corpus, "c.md", "# C\ngrape honeydew.\n")  # new
    _fingerprint(tgt)                                    # same embedder fingerprint
    tgt.db.commit()

    reused, cand = tgt.reuse_embeddings_from(str(tmp_path / "src.db"),
                                             model="test-model", provider="local")
    assert reused == 1, "a.md's byte-identical chunk is reused"
    embedded = {r["rel_path"]: r["embedded"]
                for r in tgt.db.execute("SELECT rel_path, embedded FROM chunks")}
    assert embedded["a.md"] == 1, "reused chunk marked embedded"
    assert embedded["c.md"] == 0, "genuinely-new chunk left for fresh embedding"


def test_fingerprint_mismatch_reuses_nothing(brains):
    tmp_path, corpus = brains
    tgt = Store(tmp_path / "tgt2.db", embedding_dim=DIM)
    _add(tgt, corpus, "a.md", "")
    _fingerprint(tgt, model="OTHER-model")   # different embedder → different vector space
    tgt.db.commit()
    reused, _ = tgt.reuse_embeddings_from(str(tmp_path / "src.db"),
                                          model="OTHER-model", provider="local")
    assert reused == 0, "a different embedder fingerprint must reuse nothing (embed fresh)"


def test_missing_source_is_safe(brains):
    tmp_path, corpus = brains
    tgt = Store(tmp_path / "tgt3.db", embedding_dim=DIM)
    _add(tgt, corpus, "a.md", "")
    _fingerprint(tgt)
    tgt.db.commit()
    assert tgt.reuse_embeddings_from(str(tmp_path / "nope.db"),
                                     model="test-model", provider="local") == (0, 0)
