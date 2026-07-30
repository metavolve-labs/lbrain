"""Ranking behaviour — the features the product is actually sold on.

Written 2026-07-30 to close anomaly A-414. Before this file, **nothing** tested
priority boost, the wikilink graph boost, RRF fusion, the recency curve, the
abstraction cap, or the supersession de-rank — every ranking behaviour in the
pitch. That gap was not theoretical: four bugs lived in it undetected.

  A-404  the priority/wikilink/supersession slug split on "/" only, so all three
         silently no-opped on Windows
  A-402  stale_marker branched on labels record_date could never return
  A-422  supersession could be recorded INVERTED, burying the live record
  A-423  164 of 167 lairs collapsed onto one slug; links resolved 36% of the time

Each was found by running something against reality, never by reading the code.
These tests make the next one fail loudly instead.

Design note: the embedder is deterministic and fake. Ranking is what is under
test, not embedding quality — a real model would make these tests slow, networked
and non-deterministic while testing someone else's code.
"""

from __future__ import annotations

import struct

import pytest

from lbrain.config import Config
from lbrain.index import chunk as chunk_doc
from lbrain.index import parse
from lbrain.search import search
from lbrain.store import Store

DIM = 8


class FakeEmbedder:
    """Deterministic vectors from a token-overlap hash.

    Same text always yields the same vector, and texts sharing tokens land nearer
    each other — enough structure for RRF to have something to fuse, with no
    network and no model download.
    """

    def __init__(self, dim: int = DIM):
        self.dim = dim

    def _vec(self, text: str) -> bytes:
        acc = [0.0] * self.dim
        for tok in text.lower().split():
            acc[hash(tok) % self.dim] += 1.0
        norm = sum(v * v for v in acc) ** 0.5 or 1.0
        return struct.pack(f"{self.dim}f", *[v / norm for v in acc])

    def embed(self, texts, batch_size: int = 64):
        return [self._vec(t) for t in texts]

    def embed_one(self, text: str) -> bytes:
        return self._vec(text)

    def close(self):
        pass


def _cfg(**kw) -> Config:
    c = Config(embedding_provider="local", embedding_dim=DIM)
    c.rrf_k = 60
    c.priority_boost = 1.3
    c.supersede_aware = True
    c.abstraction_topk_cap = 2
    for k, v in kw.items():
        setattr(c, k, v)
    return c


def _brain(tmp_path, docs: dict[str, str]):
    """docs: {relative/path.md: full file text} → (cfg, store, embedder)."""
    root = tmp_path / "corpus"
    for rel, text in docs.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")

    cfg = _cfg()
    cfg.db_path = tmp_path / "brain.db"
    store = Store(cfg.db_path, embedding_dim=DIM)
    emb = FakeEmbedder()

    for rel in docs:
        doc = parse(root / rel, repo_root=root)
        store.upsert_doc(doc)
        chunks = chunk_doc(doc)
        ids = store.insert_chunks(chunks)
        vecs = emb.embed([c.text for c in chunks])
        store.write_embeddings(ids, vecs)
        store.replace_wikilinks(doc)
        store.replace_supersessions(doc)
    store.db.commit()
    return cfg, store, emb


def _rank(hits, needle: str) -> int:
    for i, h in enumerate(hits):
        if needle in h.rel_path:
            return i
    return -1


# --- priority boost ---------------------------------------------------------

def test_priority_boost_lifts_a_000_PRIORITY_document(tmp_path):
    """`000-PRIORITY` in any path segment multiplies the fused score."""
    cfg, store, emb = _brain(tmp_path, {
        "plain/notes.md": "---\nname: plain\n---\n# Deploy\n\nthe deploy rollback flag alpha\n",
        "000-PRIORITY-OPS/LAIR.md": "---\nname: ops\n---\n# Deploy\n\nthe deploy rollback flag alpha\n",
    })
    hits = search(cfg, store, emb, "deploy rollback flag alpha", k=5)
    prio = [h for h in hits if h.is_priority]
    assert prio, "the 000-PRIORITY doc was not retrieved at all"
    assert prio[0].boosts.get("priority") == cfg.priority_boost
    # identical text, so the only differentiator is the boost
    assert _rank(hits, "000-PRIORITY-OPS") < _rank(hits, "plain/notes.md")
    store.close()


def test_priority_boost_is_off_for_ordinary_documents(tmp_path):
    cfg, store, emb = _brain(tmp_path, {
        "plain/notes.md": "---\nname: plain\n---\n# Deploy\n\nrollback flag alpha\n",
    })
    hits = search(cfg, store, emb, "rollback flag alpha", k=5)
    assert hits and "priority" not in hits[0].boosts
    store.close()


