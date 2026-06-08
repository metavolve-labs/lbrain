"""Optional cross-encoder reranker — call-when-needed (install via lbrain[rerank]).

A bi-encoder retrieves by independent query/doc embeddings; a cross-encoder reads the
(query, chunk) pair JOINTLY and scores true relevance — a precision pass. Measured
(2026-06-08) to help PRECISE/known-item lookups and hurt broad/multi-doc coverage, so
it is OFF by default and enabled per call (search(..., rerank=True)). It no-ops
gracefully when no backend is installed, so the core never hard-depends on it.

Backends, in preference order:
  1. fastembed TextCrossEncoder (ONNX, no torch)   ← `lbrain[rerank]`
  2. sentence-transformers CrossEncoder            ← fallback if already present
  3. none → return the input order unchanged
"""
from __future__ import annotations

import math

_BACKEND = None  # ("fastembed"|"st"|"none", model) — loaded once per process
_MODEL = "Xenova/ms-marco-MiniLM-L-6-v2"
_ST_ALIASES = {"Xenova/ms-marco-MiniLM-L-6-v2": "cross-encoder/ms-marco-MiniLM-L-6-v2"}


def _load():
    global _BACKEND
    if _BACKEND is not None:
        return _BACKEND
    try:  # preferred: ONNX, no heavy framework
        from fastembed.rerank.cross_encoder import TextCrossEncoder

        _BACKEND = ("fastembed", TextCrossEncoder(model_name=_MODEL))
    except Exception:
        try:  # fallback: common in ML envs (pulls torch)
            from sentence_transformers import CrossEncoder

            _BACKEND = ("st", CrossEncoder(_ST_ALIASES.get(_MODEL, _MODEL)))
        except Exception:
            _BACKEND = ("none", None)
    return _BACKEND


def rerank(query, hits, top_n: int = 30, priority_boost: float = 1.3):
    """Reorder the top ``top_n`` fused candidates by cross-encoder relevance.

    Relevance is squashed to (0,1) via a logistic so scales compare; the priority
    prior is re-applied so canonical lairs keep their edge. The untouched tail keeps
    its fused order. Returns the input unchanged when no backend is installed.
    """
    if not hits:
        return hits
    backend, model = _load()
    if backend == "none":
        return hits  # graceful no-op — the lbrain[rerank] extra isn't installed
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
