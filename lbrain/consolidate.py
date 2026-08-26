"""Memory Consolidation & Abstraction layer (v2, hardened).

Groups related chunks via a fast, native Leader Clustering pass over the
stored embeddings, then synthesizes each cluster into a dense "abstraction"
memory using an LLM. Original design by Gemini (2026-07-09); hardened per
hardened following an internal design review.

Design invariants:
- GATED OUTPUT: abstractions are written to ~/.lbrain/abstractions/, which
  is NOT a configured source. They enter retrieval only if the user
  deliberately adds that directory as a source and imports it — the same
  call-when-needed pattern as rerank/recency (2026-06-08 doctrine). The
  predecessor (Tier-3 consolidation) measured net-negative in the 4-regime
  A/B; v2 stays off-path until it measures net-positive.
- NO SELF-FEEDING: chunks that came from abstraction files are excluded
  from clustering input, so re-runs never synthesize
  abstractions-of-abstractions.
- IDEMPOTENT: each cluster gets a stable content signature; a cluster whose
  abstraction file already exists is skipped, so re-runs only pay for new
  or changed clusters.
"""

import hashlib
import json
import math
import os
import struct
import time
from pathlib import Path

import httpx

from .config import CONFIG_DIR, Config
from .store import Store

# Output home — deliberately OUTSIDE every source tree (never auto-indexed,
# never inside a git repo). Serving is an explicit opt-in.
ABSTRACTIONS_DIR = CONFIG_DIR / "abstractions"

# Verified against the live model registry 2026-07-10 (the bare
# "models/gemini-3.1-pro" name 404s on generateContent for this key).
# For cheap sampled runs, pass --model models/gemini-3.5-flash.
DEFAULT_MODEL = "models/gemini-3.1-pro-preview"

# rel_path patterns that identify abstraction-derived chunks (current naming,
# the 2026-07-09 prototype naming, and anything under an abstractions folder).
_SELF_EXCLUDE = (
    "%.lbrain-abstractions%",
    "abstraction-%",
    "%/abstraction-%",
    "abstraction_%",
    "%/abstraction_%",
)


def source_date(rel_path: str, mtime: float | None = None) -> str:
    """Best-available date label for a source, so the synthesizer can anchor
    time-bound facts ('as of <date>'). Editorial dates in filenames beat file
    mtime (which drifts on rewrites)."""
    import re
    m = re.findall(r"\d{4}-\d{2}-\d{2}", rel_path)
    if m:
        return f"dated {m[-1]}"
    m = re.findall(r"\d{4}-\d{2}(?!\d)", rel_path)
    if m:
        return f"dated {m[-1]}"
    if mtime:
        return f"last modified {time.strftime('%Y-%m-%d', time.localtime(mtime))}"
    return "date unknown"


def get_all_embedded_chunks(store: Store) -> list[dict]:
    """All embedded chunks EXCEPT abstraction-derived ones (no self-feeding)."""
    where = " AND ".join("c.rel_path NOT LIKE ?" for _ in _SELF_EXCLUDE)
    rows = store.db.execute(
        "SELECT c.chunk_id, c.text, c.rel_path, d.mtime, v.embedding "
        "FROM chunks c JOIN vec_chunks v ON c.chunk_id = v.rowid "
        "JOIN docs d ON d.rel_path = c.rel_path "
        f"WHERE {where}",
        _SELF_EXCLUDE,
    ).fetchall()

    chunks = []
    for r in rows:
        blob = r["embedding"]
        if not blob:
            continue
        vec = struct.unpack(f"<{len(blob)//4}f", blob)
        norm = math.sqrt(sum(a * a for a in vec))
        if norm == 0:
            continue
        chunks.append({
            "chunk_id": r["chunk_id"],
            "text": r["text"],
            "rel_path": r["rel_path"],
            "date": source_date(r["rel_path"], r["mtime"]),
            # Unit-normalize once so clustering is dot-product only.
            "vector": [a / norm for a in vec],
        })
    return chunks


def cluster_chunks(chunks: list[dict], threshold=0.92, max_cluster_size=20) -> list[dict]:
    """Leader Clustering: fast, single-pass O(N*K).

    Chunk vectors are unit-normalized, so similarity against a cluster is
    dot(vec, centroid_sum) / |centroid_sum| — no per-comparison sqrt over
    members, no renormalization drift.
    """
    clusters = []
    for chunk in chunks:
        placed = False
        vec = chunk["vector"]
        for c in clusters:
            if len(c["chunks"]) >= max_cluster_size:
                continue
            sim = sum(a * b for a, b in zip(vec, c["centroid_sum"])) / c["centroid_norm"]
            if sim >= threshold:
                c["chunks"].append(chunk)
                c["centroid_sum"] = [s + v for s, v in zip(c["centroid_sum"], vec)]
                c["centroid_norm"] = math.sqrt(sum(s * s for s in c["centroid_sum"])) or 1.0
                placed = True
                break
        if not placed:
            clusters.append({
                "centroid_sum": list(vec),
                "centroid_norm": 1.0,  # unit vector
                "chunks": [chunk],
            })

    # Single-chunk clusters need no abstraction.
    return [c for c in clusters if len(c["chunks"]) > 1]


