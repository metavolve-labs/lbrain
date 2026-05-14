"""MCP server — exposes LBrain to Claude Code and other MCP clients.

Tools surfaced:
- lair_query     : hybrid semantic+keyword search with Cognitive Nutrition preamble
- lair_search    : exact-keyword FTS5 search (no embedding call)
- lair_protocol_check : "Should I commit this text to a lair?" decision
- lair_check_action   : cross-check a proposed action against feedback rules
- lair_stats     : brain statistics
"""

from __future__ import annotations

import json

from mcp.server.fastmcp import FastMCP

from .config import Config
from .embed import EmbedClient
from .lair_protocol import (
    cognitive_nutrition_preamble,
    detect_anti_pattern,
    should_commit_to_lair,
)
from .search import keyword_only, search
from .store import Store

mcp = FastMCP("lbrain")


@mcp.tool()
def lair_query(query: str, k: int = 8, doc_type: str | None = None, priority_only: bool = False) -> str:
    """Hybrid semantic + keyword search across all lairs and memory.

    Args:
        query: Natural-language question or topic.
        k: Number of results (default 8).
        doc_type: Optional frontmatter type filter — user|feedback|project|reference.
        priority_only: If true, restrict to 000-PRIORITY-* lairs.

    Returns the Cognitive Nutrition preamble (if any triggers fire) plus formatted hits.
    """
    cfg = Config.load()
    store = Store(cfg.db_path, embedding_dim=cfg.embedding_dim)
    embedder = EmbedClient(cfg.openai_api_key, cfg.embedding_model, cfg.embedding_dim)
    try:
        hits = search(cfg, store, embedder, query, k=k, doc_type=doc_type, priority_only=priority_only)
        out = []
        preamble = cognitive_nutrition_preamble(query, hits)
        if preamble:
            out.append(preamble)
        out.append(f"--- {len(hits)} hits ---\n")
        for i, h in enumerate(hits, 1):
            prefix = "★ " if h.is_priority else "  "
            out.append(f"{prefix}[{i}] {h.title}  (score={h.score:.3f})")
            out.append(f"    {h.rel_path} :: chunk {h.chunk_idx}  type={h.doc_type or '?'}")
            preview = h.text.strip().replace("\n", " ")[:300]
            out.append(f"    {preview}\n")
        return "\n".join(out)
    finally:
        embedder.close()
        store.close()


@mcp.tool()
def lair_search(query: str, k: int = 10) -> str:
    """Exact-keyword search using SQLite FTS5. No embedding API call. Fastest path."""
    cfg = Config.load()
    store = Store(cfg.db_path, embedding_dim=cfg.embedding_dim)
    try:
        hits = keyword_only(store, query, k=k)
        out = [f"--- {len(hits)} keyword hits ---\n"]
        for i, h in enumerate(hits, 1):
            out.append(f"  [{i}] {h.title}")
            out.append(f"    {h.rel_path} :: chunk {h.chunk_idx}")
            preview = h.text.strip().replace("\n", " ")[:300]
            out.append(f"    {preview}\n")
        return "\n".join(out)
    finally:
        store.close()


@mcp.tool()
def lair_protocol_check(text: str) -> str:
    """Decide whether a piece of conversation text should become a lair/memory entry.

    Returns a structured suggestion: should_commit, confidence, suggested_type,
    suggested_slug, reasoning.
    """
    sug = should_commit_to_lair(text)
    return json.dumps(
        {
            "should_commit": sug.should_commit,
            "confidence": round(sug.confidence, 2),
            "suggested_type": sug.suggested_type,
            "suggested_slug": sug.suggested_slug,
            "reasoning": sug.reasoning,
        },
        indent=2,
    )


@mcp.tool()
def lair_check_action(action_text: str) -> str:
    """Cross-check a proposed action against saved feedback rules.

    Returns warnings if the action looks like it conflicts with prior user feedback,
    or 'No conflicts' if clean.
    """
    cfg = Config.load()
    store = Store(cfg.db_path, embedding_dim=cfg.embedding_dim)
    embedder = EmbedClient(cfg.openai_api_key, cfg.embedding_model, cfg.embedding_dim)
    try:
        hits = search(cfg, store, embedder, action_text, k=8, doc_type="feedback")
        warnings = detect_anti_pattern(action_text, hits)
        if not warnings:
            return "✓ No conflicts with saved feedback rules."
        return "\n".join(["⚠️ Potential conflicts:"] + [f"  {w}" for w in warnings])
    finally:
        embedder.close()
        store.close()


@mcp.tool()
def lair_stats() -> str:
    """Return brain statistics: doc count, chunk count, embedding coverage, etc."""
    cfg = Config.load()
    store = Store(cfg.db_path, embedding_dim=cfg.embedding_dim)
    try:
        s = store.stats()
        cov = s["embedded"] / max(s["chunks"], 1) * 100
        return (
            f"docs: {s['docs']}\n"
            f"chunks: {s['chunks']}\n"
            f"embedded: {s['embedded']} ({cov:.1f}% coverage)\n"
            f"priority docs: {s['priority_docs']}\n"
            f"wikilinks: {s['wikilinks']}"
        )
    finally:
        store.close()


def serve() -> None:
    mcp.run()


if __name__ == "__main__":
    serve()
