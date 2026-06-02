"""Tests for supersession-aware retrieval (Zep-inspired).

A doc that another doc explicitly supersedes is buried at retrieval — not
deleted — so the live truth surfaces while the original stays indexed for
provenance ("permanence at the substrate, selectivity at the surface").
"""

import tempfile
from pathlib import Path

from lbrain.index import parse
from lbrain.search import Hit
from lbrain.store import Store


def _doc(d: Path, name: str, body: str) -> Path:
    p = d / f"{name}.md"
    p.write_text(f"---\nname: {name}\n---\n{body}")
    return p


def test_parse_supersedes_body_marker():
    d = Path(tempfile.mkdtemp())
    p = _doc(d, "thing-new", "# New\n**Supersedes:** [[thing-old]]\napproach Y")
    assert parse(p, repo_root=d).supersedes == ["thing-old"]


def test_parse_supersedes_frontmatter_list():
    d = Path(tempfile.mkdtemp())
    p = d / "fm.md"
    p.write_text("---\nname: fm\nsupersedes: [alpha, beta]\n---\nbody")
    assert parse(p, repo_root=d).supersedes == ["alpha", "beta"]


def test_prose_mention_of_supersede_is_not_an_edge():
    """A doc merely discussing the word in prose (no wikilink) declares nothing."""
    d = Path(tempfile.mkdtemp())
    p = _doc(d, "essay", "# Essay\nThis paragraph supersedes nothing in particular.")
    assert parse(p, repo_root=d).supersedes == []


def test_superseded_doc_is_buried_below_live_truth():
    db = Path(tempfile.mkdtemp()) / "t.db"
    store = Store(db, embedding_dim=8)
    d = Path(tempfile.mkdtemp())
    for p in (
        _doc(d, "thing-old", "# Old\napproach X"),
        _doc(d, "thing-new", "# New\n**Supersedes:** [[thing-old]]\napproach Y"),
    ):
        doc = parse(p, repo_root=d)
        store.upsert_doc(doc)
        store.replace_supersessions(doc)
    store.db.commit()

    assert store.superseded_slugs() == {"thing-old"}

    # Old scores higher pre-penalty; the de-rank must flip the order.
    hits = [
        Hit(rel_path="thing-old.md", chunk_idx=0, text="X", title="Old", score=0.90),
        Hit(rel_path="thing-new.md", chunk_idx=0, text="Y", title="New", score=0.80),
    ]
    superseded, pen = store.superseded_slugs(), 0.25
    for h in hits:
        if h.rel_path.rsplit("/", 1)[-1].replace(".md", "") in superseded:
            h.score *= pen
            h.boosts["superseded"] = pen
    hits.sort(key=lambda h: h.score, reverse=True)
    assert hits[0].title == "New"
    assert hits[1].boosts.get("superseded") == pen
    store.close()


def test_pruning_the_superseder_clears_the_edge():
    import os

    db = Path(tempfile.mkdtemp()) / "t.db"
    store = Store(db, embedding_dim=8)
    d = Path(tempfile.mkdtemp())
    old = _doc(d, "thing-old", "# Old\napproach X")
    new = _doc(d, "thing-new", "# New\n**Supersedes:** [[thing-old]]\napproach Y")
    for p in (old, new):
        doc = parse(p, repo_root=d)
        store.upsert_doc(doc)
        store.replace_supersessions(doc)
    store.db.commit()
    assert store.superseded_slugs() == {"thing-old"}

    os.remove(new)
    store.prune_missing()
    store.db.commit()
    assert store.superseded_slugs() == set()
    store.close()
