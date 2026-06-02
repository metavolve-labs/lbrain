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
    metadata TEXT NOT NULL DEFAULT '{}',
    last_retrieved REAL NOT NULL DEFAULT 0,
    retrieval_count INTEGER NOT NULL DEFAULT 0
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

CREATE TABLE IF NOT EXISTS associations (
    a_path   TEXT NOT NULL,
    b_path   TEXT NOT NULL,
    strength REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (a_path, b_path)
);

CREATE TABLE IF NOT EXISTS summaries (
    summary_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    title            TEXT NOT NULL,
    text             TEXT NOT NULL,
    source_paths     TEXT NOT NULL DEFAULT '[]',
    source_chunk_ids TEXT NOT NULL DEFAULT '[]',
    n_sources        INTEGER NOT NULL DEFAULT 0,
    created          REAL NOT NULL DEFAULT 0,
    embedded         INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_wikilinks_tgt ON wikilinks(tgt_slug);
CREATE INDEX IF NOT EXISTS idx_supersessions_tgt ON supersessions(tgt_slug);
CREATE INDEX IF NOT EXISTS idx_chunks_embedded ON chunks(embedded);
CREATE INDEX IF NOT EXISTS idx_assoc_a ON associations(a_path);
CREATE INDEX IF NOT EXISTS idx_assoc_b ON associations(b_path);
"""


class Store:
    def __init__(self, db_path: Path, embedding_dim: int = 1536):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path
        self.embedding_dim = embedding_dim
        self.db = sqlite3.connect(str(db_path))
        self.db.enable_load_extension(True)
        sqlite_vec.load(self.db)
        self.db.enable_load_extension(False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        # Idempotent migrations for DBs created before later columns existed.
        chunk_cols = {r["name"] for r in self.db.execute("PRAGMA table_info(chunks)")}
        if "context" not in chunk_cols:
            self.db.execute("ALTER TABLE chunks ADD COLUMN context TEXT NOT NULL DEFAULT ''")
        doc_cols = {r["name"] for r in self.db.execute("PRAGMA table_info(docs)")}
        if "last_retrieved" not in doc_cols:
            self.db.execute("ALTER TABLE docs ADD COLUMN last_retrieved REAL NOT NULL DEFAULT 0")
        if "retrieval_count" not in doc_cols:
            self.db.execute("ALTER TABLE docs ADD COLUMN retrieval_count INTEGER NOT NULL DEFAULT 0")
        self.db.execute(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS vec_chunks USING vec0(embedding float[{embedding_dim}])"
        )
        # FTS5 keyword index
        self.db.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS fts_chunks USING fts5("
            "text, rel_path UNINDEXED, chunk_idx UNINDEXED, tokenize='porter unicode61')"
        )
        # Consolidation layer (Tier 3) — dense summary memories + their own vec/FTS.
        self.db.execute(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS vec_summaries USING vec0(embedding float[{embedding_dim}])"
        )
        self.db.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS fts_summaries USING fts5("
            "text, title UNINDEXED, tokenize='porter unicode61')"
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

    def record_retrievals(self, rel_paths: list[str], ts: float) -> None:
        """Reinforce-on-use: bump retrieval_count + stamp last_retrieved for the
        docs a query actually surfaced. Frequently-surfaced docs accrue salience
        (the write path of the Ebbinghaus/Oblivion decay model)."""
        seen = set()
        for rel in rel_paths:
            if rel in seen:
                continue
            seen.add(rel)
            self.db.execute(
                "UPDATE docs SET retrieval_count = retrieval_count + 1, last_retrieved = ? "
                "WHERE rel_path = ?",
                (ts, rel),
            )
        self.db.commit()

    # ---------- associative memory (Hebbian co-retrieval graph) ----------

    def strengthen_associations(self, rel_paths: list[str], inc: float = 1.0) -> None:
        """Hebbian write: every unordered pair among the surfaced docs gains
        ``inc`` strength. Stored canonically (a_path < b_path) as an undirected
        edge. 'Docs that fire together wire together.'"""
        uniq = sorted({r for r in rel_paths})
        for i in range(len(uniq)):
            for j in range(i + 1, len(uniq)):
                a, b = uniq[i], uniq[j]
                self.db.execute(
                    "INSERT INTO associations (a_path, b_path, strength) VALUES (?, ?, ?) "
                    "ON CONFLICT(a_path, b_path) DO UPDATE SET strength = strength + ?",
                    (a, b, inc, inc),
                )
        self.db.commit()

    def neighbors(self, rel_path: str, min_strength: float = 0.0, limit: int = 8) -> list[tuple[str, float]]:
        """Strongest learned associations for a doc (both edge directions)."""
        rows = self.db.execute(
            "SELECT other, strength FROM ("
            "  SELECT b_path AS other, strength FROM associations WHERE a_path = ? "
            "  UNION ALL "
            "  SELECT a_path AS other, strength FROM associations WHERE b_path = ? "
            ") WHERE strength >= ? ORDER BY strength DESC LIMIT ?",
            (rel_path, rel_path, min_strength, limit),
        ).fetchall()
        return [(r["other"], r["strength"]) for r in rows]

    def representative_chunk(self, rel_path: str):
        """First chunk + doc signals for a doc — used to inject an associatively
        recalled doc that didn't directly match the query."""
        return self.db.execute(
            "SELECT c.chunk_idx, c.text, d.title, d.is_priority, d.doc_type, "
            "       d.mtime, d.retrieval_count "
            "FROM chunks c JOIN docs d ON d.rel_path = c.rel_path "
            "WHERE c.rel_path = ? ORDER BY c.chunk_idx LIMIT 1",
            (rel_path,),
        ).fetchone()

    def prune_missing(self) -> list[str]:
        """Drop docs whose source file no longer exists on disk — and their
        chunks, vectors, FTS rows, and wikilinks. (FK cascade does not fire
        without PRAGMA foreign_keys, and vec_chunks has no FK at all, so we
        delete every dependent row explicitly.) Returns the pruned rel_paths."""
        import os

        rows = self.db.execute("SELECT rel_path, abs_path FROM docs").fetchall()
        gone = [r["rel_path"] for r in rows if not os.path.exists(r["abs_path"])]
        for rel in gone:
            self.delete_doc_chunks(rel)
            self.db.execute("DELETE FROM wikilinks WHERE src_path = ?", (rel,))
            self.db.execute("DELETE FROM supersessions WHERE src_path = ?", (rel,))
            self.db.execute("DELETE FROM associations WHERE a_path = ? OR b_path = ?", (rel, rel))
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
        # sqlite-vec virtual tables don't support UPSERT; use DELETE+INSERT
        for cid, blob in zip(chunk_ids, blobs):
            self.db.execute("DELETE FROM vec_chunks WHERE rowid = ?", (cid,))
            self.db.execute(
                "INSERT INTO vec_chunks (rowid, embedding) VALUES (?, ?)",
                (cid, blob),
            )
            self.db.execute("UPDATE chunks SET embedded = 1 WHERE chunk_id = ?", (cid,))

    # ---------- consolidation layer (dense summary memories) ----------

    def all_chunk_vectors(self) -> list:
        """Every embedded chunk with its vector — clustering input for Tier 3."""
        return self.db.execute(
            "SELECT v.rowid AS chunk_id, c.rel_path, c.text, d.is_priority, v.embedding "
            "FROM vec_chunks v "
            "JOIN chunks c ON c.chunk_id = v.rowid "
            "JOIN docs d ON d.rel_path = c.rel_path"
        ).fetchall()

    def clear_summaries(self) -> None:
        self.db.execute("DELETE FROM vec_summaries")
        self.db.execute("DELETE FROM fts_summaries")
        self.db.execute("DELETE FROM summaries")
        self.db.commit()

    def insert_summary(self, title, text, source_paths, source_chunk_ids, created) -> int:
        import json

        cur = self.db.execute(
            "INSERT INTO summaries (title, text, source_paths, source_chunk_ids, n_sources, created) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (title, text, json.dumps(source_paths), json.dumps(source_chunk_ids),
             len(source_paths), created),
        )
        sid = cur.lastrowid
        self.db.execute(
            "INSERT INTO fts_summaries (rowid, text, title) VALUES (?, ?, ?)", (sid, text, title)
        )
        return sid

    def write_summary_embedding(self, summary_id: int, blob: bytes) -> None:
        self.db.execute("DELETE FROM vec_summaries WHERE rowid = ?", (summary_id,))
        self.db.execute(
            "INSERT INTO vec_summaries (rowid, embedding) VALUES (?, ?)", (summary_id, blob)
        )
        self.db.execute("UPDATE summaries SET embedded = 1 WHERE summary_id = ?", (summary_id,))

    def search_summaries(self, q_vec: bytes, k: int = 2) -> list:
        """Nearest dense summaries to a query vector (the abstraction layer)."""
        return self.db.execute(
            "SELECT s.summary_id, s.title, s.text, s.source_paths, s.n_sources, "
            "       vec_distance_cosine(v.embedding, ?) AS dist "
            "FROM vec_summaries v JOIN summaries s ON s.summary_id = v.rowid "
            "WHERE v.embedding MATCH ? AND k = ? ORDER BY dist",
            (q_vec, q_vec, k),
        ).fetchall()

    def list_summaries(self) -> list:
        return self.db.execute(
            "SELECT summary_id, title, n_sources, length(text) AS len, source_paths "
            "FROM summaries ORDER BY n_sources DESC"
        ).fetchall()

    # ---------- counts / health ----------

    def stats(self) -> dict:
        out = {}
        out["docs"] = self.db.execute("SELECT COUNT(*) AS n FROM docs").fetchone()["n"]
        out["chunks"] = self.db.execute("SELECT COUNT(*) AS n FROM chunks").fetchone()["n"]
        out["embedded"] = self.db.execute(
            "SELECT COUNT(*) AS n FROM chunks WHERE embedded = 1"
        ).fetchone()["n"]
        out["wikilinks"] = self.db.execute("SELECT COUNT(*) AS n FROM wikilinks").fetchone()["n"]
        out["priority_docs"] = self.db.execute(
            "SELECT COUNT(*) AS n FROM docs WHERE is_priority = 1"
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
