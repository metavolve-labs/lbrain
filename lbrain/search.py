"""Hybrid search: BM25 (FTS5) + cosine (sqlite-vec) fused by Reciprocal Rank Fusion,
then a few cheap, bounded signal boosts (priority, wikilink graph, supersession)."""

from __future__ import annotations

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
    mtime: float = 0.0


# Temporal query signature — triggers the abstraction recency guardrail.
_TEMPORAL_RE = __import__("re").compile(
    r"\b(latest|recent(ly)?|current(ly)?|now|today|newest|most recent|status|"
    r"state of|up[- ]?to[- ]?date|this (week|month)|what changed|updated?)\b",
    __import__("re").IGNORECASE,
)


def _is_abstraction(h: Hit) -> bool:
    """type: abstraction awareness. doc_type when the importer captured it;
    filename convention as fallback (verified 2026-07-11: 10/50 live abstraction
    docs carry an empty doc_type — never trust the field alone)."""
    if h.doc_type == "abstraction":
        return True
    name = h.rel_path.rsplit("/", 1)[-1]
    return name.startswith("abstraction-") or name.startswith("abstraction_")


def _assemble_topk(out: list[Hit], k: int, cfg: Config, query: str, recency: bool) -> list[Hit]:
    """Abstraction-aware final assembly (measured 2026-07-11: at ~46% corpus
    share, uncapped abstractions cost recency −0.083 MRR and evicted gold docs
    from the top-k; at low density they are net-safe enrichment).

    Recency guardrail — on temporally-signed queries (regex or explicit
    recency=True), source documents hold the high ground: abstraction hits are
    stably demoted below ALL source hits. Deliberately stricter than a
    timestamp comparison: an abstraction's mtime is its GENERATION time, not
    its content's age, so freshness math would flatter exactly the stale
    summaries this guards against.

    Density cap — at most cfg.abstraction_topk_cap abstraction chunks in the
    final top-k; excess abstractions yield their slots to source chunks.
    """
    guard = getattr(cfg, "abstraction_recency_guard", True)
    if guard and (recency or _TEMPORAL_RE.search(query)):
        demoted = [h for h in out if _is_abstraction(h)]
        if demoted:
            out = [h for h in out if not _is_abstraction(h)] + demoted
            for h in demoted:
                h.boosts["recency_guard"] = 0.0  # marker: demoted below sources

    cap = getattr(cfg, "abstraction_topk_cap", 2)
    if cap < 0:
        return out[:k]
    picked: list[Hit] = []
    n_abs = 0
    for h in out:
        if _is_abstraction(h):
            if n_abs >= cap:
                continue
            n_abs += 1
        picked.append(h)
        if len(picked) >= k:
            break
    return picked


def search(
    cfg: Config,
    store: Store,
    embedder: EmbedClient,
    query: str,
    k: int = 10,
    doc_type: str | None = None,
    priority_only: bool = False,
    rerank: bool = False,
    recency: bool = False,
) -> list[Hit]:
    """Hybrid: vector top-N + BM25 top-N → RRF merge → always-on boosts → top-k.

    Always-on boosts (net-positive in every regime, measured 2026-06-08): priority,
    wikilink graph, supersession de-ranking.

    Call-when-needed (default OFF — see lairs/000-OPERATING-DOCTRINE):
      recency=True — bounded, read-only mtime-freshness lift; use for recency-sensitive
                     queries ("what's the latest on X"). Priority docs exempt.
      rerank=True  — cross-encoder precision pass over the head; use for PRECISE/
                     known-item lookups. Do NOT use for broad/exploratory queries (it
                     hurts multi-doc coverage). No-ops without the lbrain[rerank] backend.
    """
    over_k = max(k * 4, 40)

    # 1. Vector retrieval
    q_vec = embedder.embed_one(query)
    vec_rows = store.db.execute(
        "SELECT v.rowid AS chunk_id, vec_distance_cosine(v.embedding, ?) AS dist, "
        "       c.rel_path, c.chunk_idx, c.text, d.title, d.is_priority, d.doc_type, "
        "       d.mtime "
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
                "       d.title, d.is_priority, d.doc_type, d.mtime "
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
                mtime=r["mtime"],
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

    # 5.5 Supersession-aware de-ranking (Zep-inspired). A doc that another doc
    #     explicitly supersedes is BURIED, not deleted — the live truth surfaces
    #     while the original stays retrievable for provenance/audit. This turns
    #     the "amendable, supersede-not-overwrite" convention into actual ranking
    #     behavior: "permanence at the substrate, selectivity at the surface."
    if getattr(cfg, "supersede_aware", True) and out:
        superseded = store.superseded_slugs()
        if superseded:
            pen = getattr(cfg, "supersede_penalty", 0.25)
            for h in out:
                slug = h.rel_path.rsplit("/", 1)[-1].replace(".md", "")
                if slug in superseded:
                    h.score *= pen
                    h.boosts["superseded"] = pen

    # 6. Recency (call-when-needed) — bounded mtime freshness for recency-sensitive
    #    queries. READ-ONLY (no salience writes, no feedback loop), priority docs exempt.
    if recency and out:
        import math
        import time as _time

        now = _time.time()
        hl = 120.0 * 86400.0  # 120-day half-life
        for h in out:
            if h.is_priority:
                continue
            if _is_abstraction(h):
                # mtime = generation time, not content age — a freshness lift here
                # would flatter yesterday's synthesis of last month's state.
                continue
            age = max(now - (h.mtime or now), 0.0)
            freshness = math.pow(0.5, age / hl)  # (0,1], 1 = brand new
            factor = 1.0 + 0.15 * (freshness - 0.5)  # bounded ±0.075
            h.score *= factor
            h.boosts["recency"] = round(factor, 3)

    out.sort(key=lambda h: h.score, reverse=True)

    # 7. Cross-encoder rerank (call-when-needed) — joint (query, chunk) precision pass
    #    over the fused head. No-op without a reranker backend installed.
    if rerank and out:
        from .rerank import rerank as _rerank

        out = _rerank(query, out, top_n=30, priority_boost=cfg.priority_boost)

    # 8. Abstraction-aware final assembly: recency guardrail + top-k density cap.
    #    Applied LAST so the guarantees hold regardless of boosts or rerank.
    return _assemble_topk(out, k, cfg, query, recency)


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