def cluster_signature(cluster_chunks: list[dict]) -> str:
    """Stable content signature: survives re-imports (chunk_ids change,
    text does not). Same member texts -> same signature -> skip on re-run."""
    h = hashlib.sha256()
    for th in sorted(
        hashlib.sha256(c["text"].encode("utf-8")).hexdigest()[:16]
        for c in cluster_chunks
    ):
        h.update(th.encode("ascii"))
    return h.hexdigest()[:12]


def sanitize_wikilinks(text: str, source_paths: list[str]) -> tuple[str, int]:
    """Deterministically strip [[wikilinks]] whose target is not one of the
    cluster's source documents (full rel_path, basename, or stem) — LLM-emitted
    links feed LBrain's ranking boosts, so hallucinated targets are not cosmetic."""
    import re
    allowed = set()
    for sp in source_paths:
        p = Path(sp)
        allowed.update(x.lower() for x in (sp, p.name, p.stem))

    removed = 0

    def _check(m):
        nonlocal removed
        target = m.group(1).strip()
        if target.lower() in allowed:
            return m.group(0)
        removed += 1
        return target

    return re.sub(r"\[\[([^\]]+)\]\]", _check, text), removed


def is_claude_model(model: str) -> bool:
    """Claude models route to Vertex; anything else to the Gemini Developer API."""
    return model.startswith("claude-")


INSTRUCTION = (
    "You are LBrain's cognitive consolidation engine. Synthesize the following memory fragments into a single, "
    "dense, coherent abstraction. Focus on extracting the highest-signal principles, facts, or architecture decisions. "
    "Filter out noise. Ensure you synthesize the information, do not just list it. "
    "Ground every statement in the fragments — do not add facts that are not present in them. "
    "Each fragment header carries its source date: anchor EVERY time-bound fact to it as a point-in-time observation "
    "(e.g. 'as of 2026-05-22, ...'), never as current state — plans, statuses, deadlines, and open items are all time-bound. "
    "Begin directly with a heading naming the topic — no preamble like 'Here is the synthesis'.\n"
    "Crucially: Embed wikilinks back to the source documents referenced where appropriate, for example [[filename]] — "
    "but ONLY for names that appear in the fragment Source paths above."
)


def synthesize_cluster_vertex(cluster_chunks: list[dict], *, model: str,
                              project_id: str, region: str = "global",
                              effort: str = "medium", max_tokens: int = 8192) -> str:
    """Synthesize via Claude on Google Vertex AI.

    Deliberately in-perimeter: Vertex authenticates with GCP ADC/service-account
    credentials, so this path needs no Gemini Developer API key and is unaffected by
    that surface's monthly spending cap — which exists as breach containment and must
    not be raised to run a batch job.

    Three model-specific constraints, each of which is a 400 or a silent defect if
    ignored on Claude Opus 5:
      * NO sampling parameters. ``temperature``/``top_p``/``top_k`` are rejected.
      * Thinking is ON by default and shares the ``max_tokens`` budget with the
        response, so the budget must cover both or output truncates mid-sentence.
        ``effort`` is the cost lever, not a sampling knob.
      * A response can come back ``stop_reason == "refusal"`` with empty content.
        That is a normal HTTP 200, so ``content[0]`` must never be read blind.
    """
    from anthropic import AnthropicVertex

    client = AnthropicVertex(project_id=project_id, region=region)
    resp = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=INSTRUCTION,
        output_config={"effort": effort},
        messages=[{"role": "user", "content": _build_prompt_fragments(cluster_chunks)}],
    )
    if resp.stop_reason == "refusal":
        cat = getattr(getattr(resp, "stop_details", None), "category", None)
        raise RuntimeError(f"synthesis refused by safety classifier (category={cat})")
    text = "".join(b.text for b in resp.content if b.type == "text").strip()
    if not text:
        raise RuntimeError(f"empty synthesis (stop_reason={resp.stop_reason})")
    return text


def _build_prompt_fragments(cluster_chunks: list[dict]) -> str:
    """Just the fragments — the instruction rides in ``system`` on the Vertex path."""
    texts = []
    for i, c in enumerate(cluster_chunks):
        date = c.get("date", "date unknown")
        texts.append(f"--- Fragment {i+1} (Source: {c['rel_path']}; {date}) ---\n{c['text']}")
    return "\n\n".join(texts)


