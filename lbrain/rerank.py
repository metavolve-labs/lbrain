"""Optional second-stage cross-encoder reranker.

Retrieval (RRF + boosts + spreading activation) is recall-oriented and ranks by
proxy signals. A cross-encoder reads the (query, chunk) pair *jointly* and scores
true relevance — the precision pass. Kept lightweight and optional:

  1. fastembed TextCrossEncoder (ONNX runtime, no torch)  ← preferred
  2. sentence-transformers CrossEncoder                    ← fallback if present
  3. no-op (returns the fused order unchanged)             ← neither installed

Default-off via cfg.rerank. Hits are duck-typed (``.text``, ``.score``,
``.boosts``, ``.is_priority``) so this module has no import cycle with search.
"""

from __future__ import annotations

import math

# Module-level singleton so the model loads once per process (e.g. the MCP server).
_BACKEND = None  # tuple[str, object] | None  — ("fastembed"|"st"|"none", model)

# fastembed publishes Xenova/* names; sentence-transformers uses cross-encoder/*.
_ST_ALIASES = {
    "Xenova/ms-marco-MiniLM-L-6-v2": "cross-encoder/ms-marco-MiniLM-L-6-v2",
    "Xenova/ms-marco-MiniLM-L-12-v2": "cross-encoder/ms-marco-MiniLM-L-12-v2",
}


def _load(model_name: str):
    global _BACKEND
    if _BACKEND is not None:
        return _BACKEND
    try:  # preferred: ONNX, no heavy framework
        from fastembed.rerank.cross_encoder import TextCrossEncoder

        _BACKEND = ("fastembed", TextCrossEncoder(model_name=model_name))
        return _BACKEND
    except Exception:
        pass
    try:  # fallback: already common in ML envs (pulls torch)
        from sentence_transformers import CrossEncoder

        st_name = _ST_ALIASES.get(model_name, model_name)
        _BACKEND = ("st", CrossEncoder(st_name))
        return _BACKEND
    except Exception:
        _BACKEND = ("none", None)
        return _BACKEND


def available() -> bool:
    backend, _ = _load("Xenova/ms-marco-MiniLM-L-6-v2")
    return backend != "none"


def rerank(query, hits, model_name, top_n=30, priority_boost=1.3):
    """Reorder the top ``top_n`` fused candidates by cross-encoder relevance.

    Relevance is squashed to (0,1) via a logistic so scales are comparable, then
    the priority prior is re-applied multiplicatively (canonical lairs keep their
    edge). The untouched tail keeps its fused order and follows the reranked head.
    Returns the input unchanged when no backend is installed.
    """
    if not hits:
        return hits
    backend, model = _load(model_name)
    if backend == "none":
        return hits

    pool, tail = hits[:top_n], hits[top_n:]
    texts = [h.text for h in pool]
    if backend == "fastembed":
        scores = list(model.rerank(query, texts))
    else:  # sentence-transformers
        scores = [float(s) for s in model.predict([(query, t) for t in texts])]

    for h, s in zip(pool, scores):
        rel = 1.0 / (1.0 + math.exp(-float(s)))  # logistic → (0,1)
        if h.is_priority:
            rel *= priority_boost
        h.score = rel
        h.boosts["rerank"] = round(float(s), 3)

    pool.sort(key=lambda h: h.score, reverse=True)
    return pool + tail
