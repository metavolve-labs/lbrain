"""Tier 3 — memory consolidation (the heartbeat).

Clusters related chunks over their existing vectors and synthesizes DENSE,
structured *summary memories*: a neocortical abstraction layer over the raw
episodic fragments. This is the densification principle turned inward — raising the
information density of what retrieval returns, so a query can hit one dense
abstraction instead of a dozen scattered fragments.

Summaries are DERIVATIVE and regenerable. Each carries explicit provenance
(source doc paths + chunk ids), lives in its own layer (the `summaries` table +
`vec_summaries`/`fts_summaries`), and never touches the source markdown — which
remains the single source of truth. Faithfulness over fluency: the synthesis
prompt forbids invention.
"""

from __future__ import annotations

import struct
from collections import Counter

import httpx
import numpy as np

CHAT_URL = "https://api.openai.com/v1/chat/completions"

SYNTH_SYSTEM = (
    "You consolidate related memory fragments into ONE dense, structured summary "
    "memory. Rules: (1) preserve every specific fact, date, decision, name, "
    "identifier, address, and number found in the sources; (2) organize into "
    "clear labeled sections; (3) be information-dense — no filler, no hedging, no "
    "preamble; (4) invent NOTHING — if the fragments don't state it, do not write "
    "it. Output concise markdown. This is a derivative abstraction over the "
    "sources; faithfulness to them is paramount."
)


def _unpack(blob: bytes, dim: int) -> np.ndarray:
    return np.frombuffer(blob, dtype=np.float32, count=dim)


def cluster_chunks(store, dim, distance_threshold=0.45, min_size=4, max_clusters=None):
    """Agglomerative clustering over chunk vectors (cosine, no fixed k).

    Returns clusters (lists of chunk rows) with >= min_size members, largest
    first. Priority chunks are eligible — consolidation spans the whole corpus.
    """
    from sklearn.cluster import AgglomerativeClustering

    rows = store.all_chunk_vectors()
    if len(rows) < min_size:
        return []
    X = np.vstack([_unpack(r["embedding"], dim) for r in rows])
    labels = AgglomerativeClustering(
        n_clusters=None,
        metric="cosine",
        linkage="average",
        distance_threshold=distance_threshold,
    ).fit_predict(X)

    buckets: dict[int, list] = {}
    for r, lab in zip(rows, labels):
        buckets.setdefault(int(lab), []).append(r)
    clusters = [c for c in buckets.values() if len(c) >= min_size]
    clusters.sort(key=len, reverse=True)
    return clusters[:max_clusters] if max_clusters else clusters


def _title_for(members) -> str:
    common = Counter(
        m["rel_path"].rsplit("/", 1)[-1].replace(".md", "") for m in members
    ).most_common(1)[0][0]
    return f"Consolidated · {common}"


def synthesize(api_key: str, model: str, members) -> str:
    frag = "\n\n---\n\n".join(
        f"[source: {m['rel_path']}]\n{m['text']}" for m in members[:24]
    )
    with httpx.Client(timeout=120.0) as client:
        r = client.post(
            CHAT_URL,
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": model,
                "temperature": 0.2,
                "messages": [
                    {"role": "system", "content": SYNTH_SYSTEM},
                    {
                        "role": "user",
                        "content": f"Consolidate these {len(members)} related "
                        f"fragments into one dense summary memory:\n\n{frag}",
                    },
                ],
            },
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"].strip()


def consolidate(
    cfg, store, embedder, synth_model="gpt-4o-mini",
    distance_threshold=0.45, min_size=4, max_clusters=20, log=print,
):
    """Cluster → synthesize → store. Returns the number of summaries written.

    Clears the prior summary layer first (summaries are fully regenerable). Each
    summary is embedded into vec_summaries so it is retrievable as an abstraction.
    """
    import time as _time

    clusters = cluster_chunks(store, cfg.embedding_dim, distance_threshold, min_size, max_clusters)
    log(f"  clusters formed (>= {min_size} chunks): {len(clusters)}")
    if not clusters:
        return 0

    store.clear_summaries()
    now = _time.time()
    made = 0
    for i, members in enumerate(clusters, 1):
        paths = sorted({m["rel_path"] for m in members})
        # Skip degenerate clusters that are just one doc talking to itself.
        if len(paths) < 2:
            continue
        text = synthesize(cfg.openai_api_key, synth_model, members)
        title = _title_for(members)
        cids = [m["chunk_id"] for m in members]
        sid = store.insert_summary(title, text, paths, cids, now)
        store.write_summary_embedding(sid, embedder.embed_one(text))
        made += 1
        log(f"    [{i}/{len(clusters)}] {title} — {len(members)} fragments across {len(paths)} docs")
    store.db.commit()
    return made
