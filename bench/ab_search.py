#!/usr/bin/env python3
"""A/B retrieval harness — run a fixed query set against a chosen brain.db and
print a compact ranking signature per query. Run before/after a change and diff.

Usage:
    python bench/ab_search.py [DB_PATH] [LABEL]

Read-only on the DB (search only; no writes). Safe to run against the live brain.
"""
import sys
from pathlib import Path

from lbrain.config import Config
from lbrain.embed import EmbedClient
from lbrain.search import search
from lbrain.store import Store

# Corpus-agnostic: exercises the retrieval characteristics that matter — compound/technical
# tokens, snake_case vs camelCase, multi-word semantic phrases, and supersession negatives.
QUERIES = [
    "hybrid vector keyword RRF fusion ranking",
    "content addressable storage dedup sha256",
    "OAuth token refresh race condition retry backoff",
    "HyperBEAM compute_cached function_clause crash recovery",
    "kubernetes pod eviction OOMKilled recovery",
    "Argon2id crypto-shred encrypted archive envelope",
    "sqlite-vec FTS5 hybrid index rebuild",
    "snake_case schema validation camelCase whitelist",
    "Ebbinghaus forgetting curve memory decay",
    "Arweave UDL v0.2 ANS-110 atomic asset tags",
]


def sig(db_path: str, label: str) -> None:
    cfg = Config.load()
    cfg.db_path = Path(db_path)
    store = Store(cfg.db_path, embedding_dim=cfg.embedding_dim)
    embedder = EmbedClient(cfg.openai_api_key, cfg.embedding_model, cfg.embedding_dim)
    print(f"\n===== {label}  ({db_path}) =====")
    for q in QUERIES:
        hits = search(cfg, store, embedder, q, k=5)
        print(f"\nQ: {q}")
        for i, h in enumerate(hits, 1):
            tag = "★" if h.is_priority else " "
            short = h.rel_path.split("/")[-1]
            print(f"  {i}.{tag} {short} #c{h.chunk_idx}  score={h.score:.4f} "
                  f"v={h.vector_score:.3f} kw={h.keyword_score:.3f} {h.boosts}")
    embedder.close()
    store.close()


if __name__ == "__main__":
    db = sys.argv[1] if len(sys.argv) > 1 else str(Config.load().db_path)
    label = sys.argv[2] if len(sys.argv) > 2 else "run"
    sig(db, label)
