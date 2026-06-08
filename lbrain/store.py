"""SQLite + sqlite-vec + FTS5 store. Single file. Native. Fast."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

import sqlite_vec

from .index import Chunk, Doc

SCHEMA = """
CREATE TABLE IF NOT EXISTS docs (
    rel_path TEXT PRIMARY KEY,
    abs_path TEXT NOT NULL,
    title    TEXT NOT NULL,
    doc_hash TEXT NOT NULL,
    mtime    REAL NOT NULL,
    is_priority INTEGER NOT NULL DEFAULT 0,
    doc_type TEXT NOT NULL DEFAULT '',
    metadata TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS chunks (
    chunk_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    rel_path   TEXT NOT NULL,
    chunk_idx  INTEGER NOT NULL,
    text       TEXT NOT NULL,
    token_count INTEGER NOT NULL,
    chunk_hash TEXT NOT NULL,
    embedded   INTEGER NOT NULL DEFAULT 0,
    context    TEXT NOT NULL DEFAULT '',
    UNIQUE(rel_path, chunk_idx),
    FOREIGN KEY (rel_path) REFERENCES docs(rel_path) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS wikilinks (
    src_path TEXT NOT NULL,
    tgt_slug TEXT NOT NULL,
    PRIMARY KEY (src_path, tgt_slug),
    FOREIGN KEY (src_path) REFERENCES docs(rel_path) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS supersessions (
    src_path TEXT NOT NULL,
    tgt_slug TEXT NOT NULL,
    PRIMARY KEY (src_path, tgt_slug),
    FOREIGN KEY (src_path) REFERENCES docs(rel_path) ON DELETE CASCADE
);

-- Key/value metadata. Records the embedding fingerprint (dim/model/provider) the
-- vectors were built with, so a later config change can't silently corrupt the space.
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS archives (
    archive_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    txid          TEXT NOT NULL UNIQUE,
    snapshot_txid TEXT NOT NULL DEFAULT '',
    namespace     TEXT NOT NULL DEFAULT 'private',
    title         TEXT NOT NULL DEFAULT '',
    snapshot      TEXT NOT NULL DEFAULT '',
    tags          TEXT NOT NULL DEFAULT '{}',
    n_bytes       INTEGER NOT NULL DEFAULT 0,
    created       REAL NOT NULL DEFAULT 0,
    transport     TEXT NOT NULL DEFAULT 'local',
    source_hash   TEXT NOT NULL DEFAULT '',
    shredded      INTEGER NOT NULL DEFAULT 0,
    embedded      INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_wikilinks_tgt ON wikilinks(tgt_slug);
CREATE INDEX IF NOT EXISTS idx_supersessions_tgt ON supersessions(tgt_slug);
CREATE INDEX IF NOT EXISTS idx_chunks_embedded ON chunks(embedded);
"""


class Store:
    def __init__(self, db_path: Path, embedding_dim: int = 1536):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path
        self.embedding_dim = embedding_dim
        self.db = sqlite3.connect(str(db_path))
        # Concurrency hardening — without these, the long-lived MCP server and a
        # concurrent CLI/cron writer collide: default rollback journal serializes
        # readers/writers and the default busy_timeout of 0 raises "database is
        # locked" instantly. WAL lets a reader coexist with a writer; busy_timeout
        # makes contenders wait instead of erroring; foreign_keys enforces the
        # ON DELETE CASCADE the schema already declares.
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.enable_load_extension(True)
        sqlite_vec.load(self.db)
        self.db.enable_load_extension(False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        # Idempotent migrations for DBs created before later columns existed.
        chunk_cols = {r["name"] for r in self.db.execute("PRAGMA table_info(chunks)")}
        if "context" not in chunk_cols:
            self.db.execute("ALTER TABLE chunks ADD COLUMN context TEXT NOT NULL DEFAULT ''")
        arch_cols = {r["name"] for r in self.db.execute("PRAGMA table_info(archives)")}
        if arch_cols and "source_hash" not in arch_cols:
            self.db.execute("ALTER TABLE archives ADD COLUMN source_hash TEXT NOT NULL DEFAULT ''")
        if arch_cols:  # column now guaranteed (fresh schema or just-migrated) — index idempotently
            self.db.execute("CREATE INDEX IF NOT EXISTS idx_archives_source ON archives(source_hash)")
        self.db.execute(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS vec_chunks USING vec0(embedding float[{embedding_dim}])"
        )
        # FTS5 keyword index
        self.db.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS fts_chunks USING fts5("
            "text, rel_path UNINDEXED, chunk_idx UNINDEXED, tokenize='porter unicode61')"
        )
        # Tier-2 archive layer — snapshots of permanent encrypted episodic records.
        # Siloed from the main retrieval path (its own vec/FTS); reached via deep-recall.
        self.db.execute(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS vec_archives USING vec0(embedding float[{embedding_dim}])"
        )
        self.db.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS fts_archives USING fts5("
            "snapshot, title UNINDEXED, txid UNINDEXED, tokenize='porter unicode61')"
        )
        self.db.commit()

    @contextmanager
    def transaction(self):
        try:
            yield
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise

    # ---------- embedding-config fingerprint (silent-corruption guard) ----------

    def get_meta(self, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value) -> None:
        self.db.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(value)),
        )
        self.db.commit()

    def embedding_config_status(self, dim: int, model: str, provider: str) -> str:
        """Compare the live embedding config against the one the stored vectors
        were built with. Returns 'unset' (no vectors yet), 'match', 'model_changed'
        (same dim, different model/provider — old vectors live in a different space),
        or 'dim_changed' (vector width differs — the vec tables must be rebuilt)."""
        stored_dim = self.get_meta("embedding_dim")
        if stored_dim is None:
            return "unset"
        if int(stored_dim) != int(dim):
            return "dim_changed"
        if (self.get_meta("embedding_model"), self.get_meta("embedding_provider")) != (model, provider):
            return "model_changed"
        return "match"

    def stamp_embedding_config(self, dim: int, model: str, provider: str) -> None:
        """Record the fingerprint of the vectors currently in the store."""
        self.set_meta("embedding_dim", dim)
        self.set_meta("embedding_model", model)
        self.set_meta("embedding_provider", provider)

    def reset_vectors(self, dim: int) -> None:
        """Drop + recreate the vector tables at a new dimension and mark every
        chunk/archive un-embedded. Required when the embedding dim changes
        (vec0 column width is fixed at creation); a full re-embed must follow."""
        for name in ("vec_chunks", "vec_archives"):
            self.db.execute(f"DROP TABLE IF EXISTS {name}")
            self.db.execute(f"CREATE VIRTUAL TABLE {name} USING vec0(embedding float[{dim}])")
        self.db.execute("UPDATE chunks SET embedded = 0")
        self.db.execute("UPDATE archives SET embedded = 0")
        self.embedding_dim = dim
        self.db.commit()

    # ---------- docs ----------

    def get_doc_hash(self, rel_path: str) -> str | None:
        row = self.db.execute(
            "SELECT doc_hash FROM docs WHERE rel_path = ?", (rel_path,)
        ).fetchone()
        return row["doc_hash"] if row else None

    def upsert_doc(self, doc: Doc) -> None:
        import json

        self.db.execute(
            "INSERT INTO docs (rel_path, abs_path, title, doc_hash, mtime, is_priority, doc_type, metadata) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(rel_path) DO UPDATE SET abs_path=excluded.abs_path, title=excluded.title, "
            "doc_hash=excluded.doc_hash, mtime=excluded.mtime, is_priority=excluded.is_priority, "
            "doc_type=excluded.doc_type, metadata=excluded.metadata",
            (
                doc.rel_path,
                str(doc.path),
                doc.title,
                doc.doc_hash,
                doc.mtime,
                int(doc.is_priority),
                doc.doc_type,
                json.dumps(_safe_meta(doc.metadata)),
            ),
        )

    def delete_doc_chunks(self, rel_path: str) -> None:
        # Need to remove from vec_chunks too (FK only on chunks)
        chunk_ids = [
            r["chunk_id"]
            for r in self.db.execute("SELECT chunk_id FROM chunks WHERE rel_path = ?", (rel_path,))
        ]
        for cid in chunk_ids:
            self.db.execute("DELETE FROM vec_chunks WHERE rowid = ?", (cid,))
        self.db.execute("DELETE FROM fts_chunks WHERE rel_path = ?", (rel_path,))
        self.db.execute("DELETE FROM chunks WHERE rel_path = ?", (rel_path,))

    def prune_missing(
        self,
        source_roots: list | None = None,
        max_fraction: float = 0.5,
        force: bool = False,
    ) -> list[str]:
        """Drop docs whose source file no longer exists on disk — and their
        chunks, vectors, FTS rows, and wikilinks. (vec_chunks has no FK, so we
        delete every dependent row explicitly.) Returns the pruned rel_paths.

        Two safety guards against the catastrophic "an unmounted source dir looks
        like every file vanished, so nuke the whole index" failure mode:
          - if any provided source_root is itself missing, prune NOTHING (the mount
            is gone, not the docs);
          - refuse to prune more than ``max_fraction`` of the corpus unless ``force``."""
        import os

        if source_roots:
            for root in source_roots:
                if not os.path.isdir(str(root)):
                    return []  # a source root vanished → mount gone, not docs; skip prune

        rows = self.db.execute("SELECT rel_path, abs_path FROM docs").fetchall()
        gone = [r["rel_path"] for r in rows if not os.path.exists(r["abs_path"])]
        if gone and not force and rows and len(gone) / len(rows) > max_fraction:
            raise RuntimeError(
                f"prune would remove {len(gone)}/{len(rows)} docs "
                f"(>{int(max_fraction * 100)}%) — refusing. A source directory is "
                "probably unmounted. Re-run with --force-prune to override."
            )
        for rel in gone:
            self.delete_doc_chunks(rel)
            self.db.execute("DELETE FROM wikilinks WHERE src_path = ?", (rel,))
            self.db.execute("DELETE FROM supersessions WHERE src_path = ?", (rel,))
            self.db.execute("DELETE FROM docs WHERE rel_path = ?", (rel,))
        return gone

    def replace_wikilinks(self, doc: Doc) -> None:
        self.db.execute("DELETE FROM wikilinks WHERE src_path = ?", (doc.rel_path,))
        for tgt in doc.wikilinks:
            self.db.execute(
                "INSERT OR IGNORE INTO wikilinks (src_path, tgt_slug) VALUES (?, ?)",
                (doc.rel_path, tgt),
            )

    def replace_supersessions(self, doc: Doc) -> None:
        """Record the slugs this doc declares it supersedes. Idempotent per doc
        (mirrors replace_wikilinks) so it stays correct on re-import even when the
        doc's chunks are unchanged — and clears the edge if the marker is removed."""
        self.db.execute("DELETE FROM supersessions WHERE src_path = ?", (doc.rel_path,))
        for tgt in getattr(doc, "supersedes", []):
            self.db.execute(
                "INSERT OR IGNORE INTO supersessions (src_path, tgt_slug) VALUES (?, ?)",
                (doc.rel_path, tgt),
            )

    def superseded_slugs(self) -> set[str]:
        """Slugs some other doc explicitly supersedes — buried at retrieval so the
        live truth surfaces while the original stays indexed for provenance."""
        return {
            r["tgt_slug"]
            for r in self.db.execute("SELECT DISTINCT tgt_slug FROM supersessions")
        }

    def insert_chunks(self, chunks: list[Chunk]) -> list[int]:
        ids: list[int] = []
        for c in chunks:
            cur = self.db.execute(
                "INSERT INTO chunks (rel_path, chunk_idx, text, token_count, chunk_hash, context) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (c.doc_path, c.chunk_idx, c.text, c.token_count, c.chunk_hash, c.context),
            )
            chunk_id = cur.lastrowid
            ids.append(chunk_id)
            # FTS indexes context+text so a chunk is findable under its doc's
            # subject even when the chunk body never restates it. Display still
            # reads the raw `text` column, so previews stay clean.
            fts_text = f"{c.context}\n{c.text}" if c.context else c.text
            self.db.execute(
                "INSERT INTO fts_chunks (rowid, text, rel_path, chunk_idx) VALUES (?, ?, ?, ?)",
                (chunk_id, fts_text, c.doc_path, c.chunk_idx),
            )
        return ids

    # Embed text = context + text when contextualized, else text. Used so the
    # vector embedding carries the doc macro-context without touching display.
    EMBED_TEXT_SQL = "CASE WHEN context != '' THEN context || char(10) || text ELSE text END"

    def stale_chunks(self) -> list[tuple[int, str]]:
        rows = self.db.execute(
            f"SELECT chunk_id, {self.EMBED_TEXT_SQL} AS etext "
            "FROM chunks WHERE embedded = 0 ORDER BY chunk_id"
        ).fetchall()
        return [(r["chunk_id"], r["etext"]) for r in rows]

    def write_embeddings(self, chunk_ids: list[int], blobs: list[bytes]) -> None:
        # Guard against a truncated/short provider response silently embedding
        # fewer chunks than requested (zip() would otherwise drop the tail with no
        # error). Both the count and each blob's byte-width must match exactly.
        if len(chunk_ids) != len(blobs):
            raise ValueError(
                f"embedding count mismatch: {len(chunk_ids)} chunks vs {len(blobs)} vectors"
            )
        expected = self.embedding_dim * 4  # little-endian f32
        for cid, blob in zip(chunk_ids, blobs):
            if len(blob) != expected:
                raise ValueError(
                    f"embedding for chunk {cid} is {len(blob)} bytes, expected {expected} "
                    f"({self.embedding_dim}-dim f32)"
                )
        # sqlite-vec virtual tables don't support UPSERT; use DELETE+INSERT
        for cid, blob in zip(chunk_ids, blobs):
            self.db.execute("DELETE FROM vec_chunks WHERE rowid = ?", (cid,))
            self.db.execute(
                "INSERT INTO vec_chunks (rowid, embedding) VALUES (?, ?)",
                (cid, blob),
            )
            self.db.execute("UPDATE chunks SET embedded = 1 WHERE chunk_id = ?", (cid,))

    # ---------- Tier-2 archive layer (permanent verifiable episodic memory) ----------

    def get_archive_by_source(self, source_hash: str):
        """Find a live (non-shredded) archive by the stable hash of its PLAINTEXT
        content. Ciphertext is non-deterministic (random DEK/nonce per encrypt), so
        idempotent capture must dedup on the source hash, not the txid."""
        if not source_hash:
            return None
        return self.db.execute(
            "SELECT * FROM archives WHERE source_hash = ? AND shredded = 0 LIMIT 1",
            (source_hash,),
        ).fetchone()

    def insert_archive(self, *, txid, namespace, title, snapshot, tags, n_bytes,
                       created, transport, snapshot_txid="", source_hash="") -> int:
        """Mirror a permanent archive's snapshot into the index (the card-catalog
        entry). Idempotent on txid — re-archiving identical content updates in place."""
        import json

        cur = self.db.execute(
            "INSERT INTO archives (txid, snapshot_txid, namespace, title, snapshot, tags, "
            "n_bytes, created, transport, source_hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(txid) DO UPDATE SET snapshot=excluded.snapshot, title=excluded.title, "
            "tags=excluded.tags, namespace=excluded.namespace, snapshot_txid=excluded.snapshot_txid, "
            "source_hash=excluded.source_hash, shredded=0, embedded=0",
            (txid, snapshot_txid, namespace, title, snapshot,
             json.dumps(tags), n_bytes, created, transport, source_hash),
        )
        # Refresh the FTS row for this txid.
        self.db.execute("DELETE FROM fts_archives WHERE txid = ?", (txid,))
        self.db.execute(
            "INSERT INTO fts_archives (rowid, snapshot, title, txid) VALUES "
            "((SELECT archive_id FROM archives WHERE txid = ?), ?, ?, ?)",
            (txid, snapshot, title, txid),
        )
        self.db.commit()
        return cur.lastrowid or self.db.execute(
            "SELECT archive_id FROM archives WHERE txid = ?", (txid,)
        ).fetchone()["archive_id"]

    def write_archive_embedding(self, txid: str, blob: bytes) -> None:
        row = self.db.execute(
            "SELECT archive_id FROM archives WHERE txid = ?", (txid,)
        ).fetchone()
        if not row:
            return
        aid = row["archive_id"]
        self.db.execute("DELETE FROM vec_archives WHERE rowid = ?", (aid,))
        self.db.execute("INSERT INTO vec_archives (rowid, embedding) VALUES (?, ?)", (aid, blob))
        self.db.execute("UPDATE archives SET embedded = 1 WHERE archive_id = ?", (aid,))
        self.db.commit()

    def search_archives(self, q_vec: bytes, k: int = 5, namespace: str | None = None) -> list:
        """Semantic recall over archive snapshots (the read surface of Tier-2).
        Shredded archives are excluded — the snapshot survives but the record is gone."""
        rows = self.db.execute(
            "SELECT a.archive_id, a.txid, a.title, a.snapshot, a.namespace, a.created, "
            "       a.n_bytes, a.shredded, a.transport, "
            "       vec_distance_cosine(v.embedding, ?) AS dist "
            "FROM vec_archives v JOIN archives a ON a.archive_id = v.rowid "
            "WHERE v.embedding MATCH ? AND k = ? ORDER BY dist",
            (q_vec, q_vec, max(k * 3, 15)),
        ).fetchall()
        out = []
        for r in rows:
            if r["shredded"]:
                continue
            if namespace and r["namespace"] != namespace:
                continue
            out.append(r)
            if len(out) >= k:
                break
        return out

    def get_archive(self, txid: str):
        return self.db.execute("SELECT * FROM archives WHERE txid = ?", (txid,)).fetchone()

    def mark_archive_shredded(self, txid: str, purge_snapshot: bool = True) -> None:
        """Mark an archive crypto-shredded (its key has been destroyed).

        With ``purge_snapshot`` (the default — "hard" shred), also erase the local
        cleartext snapshot and its FTS + vector rows, so NOTHING readable about the
        record survives locally — only an audit stub (txid, title label, dates, the
        shredded flag). Otherwise ("soft" shred) the snapshot is kept for browsing
        while the on-chain payload is already unrecoverable (key gone)."""
        row = self.db.execute(
            "SELECT archive_id FROM archives WHERE txid = ?", (txid,)
        ).fetchone()
        if not row:
            return
        aid = row["archive_id"]
        self.db.execute("DELETE FROM vec_archives WHERE rowid = ?", (aid,))
        if purge_snapshot:
            self.db.execute("DELETE FROM fts_archives WHERE rowid = ?", (aid,))
            self.db.execute(
                "UPDATE archives SET shredded = 1, embedded = 0, snapshot = '', tags = '{}' "
                "WHERE archive_id = ?",
                (aid,),
            )
        else:
            self.db.execute("UPDATE archives SET shredded = 1 WHERE archive_id = ?", (aid,))
        self.db.commit()

    def list_archives(self, namespace: str | None = None) -> list:
        if namespace:
            return self.db.execute(
                "SELECT txid, title, namespace, n_bytes, created, shredded, transport "
                "FROM archives WHERE namespace = ? ORDER BY created DESC", (namespace,)
            ).fetchall()
        return self.db.execute(
            "SELECT txid, title, namespace, n_bytes, created, shredded, transport "
            "FROM archives ORDER BY created DESC"
        ).fetchall()

    # ---------- counts / health ----------

    def stats(self) -> dict:
        out = {}
        out["docs"] = self.db.execute("SELECT COUNT(*) AS n FROM docs").fetchone()["n"]
        out["chunks"] = self.db.execute("SELECT COUNT(*) AS n FROM chunks").fetchone()["n"]
        # Coverage = chunks with a REAL vector in vec_chunks, not just the
        # embedded=1 flag. The flag and the vector table can drift (a killed
        # process between INSERT and UPDATE, a dropped vec table); reporting the
        # flag would let "100% coverage" lie about missing vectors.
        out["embedded"] = self.db.execute(
            "SELECT COUNT(*) AS n FROM chunks c "
            "WHERE EXISTS (SELECT 1 FROM vec_chunks v WHERE v.rowid = c.chunk_id)"
        ).fetchone()["n"]
        out["embedded_flagged"] = self.db.execute(
            "SELECT COUNT(*) AS n FROM chunks WHERE embedded = 1"
        ).fetchone()["n"]
        out["wikilinks"] = self.db.execute("SELECT COUNT(*) AS n FROM wikilinks").fetchone()["n"]
        out["priority_docs"] = self.db.execute(
            "SELECT COUNT(*) AS n FROM docs WHERE is_priority = 1"
        ).fetchone()["n"]
        out["archives"] = self.db.execute(
            "SELECT COUNT(*) AS n FROM archives WHERE shredded = 0"
        ).fetchone()["n"]
        return out

    def close(self) -> None:
        self.db.close()


def _safe_meta(meta: dict) -> dict:
    """Strip non-JSON-serializable values from frontmatter."""
    out = {}
    for k, v in meta.items():
        if isinstance(v, (str, int, float, bool, list, dict)) or v is None:
            out[k] = v
        else:
            out[k] = str(v)
    return out
