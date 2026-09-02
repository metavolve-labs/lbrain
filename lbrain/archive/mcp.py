"""Tier-2 deep-recall MCP tool (optional subpackage).

Registered onto the core FastMCP server by ``register(mcp)`` only when the archive
extra is installed (this module imports ``.storage`` → ``.crypto`` is not required here,
but core gates registration in a try/except so a missing extra simply omits the tool).
"""

from __future__ import annotations


def register(mcp) -> None:
    """Attach the ``lair_deep_recall`` tool to the core FastMCP server."""

    @mcp.tool()
    def lair_deep_recall(query: str, k: int = 5, namespace: str | None = None) -> str:
        """Deep-recall over the Tier-2 permanent archive: semantic search across snapshots of
        full, encrypted, immutable episodic records (sessions). Returns matching records with
        their txids — fetch the full decrypted record by txid via the `lbrain retrieve` CLI.

        Args:
            query: Natural-language description of the episode/session to recall.
            k: Number of records to surface (default 5).
            namespace: Optional silo filter (e.g. 'private').
        """
        from pathlib import Path

        from .. import amp
        from ..config import CONFIG_DIR, Config
        from ..embed import make_embedder
        from ..epoch import current_epoch_id, open_store
        from .storage import ArchiveStore

        cfg = Config.load()
        # RED-W-BYPASS-1 (CSO 2026-09-01): a direct non-immutable Store() open
        # MATERIALIZED a brain.db at a foreign db_path on a mere read. Route
        # through the engine read path (immutable on epoch homes) and refuse to
        # create on a legacy home whose db is absent.
        if current_epoch_id(Path(CONFIG_DIR)) is None and not Path(cfg.db_path).exists():
            return f"No local archive index (no brain at {cfg.db_path}) — refusing to create one on a read."
        store = open_store(cfg)
        embedder = make_embedder(cfg)
        try:
            astore = ArchiveStore(store.db, store.embedding_dim)
            rows = astore.search_archives(embedder.embed_one(query), k=k, namespace=namespace)
            if not rows:
                return "No archived records matched."
            out = [amp.UNTRUSTED_NOTICE, f"--- {len(rows)} archived record(s) ---\n"]
            for i, r in enumerate(rows, 1):
                out.append(f"[{i}] {r['title']}  (dist={r['dist']:.3f})")
                out.append(f"    txid {r['txid']}  ·  {r['namespace']}  ·  {r['n_bytes']} bytes")
                out.append(f"    {amp.fence(r['snapshot'].strip().replace(chr(10), ' ')[:300])}\n")
            out.append("Fetch a full record: `lbrain retrieve --txid <txid>`")
            return "\n".join(out)
        finally:
            embedder.close()
            store.close()