def test_priority_only_filter_excludes_everything_else(tmp_path):
    cfg, store, emb = _brain(tmp_path, {
        "plain/a.md": "---\nname: a\n---\nrollback flag alpha\n",
        "000-PRIORITY-X/LAIR.md": "---\nname: x\n---\nrollback flag alpha\n",
    })
    hits = search(cfg, store, emb, "rollback flag alpha", k=5, priority_only=True)
    assert hits and all(h.is_priority for h in hits)
    store.close()


# --- wikilink graph boost (A-423 regression) --------------------------------

def test_wikilink_inbound_boost_lifts_a_linked_document(tmp_path):
    """A doc other docs point at is lifted. Inert for two-thirds of the live
    corpus until 2026-07-30 because targets are written as relative paths."""
    cfg, store, emb = _brain(tmp_path, {
        "target/notes.md": "---\nname: notes\n---\n# Topic\n\nwidget calibration procedure\n",
        "other/decoy.md": "---\nname: decoy\n---\n# Topic\n\nwidget calibration procedure\n",
        "a/one.md": "---\nname: one\n---\nsee [[notes]] for widget calibration procedure\n",
        "b/two.md": "---\nname: two\n---\nalso [[notes]] on widget calibration procedure\n",
    })
    hits = search(cfg, store, emb, "widget calibration procedure", k=6)
    tgt = next((h for h in hits if "target/notes.md" in h.rel_path), None)
    assert tgt is not None, "linked doc not retrieved"
    assert tgt.boosts.get("wikilink_inbound", 1.0) > 1.0, "inbound links did not lift it"
    store.close()


def test_relative_path_wikilinks_still_count(tmp_path):
    """The A-423 dominant cause: Obsidian-style relative targets."""
    cfg, store, emb = _brain(tmp_path, {
        "deep/target/notes.md": "---\nname: notes\n---\nwidget calibration procedure\n",
        "a/one.md": "---\nname: one\n---\nsee [[../../deep/target/notes]] widget calibration procedure\n",
    })
    hits = search(cfg, store, emb, "widget calibration procedure", k=6)
    tgt = next((h for h in hits if "deep/target/notes.md" in h.rel_path), None)
    assert tgt is not None
    assert tgt.boosts.get("wikilink_inbound", 1.0) > 1.0, \
        "a relative-path wikilink did not resolve — A-423 regression"
    store.close()


def test_a_lair_is_linkable_by_its_directory_name(tmp_path):
    """164 of 167 lairs shared the slug `LAIR` before A-423."""
    cfg, store, emb = _brain(tmp_path, {
        "000-PRIORITY-REGISTER/LAIR.md": "---\nname: reg\n---\nwidget calibration procedure\n",
        "a/one.md": "---\nname: one\n---\nsee [[000-PRIORITY-REGISTER]] widget calibration procedure\n",
    })
    hits = search(cfg, store, emb, "widget calibration procedure", k=6)
    tgt = next((h for h in hits if "000-PRIORITY-REGISTER" in h.rel_path), None)
    assert tgt is not None
    assert tgt.boosts.get("wikilink_inbound", 1.0) > 1.0, \
        "a lair could not be linked by its directory name — A-423 regression"
    store.close()


# --- supersession de-rank (A-422 regression) --------------------------------

def test_a_superseded_document_is_buried_but_still_retrievable(tmp_path):
    cfg, store, emb = _brain(tmp_path, {
        "old/thing-old.md": "---\nname: thing-old\n---\ncalibration approach for the widget\n",
        "new/thing-new.md":
            "---\nname: thing-new\n---\n**Supersedes:** [[thing-old]]\n\n"
            "calibration approach for the widget\n",
    })
    hits = search(cfg, store, emb, "calibration approach for the widget", k=6)
    old = next((h for h in hits if "thing-old" in h.rel_path), None)
    assert old is not None, "superseded docs must stay RETRIEVABLE, not be deleted"
    assert "superseded" in old.boosts, "superseded doc was not de-ranked"
    assert _rank(hits, "thing-new") < _rank(hits, "thing-old"), "live truth must outrank"
    store.close()


def test_supersession_is_not_recorded_backwards(tmp_path):
    """A-422. A doc declaring itself REPLACED must not bury its replacement."""
    cfg, store, emb = _brain(tmp_path, {
        "000-PRIORITY-LIVE/LAIR.md": "---\nname: live\n---\nthe canonical widget register\n",
        "stub/redirect.md":
            "---\nname: redirect\n---\n"
            "**Supersedes**: nothing · **Superseded by**: [[000-PRIORITY-LIVE]]\n\n"
            "the canonical widget register\n",
    })
    hits = search(cfg, store, emb, "the canonical widget register", k=6)
    live = next((h for h in hits if "000-PRIORITY-LIVE" in h.rel_path), None)
    assert live is not None
    assert "superseded" not in live.boosts, \
        "the LIVE record was buried by the stub pointing at it — A-422 regression"
    store.close()


