"""Hybrid search: BM25 (FTS5) + cosine (sqlite-vec) + graph + frontmatter signals."""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

from .config import Config
from .embed import EmbedClient
from .store import Store


@dataclass
class Hit:
    rel_path: str
    chunk_idx: int
    text: str
    title: str
    score: float
    vector_score: float = 0.0
    keyword_score: float = 0.0
    boosts: dict[str, float] = field(default_factory=dict)
    doc_type: str = ""
    is_priority: bool = False


def search(
    cfg: Config,
    store: Store,
    embedder: EmbedClient,
    query: str,
    k: int = 10,
    doc_type: str | None = None,
    priority_only: bool = False,
) -> list[Hit]:
    """Hybrid: vector top-N + BM25 top-N → merge → apply boosts → top-k."""
    over_k = max(k * 4, 40)

    # 1. Vector retrieval
    q_vec = embedder.embed_one(query)
    vec_rows = store.db.execute(
        "SELECT v.rowid AS chunk_id, vec_distance_cosine(v.embedding, ?) AS dist, "
        "       c.rel_path, c.chunk_idx, c.text, d.title, d.is_priority, d.doc_type "
        "FROM vec_chunks v "
        "JOIN chunks c ON c.chunk_id = v.rowid "
        "JOIN docs d ON d.rel_path = c.rel_path "
        "WHERE v.embedding MATCH ? AND k = ? "
        "ORDER BY dist",
        (q_vec, q_vec, over_k),
    ).fetchall()

    # 2. Keyword retrieval (BM25 via FTS5)
    fts_query = _fts_query(query)
    kw_rows = []
    if fts_query:
        try:
            kw_rows = store.db.execute(
                "SELECT c.chunk_id, fts_chunks.rank AS rank, c.rel_path, c.chunk_idx, c.text, "
                "       d.title, d.is_priority, d.doc_type "
                "FROM fts_chunks "
                "JOIN chunks c ON c.chunk_id = fts_chunks.rowid "
                "JOIN docs d ON d.rel_path = c.rel_path "
                "WHERE fts_chunks MATCH ? "
                "ORDER BY rank "
                "LIMIT ?",
                (fts_query, over_k),
            ).fetchall()
        except Exception:
            kw_rows = []

    # 3. Reciprocal Rank Fusion. Combine the two ranked lists by ordinal RANK,
    #    not by raw (incompatible) score scales — robust to the min-max
    #    degeneracy where the single closest vector is always forced to 1.0.
    #    Each list contributes 1/(rrf_k + rank); rrf_k (~60) flattens the curve
    #    so consistent top-rankers dominate without any single list overriding
    #    the consensus. vector_score / keyword_score now hold each list's RRF
    #    contribution (for display/debug), not a normalized similarity.
    rrf_k = cfg.rrf_k
    hits: dict[int, Hit] = {}

    def _hit(r) -> Hit:
        cid = r["chunk_id"]
        h = hits.get(cid)
        if h is None:
            h = Hit(
                rel_path=r["rel_path"],
                chunk_idx=r["chunk_idx"],
                text=r["text"],
                title=r["title"],
                score=0.0,
                doc_type=r["doc_type"],
                is_priority=bool(r["is_priority"]),
            )
            hits[cid] = h
        return h

    # vec_rows are ORDER BY dist (best first); kw_rows ORDER BY rank (best first).
    for rank, r in enumerate(vec_rows, start=1):
        h = _hit(r)
        c = 1.0 / (rrf_k + rank)
        h.vector_score += c
        h.score += c
    for rank, r in enumerate(kw_rows, start=1):
        h = _hit(r)
        c = 1.0 / (rrf_k + rank)
        h.keyword_score += c
        h.score += c

    # 4. Filters + domain boosts (multiplicative on the fused score)
    out: list[Hit] = []
    for h in hits.values():
        if doc_type and h.doc_type != doc_type:
            continue
        if priority_only and not h.is_priority:
            continue
        if h.is_priority:
            h.score *= cfg.priority_boost
            h.boosts["priority"] = cfg.priority_boost
        out.append(h)

    # 5. Wikilink graph boost — if a hit is wikilinked-to by another hit, lift it
    if out:
        slugs_in_hits = set()
        for h in out:
            slug = h.rel_path.rsplit("/", 1)[-1].replace(".md", "")
            slugs_in_hits.add(slug)
        for h in out:
            slug = h.rel_path.rsplit("/", 1)[-1].replace(".md", "")
            in_links = store.db.execute(
                "SELECT COUNT(*) AS n FROM wikilinks WHERE tgt_slug = ?", (slug,)
            ).fetchone()["n"]
            if in_links:
                lift = 1.0 + 0.05 * min(in_links, 5)  # cap influence
                h.score *= lift
                h.boosts["wikilink_inbound"] = lift

    out.sort(key=lambda h: h.score, reverse=True)
    return out[:k]


def keyword_only(store: Store, query: str, k: int = 10) -> list[Hit]:
    """Pure FTS5 keyword search. No embedding required."""
    fts_query = _fts_query(query)
    if not fts_query:
        return []
    rows = store.db.execute(
        "SELECT c.chunk_id, fts_chunks.rank AS rank, c.rel_path, c.chunk_idx, c.text, "
        "       d.title, d.is_priority, d.doc_type "
        "FROM fts_chunks "
        "JOIN chunks c ON c.chunk_id = fts_chunks.rowid "
        "JOIN docs d ON d.rel_path = c.rel_path "
        "WHERE fts_chunks MATCH ? "
        "ORDER BY rank "
        "LIMIT ?",
        (fts_query, k),
    ).fetchall()
    return [
        Hit(
            rel_path=r["rel_path"],
            chunk_idx=r["chunk_idx"],
            text=r["text"],
            title=r["title"],
            score=-r["rank"],
            keyword_score=-r["rank"],
            doc_type=r["doc_type"],
            is_priority=bool(r["is_priority"]),
        )
        for r in rows
    ]


def _fts_query(q: str) -> str:
    """Sanitize free-text into an FTS5 MATCH expression.

    Tokens become OR'd quoted terms. For 2–6 token queries we ALSO prepend the
    full token sequence as a quoted phrase, so contiguous/exact matches rank
    above scattered single-token hits. This is what recovers hyphen/dot
    compounds: unicode61 indexes ``C2PA-Manifest-Hash`` as the three tokens
    ``c2pa manifest hash``, so the phrase ``"c2pa manifest hash"`` matches it
    exactly while the individual OR'd terms preserve recall.
    """
    import re

    toks = [t for t in re.findall(r"[A-Za-z0-9_]+", q) if len(t) > 1]
    if not toks:
        return ""
    terms = [f'"{t}"' for t in toks]
    if 2 <= len(toks) <= 6:
        terms.insert(0, '"' + " ".join(toks) + '"')
    return " OR ".join(terms)
