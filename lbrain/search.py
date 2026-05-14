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

    # 3. Merge & rerank
    hits: dict[int, Hit] = {}

    # vec_rows: lower dist = better. Map to similarity in [0,1] where 1 is best.
    if vec_rows:
        v_dists = [r["dist"] for r in vec_rows]
        v_min, v_max = min(v_dists), max(v_dists)
        v_span = max(v_max - v_min, 1e-6)
        for r in vec_rows:
            sim = 1.0 - (r["dist"] - v_min) / v_span  # normalized
            cid = r["chunk_id"]
            hits[cid] = Hit(
                rel_path=r["rel_path"],
                chunk_idx=r["chunk_idx"],
                text=r["text"],
                title=r["title"],
                score=0.0,
                vector_score=sim,
                doc_type=r["doc_type"],
                is_priority=bool(r["is_priority"]),
            )

    # kw_rows: more negative rank = better (FTS5 convention).
    if kw_rows:
        k_ranks = [r["rank"] for r in kw_rows]
        k_min, k_max = min(k_ranks), max(k_ranks)
        k_span = max(k_max - k_min, 1e-6)
        for r in kw_rows:
            norm = 1.0 - (r["rank"] - k_min) / k_span  # normalized
            cid = r["chunk_id"]
            if cid in hits:
                hits[cid].keyword_score = norm
            else:
                hits[cid] = Hit(
                    rel_path=r["rel_path"],
                    chunk_idx=r["chunk_idx"],
                    text=r["text"],
                    title=r["title"],
                    score=0.0,
                    keyword_score=norm,
                    doc_type=r["doc_type"],
                    is_priority=bool(r["is_priority"]),
                )

    # 4. Apply weights + boosts
    out: list[Hit] = []
    for h in hits.values():
        base = cfg.bm25_weight * h.keyword_score + cfg.vector_weight * h.vector_score
        if h.is_priority:
            base *= cfg.priority_boost
            h.boosts["priority"] = cfg.priority_boost
        if doc_type and h.doc_type != doc_type:
            continue
        if priority_only and not h.is_priority:
            continue
        h.score = base
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
    """Sanitize a free-text query for FTS5 MATCH. Quote each token; drop ones with no alphanumerics."""
    import re

    toks = re.findall(r"[A-Za-z0-9_]+", q)
    if not toks:
        return ""
    return " OR ".join(f'"{t}"' for t in toks if len(t) > 1)
