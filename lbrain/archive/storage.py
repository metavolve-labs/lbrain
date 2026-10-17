"""Archive storage layer — the Tier-2 tables and their queries.

Lives in the optional archive subpackage, not core ``store.py``: the core retrieval
engine has no knowledge of archives. ``ArchiveStore`` is a thin behavior wrapper over
the core Store's *existing* sqlite connection (one writer, one WAL), and creates its
own tables lazily the first time the archive layer is used.
"""

from __future__ import annotations

import json
import sqlite3

# Card-catalog mirror of permanent encrypted episodic records, plus its own vec/FTS.
# Siloed from the main retrieval path; reached only via deep-recall.
_ARCHIVE_SCHEMA = """
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
CREATE INDEX IF NOT EXISTS idx_archives_source ON archives(source_hash);
"""


class ArchiveStore:
    """Tier-2 archive tables + queries over a shared sqlite connection."""

    def __init__(self, db: sqlite3.Connection, embedding_dim: int = 1536):
        self.db = db
        self.embedding_dim = embedding_dim
        self.ensure_schema()

    def ensure_schema(self) -> None:
        self.db.executescript(_ARCHIVE_SCHEMA)
        # Idempotent migration for DBs created before source_hash existed.
        cols = {r["name"] for r in self.db.execute("PRAGMA table_info(archives)")}
        if cols and "source_hash" not in cols:
            self.db.execute("ALTER TABLE archives ADD COLUMN source_hash TEXT NOT NULL DEFAULT ''")
            self.db.execute("CREATE INDEX IF NOT EXISTS idx_archives_source ON archives(source_hash)")
        self.db.execute(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS vec_archives USING vec0(embedding float[{self.embedding_dim}])"
        )
        self.db.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS fts_archives USING fts5("
            "snapshot, title UNINDEXED, txid UNINDEXED, tokenize='porter unicode61')"
        )
        self.db.commit()

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

    def superseded_ids(self) -> set[int]:
        """Archive rows superseded by a NEWER snapshot of the same session, not shredded, same namespace.

        A session is captured whole at every SessionEnd and boundary, so one session becomes many archives
        (measured on a live home: 104 archives were 8 sessions). A transcript only grows, so the largest snapshot
        of a title (the session id) is its newest; every smaller one is superseded. NON-DESTRUCTIVE: superseded
        rows stay in the table and stay retrievable by txid; recall just stops serving them. Shredding them is a
        separate, explicit decision. Ties in size keep the most recently created row."""
        rows = self.db.execute(
            "SELECT archive_id, title, namespace, n_bytes, created FROM archives "
            "WHERE shredded = 0 AND title != ''"
        ).fetchall()
        best: dict[tuple[str, str], tuple[int, float, int]] = {}
        for r in rows:
            key = (r["title"], r["namespace"])
            cand = (r["n_bytes"], r["created"], r["archive_id"])
            if key not in best or cand > best[key]:
                best[key] = cand
        keep = {v[2] for v in best.values()}
        return {r["archive_id"] for r in rows if r["archive_id"] not in keep}

    def search_archives(self, q_vec: bytes, k: int = 5, namespace: str | None = None,
                        include_superseded: bool = False) -> list:
        """Semantic recall over archive snapshots (the read surface of Tier-2).
        Shredded archives are excluded — the snapshot survives but the record is gone. Superseded snapshots (an
        older, smaller capture of a session that has a newer one) are excluded unless ``include_superseded``, so a
        session that was captured N times occupies one result, not N."""
        skip = set() if include_superseded else self.superseded_ids()
        rows = self.db.execute(
            "SELECT a.archive_id, a.txid, a.title, a.snapshot, a.namespace, a.created, "
            "       a.n_bytes, a.shredded, a.transport, "
            "       vec_distance_cosine(v.embedding, ?) AS dist "
            "FROM vec_archives v JOIN archives a ON a.archive_id = v.rowid "
            "WHERE v.embedding MATCH ? AND k = ? ORDER BY dist",
            (q_vec, q_vec, max(k * 3, 15) + len(skip)),
        ).fetchall()
        out = []
        for r in rows:
            if r["shredded"] or r["archive_id"] in skip:
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

    def count_live(self) -> int:
        return self.db.execute(
            "SELECT COUNT(*) AS n FROM archives WHERE shredded = 0"
        ).fetchone()["n"]
