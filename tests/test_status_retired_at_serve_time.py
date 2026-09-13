"""A3 (1.0 acceptance, 2026-09-11) — self-declared retirement must reach the ranker and the reader.

CSO replay on a clean non-doctrine fixture (manifest sha 588cc863, results file
LBRAIN-1.0-RESULTS-A3-REPLAY-NON-DOCTRINE-CSO-2026-09-11.md): `index.is_retired_signal()`
accepts three retirement declarations (frontmatter `status`, `retired: true`, path marker),
but only the supersession EDGE reached `_resolve_superseded_paths`, the ranker, and the
served header. A record with `status: superseded` and no edge was served at rank 1 above its
own correction, rendered exactly like a live record, `SUPERSEDED` count 0.

These tests CALL the retrieval paths and the header renderer; they do not grep source.
Verified to fail against the pre-fix tree (lbrain HEAD 8fe5d07) before being committed.
"""
import struct

from lbrain.config import Config
from lbrain.index import chunk as chunk_doc
from lbrain.index import parse
from lbrain.search import keyword_only, search
from lbrain.serve import _header
from lbrain.store import Store

DIM = 32

# The three routes, plus a both-routes record and a live control. Bodies share tokens so the
# fake embedder and FTS both have something to rank; no retirement word appears in a title.
_DOCS = {
    "q/quillon-v1.md": (
        "---\nstatus: superseded\ndate: 2026-09-11\n---\n"
        "# ZG-QUILLON seal latency p95 figure (v1)\n\n"
        "ZG-QUILLON seal latency is 480 ms at the p95 mark. Budget half a second.\n"),
    "q/quillon-v2-correction.md": (
        "# ZG-QUILLON seal latency corrected to 112 ms p95\n\n"
        "The earlier ZG-QUILLON p95 of 480 ms counted queue wait. ZG-QUILLON seal latency p95 is 112 ms.\n"),
    "r/flag-true.md": (
        "---\nretired: true\n---\n# ZG-QUILLON sealing note\n\nZG-QUILLON seal latency note, retired by flag.\n"),
    "p/notes-archived-20260101/marker.md": (
        "# ZG-QUILLON sealing archive\n\nZG-QUILLON seal latency archived by path marker.\n"),
    "b/marlin-v1.md": (
        "---\nstatus: superseded\n---\n# ZG-MARLIN drift v1\n\nZG-MARLIN pane drift is 7.4x.\n"),
    "b/marlin-v2.md": (
        "# ZG-MARLIN drift corrected\n\n**Supersedes:** [[marlin-v1]]\n\nZG-MARLIN pane drift is 2.6 points.\n"),
    "live/sandgrouse.md": (
        "# ZG-SANDGROUSE ledger tally\n\nZG-SANDGROUSE ledger tally 318 entries, 41 unreconciled. seal latency unrelated.\n"),
}


class FakeEmbedder:
    def __init__(self, dim=DIM): self.dim = dim
    def _vec(self, text):
        acc = [0.0] * self.dim
        for tok in text.lower().split():
            acc[hash(tok) % self.dim] += 1.0
        n = sum(v * v for v in acc) ** 0.5 or 1.0
        return struct.pack(f"{self.dim}f", *[v / n for v in acc])
    def embed(self, texts, batch_size=64): return [self._vec(t) for t in texts]
    def embed_one(self, text): return self._vec(text)
    def close(self): pass


def _brain(tmp_path):
    root = tmp_path / "corpus"
    for rel, text in _DOCS.items():
        p = root / rel; p.parent.mkdir(parents=True, exist_ok=True); p.write_text(text, encoding="utf-8")
    cfg = Config(embedding_provider="local", embedding_dim=DIM)
    cfg.rrf_k = 60; cfg.supersede_aware = True; cfg.supersede_penalty = 0.25
    cfg.db_path = tmp_path / "brain.db"
    store = Store(cfg.db_path, embedding_dim=DIM)
    emb = FakeEmbedder()
    for rel in _DOCS:
        doc = parse(root / rel, repo_root=root)
        store.upsert_doc(doc)
        chunks = chunk_doc(doc)
        ids = store.insert_chunks(chunks)
        store.write_embeddings(ids, emb.embed([c.text for c in chunks]))
        store.replace_wikilinks(doc)
        store.replace_supersessions(doc)
    store.db.commit()
    return cfg, store, emb


def _paths(hits): return [h.rel_path for h in hits]


def test_resolver_finds_all_three_routes(tmp_path):
    # imported here, not at module level, so the other four tests still COLLECT against a
    # pre-fix tree and fail on behaviour rather than on an ImportError (a missing name proves
    # the function is new, not that serving changed)
    from lbrain.search import _resolve_self_retired_paths
    _, store, _ = _brain(tmp_path)
    got = _resolve_self_retired_paths(store)
    assert got == {"q/quillon-v1.md", "r/flag-true.md", "p/notes-archived-20260101/marker.md", "b/marlin-v1.md"}
    assert "live/sandgrouse.md" not in got and "q/quillon-v2-correction.md" not in got


def test_keyword_current_only_excludes_status_retired(tmp_path):
    _, store, _ = _brain(tmp_path)
    hits = keyword_only(store, "ZG-QUILLON seal latency", k=10, current_only=True)
    assert "q/quillon-v1.md" not in _paths(hits), "status-retired record must be excluded under current_only"
    assert "r/flag-true.md" not in _paths(hits)
    assert "p/notes-archived-20260101/marker.md" not in _paths(hits)
    assert "q/quillon-v2-correction.md" in _paths(hits), "the correction survives"


def test_keyword_default_flags_status_retired_and_header_says_RETIRED(tmp_path):
    _, store, _ = _brain(tmp_path)
    hits = keyword_only(store, "ZG-QUILLON seal latency", k=10)
    v1 = [h for h in hits if h.rel_path == "q/quillon-v1.md"]
    assert v1, "default (history) retrieval still returns the retired record"
    assert "retired" in v1[0].boosts and "superseded" not in v1[0].boosts
    line = _header(1, v1[0], "ADMISSIBLE")
    assert "RETIRED" in line and "SUPERSEDED" not in line
    live = [h for h in hits if h.rel_path == "q/quillon-v2-correction.md"][0]
    assert "RETIRED" not in _header(2, live, "ADMISSIBLE")


def test_ranked_current_only_excludes_and_default_deranks(tmp_path):
    cfg, store, emb = _brain(tmp_path)
    cur = search(cfg, store, emb, "What is the ZG-QUILLON seal latency at p95?", k=10, current_only=True)
    assert "q/quillon-v1.md" not in _paths(cur)
    assert "q/quillon-v2-correction.md" in _paths(cur)
    hist = search(cfg, store, emb, "What is the ZG-QUILLON seal latency at p95?", k=10)
    v1 = [h for h in hist if h.rel_path == "q/quillon-v1.md"]
    assert v1 and v1[0].boosts.get("retired") == 0.25
    # the correction ranks above the record it corrects
    assert _paths(hist).index("q/quillon-v2-correction.md") < _paths(hist).index("q/quillon-v1.md")


def test_both_routes_is_superseded_once_not_retired_twice(tmp_path):
    cfg, store, emb = _brain(tmp_path)
    hist = search(cfg, store, emb, "ZG-MARLIN pane drift", k=10)
    v1 = [h for h in hist if h.rel_path == "b/marlin-v1.md"]
    assert v1 and "superseded" in v1[0].boosts and "retired" not in v1[0].boosts
    assert "SUPERSEDED" in _header(1, v1[0], "ADMISSIBLE")