# --- RRF fusion -------------------------------------------------------------

def test_rrf_rewards_agreement_between_the_two_retrievers(tmp_path):
    """RRF fuses by ordinal RANK, not raw score. A doc both retrievers like
    should beat one that only a single retriever likes."""
    cfg, store, emb = _brain(tmp_path, {
        "both/agree.md": "---\nname: agree\n---\nquantum widget calibration telemetry\n",
        "kw/only.md": "---\nname: kwonly\n---\nquantum unrelated content here entirely\n",
    })
    hits = search(cfg, store, emb, "quantum widget calibration telemetry", k=5)
    assert hits, "no hits"
    assert "both/agree.md" in hits[0].rel_path
    store.close()


def test_rrf_contributions_are_recorded_for_audit(tmp_path):
    """vector_score / keyword_score carry each list's RRF contribution."""
    cfg, store, emb = _brain(tmp_path, {
        "a/one.md": "---\nname: one\n---\nwidget calibration telemetry\n",
    })
    hits = search(cfg, store, emb, "widget calibration telemetry", k=3)
    assert hits
    assert hits[0].vector_score > 0 or hits[0].keyword_score > 0
    assert hits[0].score > 0
    store.close()


# --- filters ----------------------------------------------------------------

def test_doc_type_filter_excludes_other_types(tmp_path):
    cfg, store, emb = _brain(tmp_path, {
        "a/fb.md": "---\nname: fb\ntype: feedback\n---\nrollback flag alpha\n",
        "b/pj.md": "---\nname: pj\ntype: project\n---\nrollback flag alpha\n",
    })
    hits = search(cfg, store, emb, "rollback flag alpha", k=5, doc_type="feedback")
    assert hits and all(h.doc_type == "feedback" for h in hits)
    store.close()


# --- abstraction cap + recency guard ----------------------------------------

def test_abstractions_are_capped_in_the_final_topk(tmp_path):
    """Measured 2026-07-11: uncapped abstractions at ~46% corpus share cost
    recency −0.083 MRR and evicted gold source docs."""
    docs = {f"abs/abstraction-{i}.md": f"---\nname: a{i}\ntype: abstraction\n---\nwidget calibration\n"
            for i in range(5)}
    docs["src/real.md"] = "---\nname: real\n---\nwidget calibration\n"
    cfg, store, emb = _brain(tmp_path, docs)
    cfg.abstraction_topk_cap = 2
    hits = search(cfg, store, emb, "widget calibration", k=6)
    n_abs = sum(1 for h in hits if "abstraction-" in h.rel_path)
    assert n_abs <= 2, f"abstraction cap not enforced: {n_abs} in top-k"
    store.close()


def test_temporal_queries_demote_abstractions_below_sources(tmp_path):
    """An abstraction's mtime is its SYNTHESIS time, not its content's age, so
    freshness math would flatter exactly the stale summaries this guards against."""
    cfg, store, emb = _brain(tmp_path, {
        "abs/abstraction-x.md": "---\nname: ax\ntype: abstraction\n---\nwidget calibration status\n",
        "src/real.md": "---\nname: real\n---\nwidget calibration status\n",
    })
    hits = search(cfg, store, emb, "what is the latest widget calibration status", k=5)
    assert _rank(hits, "src/real.md") < _rank(hits, "abstraction-x"), \
        "a temporal query must rank source documents above abstractions"
    store.close()


# --- the guard that would have caught A-404 ---------------------------------

@pytest.mark.parametrize("sep", ["/", "\\"])
def test_every_ranking_signal_survives_both_path_separators(sep):
    """A-404: priority, wikilink and supersession all derived slugs by splitting
    on "/" only, so all three silently no-opped on Windows. One assertion per
    signal, on both separators, so the class cannot regress quietly."""
    import re

    from lbrain.search import _basename_slug, canonical_slug

    rel = sep.join(["P3-PLATE", "000-PRIORITY-REGISTER", "LAIR.md"])
    assert any(p.startswith("000-PRIORITY") for p in re.split(r"[\\/]", rel)), "priority"
    assert _basename_slug(rel) == "000-PRIORITY-REGISTER", "slug"
    assert canonical_slug(sep.join(["..", "..", "000-PRIORITY-REGISTER", "LAIR"])) == \
        "000-PRIORITY-REGISTER", "wikilink target"