def synthesize_cluster(api_key: str, cluster_chunks: list[dict], model=DEFAULT_MODEL) -> str:
    texts = []
    for i, c in enumerate(cluster_chunks):
        date = c.get("date", "date unknown")
        texts.append(f"--- Fragment {i+1} (Source: {c['rel_path']}; {date}) ---\n{c['text']}")

    prompt = (
        "You are LBrain's cognitive consolidation engine. Synthesize the following memory fragments into a single, "
        "dense, coherent abstraction. Focus on extracting the highest-signal principles, facts, or architecture decisions. "
        "Filter out noise. Ensure you synthesize the information, do not just list it. "
        "Ground every statement in the fragments — do not add facts that are not present in them. "
        "Each fragment header carries its source date: anchor EVERY time-bound fact to it as a point-in-time observation "
        "(e.g. 'as of 2026-05-22, ...'), never as current state — plans, statuses, deadlines, and open items are all time-bound. "
        "Begin directly with a heading naming the topic — no preamble like 'Here is the synthesis'.\n"
        "Crucially: Embed wikilinks back to the source documents referenced where appropriate, for example [[filename]] — "
        "but ONLY for names that appear in the fragment Source paths above.\n\n"
        + "\n\n".join(texts)
    )

    url = f"https://generativelanguage.googleapis.com/v1beta/{model}:generateContent"
    headers = {"x-goog-api-key": api_key}
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.2},
    }

    with httpx.Client(timeout=120.0) as client:
        r = client.post(url, headers=headers, json=payload)
        r.raise_for_status()
        return r.json()["candidates"][0]["content"]["parts"][0]["text"]


def run_consolidation(
    cfg: Config,
    store: Store,
    threshold=0.92,
    model=DEFAULT_MODEL,
    limit=0,
    dry_run=False,
):
    """Returns (generated, skipped_existing, total_clusters)."""
    import click

    use_vertex = is_claude_model(model)
    vertex_project = os.environ.get("ANTHROPIC_VERTEX_PROJECT_ID", "")
    vertex_region = os.environ.get("ANTHROPIC_VERTEX_REGION", "global")
    api_key = "" if use_vertex else (cfg.gemini_api_key or os.environ.get("GEMINI_API_KEY"))
    if not dry_run:
        if use_vertex and not vertex_project:
            # Fail loud and empty rather than defaulting to a plausible project id —
            # a default that wires an external side effect is a bug, not a convenience.
            raise RuntimeError(
                "ANTHROPIC_VERTEX_PROJECT_ID is required for Claude-on-Vertex synthesis. "
                "Authenticate with `gcloud auth application-default login` (no API key needed)."
            )
        if not use_vertex and not api_key:
            raise RuntimeError("GEMINI_API_KEY is required for synthesis. Set it via lbrain init or environment.")

    click.echo("  Fetching vectors (abstraction-derived chunks excluded)...")
    chunks = get_all_embedded_chunks(store)
    if not chunks:
        click.echo("  No embedded chunks found.")
        return 0, 0, 0

    click.echo(f"  Clustering {len(chunks)} chunks (threshold={threshold})...")
    clusters = cluster_chunks(chunks, threshold=threshold)
    sizes = sorted((len(c["chunks"]) for c in clusters), reverse=True)
    click.echo(f"  Found {len(clusters)} viable clusters (size > 1; largest: {sizes[:5]}).")

    ABSTRACTIONS_DIR.mkdir(parents=True, exist_ok=True)

    generated = skipped = 0
    for i, cluster in enumerate(clusters):
        sig = cluster_signature(cluster["chunks"])
        file_path = ABSTRACTIONS_DIR / f"abstraction-{sig}.md"
        if file_path.exists():
            skipped += 1
            continue
        if dry_run:
            continue
        if limit and generated >= limit:
            break

        click.echo(f"  Synthesizing cluster {i+1}/{len(clusters)} ({len(cluster['chunks'])} chunks) -> {file_path.name}")
        try:
            if use_vertex:
                summary = synthesize_cluster_vertex(
                    cluster["chunks"], model=model,
                    project_id=vertex_project, region=vertex_region,
                )
            else:
                summary = synthesize_cluster(api_key, cluster["chunks"], model=model)
        except Exception as e:
            click.echo(f"  Failed to synthesize cluster {i+1}: {e}")
            continue

        sources = sorted(set(c["rel_path"] for c in cluster["chunks"]))
        summary, stripped = sanitize_wikilinks(summary, sources)
        if stripped:
            click.echo(f"    stripped {stripped} unsourced wikilink(s)")
        first_line = next((ln.strip("# ").strip() for ln in summary.splitlines() if ln.strip()), "")
        content = (
            "---\n"
            f"name: abstraction-{sig}\n"
            f"description: {first_line[:120]}\n"
            "type: abstraction\n"
            f"generated_by: lbrain consolidate ({model})\n"
            f"generated: {time.strftime('%Y-%m-%d')}\n"
            f"sources: {json.dumps(sources)}\n"
            "---\n\n"
            f"{summary}\n"
        )
        file_path.write_text(content, encoding="utf-8")
        generated += 1

    return generated, skipped, len(clusters)
