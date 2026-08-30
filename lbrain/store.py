"""SQLite + sqlite-vec + FTS5 store. Single file. Native. Fast."""

from __future__ import annotations

import os
import sqlite3
import sys
from contextlib import contextmanager
from pathlib import Path

import sqlite_vec

from .index import Chunk, Doc


class SqliteExtensionError(RuntimeError):
    """This interpreter cannot load SQLite extensions, so sqlite-vec is unavailable.

    Raised instead of the bare AttributeError that sqlite3 produces, so the CLI
    can surface an actionable message rather than a traceback.
    """

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
    -- Disclosure class from frontmatter (lbrain/disclosure.py): artifact |
    -- proposal | private. A COLUMN rather than a read through `metadata`,
    -- because the blinding filter touches every candidate on every query and
    -- JSON-parsing ~2,000 metadata blobs per query is not a filter, it is a
    -- tax. '' = unclassified, which every blinding mode withholds.
    disclosure TEXT NOT NULL DEFAULT '',
    -- Evidence class from frontmatter (lbrain/grading.py): observed | sourced |
    -- synthesized. A COLUMN for the same reason as disclosure: the served header
    -- renders it for every hit on every query, and JSON-parsing `metadata` per
    -- hit to read one field is a tax, not a lookup. '' = UNGRADED (Admiralty 6).
    evidence TEXT NOT NULL DEFAULT '',
    -- Frontmatter `date:` (lbrain/staleness.py). A COLUMN because the served
    -- header resolves a claim date for every hit, and the value cannot be
    -- recovered from chunk text: parse() strips the frontmatter out of the body.
    claim_date TEXT NOT NULL DEFAULT ''
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
    heading_path TEXT NOT NULL DEFAULT '',
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

-- Per-agent beliefs (lbrain/beliefs.py). A belief IS a markdown doc — this table
-- is a PROJECTION of its frontmatter, kept queryable so retrieval can filter on
-- author and lifecycle without re-parsing files. Source of truth stays the file,
-- per the corpus hierarchy: delete the file, re-import, and the row cascades away.
CREATE TABLE IF NOT EXISTS beliefs (
    belief_id   TEXT PRIMARY KEY,
    rel_path    TEXT NOT NULL UNIQUE,
    persona     TEXT NOT NULL DEFAULT '',
    state       TEXT NOT NULL DEFAULT 'draft',
    subject     TEXT NOT NULL DEFAULT '',
    claim       TEXT NOT NULL DEFAULT '',
    confidence  TEXT NOT NULL DEFAULT '',
    impact      TEXT NOT NULL DEFAULT '',
    created     TEXT NOT NULL DEFAULT '',
    promoted_at TEXT NOT NULL DEFAULT '',
    verify_by   TEXT NOT NULL DEFAULT '',
    countersigned_by TEXT NOT NULL DEFAULT '',
    FOREIGN KEY (rel_path) REFERENCES docs(rel_path) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS belief_evidence (
    belief_id TEXT NOT NULL,
    ref       TEXT NOT NULL,
    kind      TEXT NOT NULL,          -- 'link' | 'external' (syntactic, not resolved)
    verified  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (belief_id, ref),
    FOREIGN KEY (belief_id) REFERENCES beliefs(belief_id) ON DELETE CASCADE
);

-- Key/value metadata. Records the embedding fingerprint (dim/model/provider) the
-- vectors were built with, so a later config change can't silently corrupt the space.
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_wikilinks_tgt ON wikilinks(tgt_slug);
CREATE INDEX IF NOT EXISTS idx_supersessions_tgt ON supersessions(tgt_slug);
CREATE INDEX IF NOT EXISTS idx_chunks_embedded ON chunks(embedded);
CREATE INDEX IF NOT EXISTS idx_beliefs_state ON beliefs(state);
CREATE INDEX IF NOT EXISTS idx_beliefs_subject ON beliefs(subject);
"""


class Store:
    def __init__(self, db_path: Path, embedding_dim: int = 1536):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        # brain.db holds every chunk of the corpus in CLEARTEXT. Under the common
        # umask 022, mkdir gave 0755 and sqlite3.connect() gave 0644 — world-
        # readable, verified 2026-07-28 (red-team finding 10). The existing
        # chmod 0700 only ever ran from _write_env_var, i.e. only when a HOSTED
        # key was configured, so the privacy-maximal local-only install was
        # precisely the one left open. Create private, before connecting.
        try:
            db_path.parent.chmod(0o700)
        except OSError:
            pass
        if not db_path.exists():
            try:
                os.close(os.open(str(db_path), os.O_CREAT | os.O_WRONLY | os.O_EXCL, 0o600))
            except FileExistsError:
                pass
            except OSError as e:
                print(
                    f"[lbrain] WARNING: could not create {db_path} privately ({e}); "
                    "the corpus may be readable by other users on this filesystem.",
                    file=sys.stderr,
                )
        else:
            # An install that predates this fix already has a 0644 database. The
            # 0700 parent above blocks traversal, but don't leave the file itself
            # loose — a later reopen of the dir, a backup, or a copy would carry
            # the permissive mode with it. SQLite gives -wal/-shm the main file's
            # mode, so tighten those alongside it.
            for p in (db_path, db_path.with_name(db_path.name + "-wal"),
                      db_path.with_name(db_path.name + "-shm")):
                try:
                    if p.exists() and (p.stat().st_mode & 0o077):
                        p.chmod(0o600)
                except OSError:
                    pass
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
        # sqlite-vec is a loadable extension, and a Python built without
        # --enable-loadable-sqlite-extensions does not have the method at all.
        # Apple's /usr/bin/python3 and the python.org macOS installers both ship
        # it disabled, so `lbrain init` — the first command in the README — died
        # on a raw AttributeError traceback for a large share of first-time Mac
        # users. Fail with something a stranger can act on.
        if not hasattr(self.db, "enable_load_extension"):
            raise SqliteExtensionError(
                f"This Python ({sys.executable}) was built without SQLite "
                "loadable-extension support, which LBrain needs to load "
                "sqlite-vec for vector search.\n"
                "\n"
                "Apple's /usr/bin/python3 and the python.org macOS installers "
                "ship with it disabled. Use a Python that enables it:\n"
                "\n"
                "  macOS   brew install python@3.12\n"
                "          /usr/local/opt/python@3.12/bin/python3.12 -m venv .venv\n"
                "          (Apple Silicon: /opt/homebrew/opt/python@3.12/...)\n"
                "\n"
                "  pyenv   PYTHON_CONFIGURE_OPTS=\"--enable-loadable-sqlite-extensions\" \\\n"
                "              pyenv install 3.12\n"
                "\n"
                "  Linux   distro python3 packages normally have it enabled\n"
                "\n"
                "Check any interpreter with:\n"
                "  python3 -c \"import sqlite3; print(hasattr("
                "sqlite3.connect(':memory:'), 'enable_load_extension'))\""
            )
        self.db.enable_load_extension(True)
        sqlite_vec.load(self.db)
        self.db.enable_load_extension(False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        # Idempotent migrations for DBs created before later columns existed.
        chunk_cols = {r["name"] for r in self.db.execute("PRAGMA table_info(chunks)")}
        if "context" not in chunk_cols:
            self.db.execute("ALTER TABLE chunks ADD COLUMN context TEXT NOT NULL DEFAULT ''")
        if "heading_path" not in chunk_cols:
            self.db.execute(
                "ALTER TABLE chunks ADD COLUMN heading_path TEXT NOT NULL DEFAULT ''"
            )
        doc_cols = {r["name"] for r in self.db.execute("PRAGMA table_info(docs)")}
        if "disclosure" not in doc_cols:
            self.db.execute("ALTER TABLE docs ADD COLUMN disclosure TEXT NOT NULL DEFAULT ''")
        if "evidence" not in doc_cols:
            self.db.execute("ALTER TABLE docs ADD COLUMN evidence TEXT NOT NULL DEFAULT ''")
        if "claim_date" not in doc_cols:
            self.db.execute("ALTER TABLE docs ADD COLUMN claim_date TEXT NOT NULL DEFAULT ''")
        self.db.execute(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS vec_chunks USING vec0(embedding float[{embedding_dim}])"
        )
        # FTS5 keyword index
        self.db.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS fts_chunks USING fts5("
            "text, rel_path UNINDEXED, chunk_idx UNINDEXED, tokenize='porter unicode61')"
        )
        # NOTE: the optional Tier-2 archive layer (archives/vec_archives/fts_archives)
        # creates its own tables lazily via lbrain.archive.storage.ArchiveStore — the
        # core store has no knowledge of it.
        self.db.commit()

    def _table_exists(self, name: str) -> bool:
        return self.db.execute(
            "SELECT 1 FROM sqlite_master WHERE type IN ('table', 'view') AND name = ?",
            (name,),
        ).fetchone() is not None

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
        """Drop + recreate the chunk vector table at a new dimension and mark every
        chunk un-embedded. Required when the embedding dim changes (vec0 column width
        is fixed at creation); a full re-embed must follow.

        The optional archive layer keeps its vectors in a separate table at the same
        dim, so invalidate it too (drop here; the archive layer recreates it lazily at
        the new dim, and marks its rows un-embedded for re-capture)."""
        self.db.execute("DROP TABLE IF EXISTS vec_chunks")
        self.db.execute(f"CREATE VIRTUAL TABLE vec_chunks USING vec0(embedding float[{dim}])")
        self.db.execute("UPDATE chunks SET embedded = 0")
        self.db.execute("DROP TABLE IF EXISTS vec_archives")
        if self._table_exists("archives"):
            self.db.execute("UPDATE archives SET embedded = 0")
        self.embedding_dim = dim
        self.db.commit()

    # ---------- docs ----------

    def doc_paths(self) -> list[tuple[str, str]]:
        """Every indexed doc as (rel_path, abs_path) — the index's own claim
        about which files it was built from.

        Both columns, because they answer different questions: `rel_path` is the
        key an importer would write for a file it rediscovers, and `abs_path` is
        the only way to ask whether that file is still there. Comparing on one
        alone loses a whole class of divergence (see index_currency.survey).
        """
        return [(r["rel_path"], r["abs_path"]) for r in
                self.db.execute("SELECT rel_path, abs_path FROM docs")]

    def get_doc_hash(self, rel_path: str) -> str | None:
        row = self.db.execute(
            "SELECT doc_hash FROM docs WHERE rel_path = ?", (rel_path,)
        ).fetchone()
        return row["doc_hash"] if row else None

    def doc_metadata_differs(self, doc: Doc) -> bool:
        """True if the stored row disagrees with this Doc's FRONTMATTER-derived
        fields, even though the body hash matches.

        `doc_hash` covers the body only (index.py: `sha1(body)`), so editing
        `type:`, `name:`, `description:` or `verify_by:` never changed it and
        `import` skipped the file — the DB kept the old value indefinitely
        (anomaly A-401). Those fields are not cosmetic: `type` routes a document
        into the doc_type filter AND into the feedback rule engine, `name` is the
        wikilink slug, `description` is served in the record header, `verify_by`
        drives the staleness DECIDABLE tier.

        Detecting this separately from the body hash is deliberate. Folding
        metadata into `doc_hash` would have marked all ~2,000 documents changed
        on the next import and forced a full re-chunk and re-embed. A frontmatter
        edit does not change a single chunk — so it needs a one-row UPDATE, not
        re-embedding.
        """
        import json

        row = self.db.execute(
            "SELECT title, doc_type, is_priority, metadata, disclosure, evidence, claim_date "
            "FROM docs WHERE rel_path = ?",
            (rel_path := doc.rel_path,),
        ).fetchone()
        if row is None:
            return True
        try:
            stored_meta = json.loads(row["metadata"] or "{}")
        except (ValueError, TypeError):
            stored_meta = {}
        # Compare against the SAME transform upsert_doc stores through. YAML
        # parses an unquoted `created: 2026-05-03` into a datetime.date, which is
        # stored as the string "2026-05-03" — so comparing a fresh parse to the
        # stored row reported a difference on every import, forever. Observed
        # live: 3 documents refreshed on every single run. The counter is what
        # exposed it; a silent version of this fix would have looked like it worked.
        parsed_meta = json.loads(json.dumps(_safe_meta(doc.metadata)))
        return (
            (row["title"] or "") != (doc.title or "")
            or (row["doc_type"] or "") != (doc.doc_type or "")
            or (row["disclosure"] or "") != (getattr(doc, "disclosure", "") or "")
            # Every frontmatter-DERIVED column belongs in this comparison, not
            # just the ones that existed when it was written. A column added by
            # migration starts empty on an existing brain, and if nothing here
            # notices, an unchanged file is skipped on every future import and the
            # column stays empty forever — the feature ships and reaches only
            # corpora imported after it. That is A-401 one level down: the stored
            # PROJECTION disagrees with what a fresh parse would derive, and the
            # body hash cannot see it because the body did not change.
            or (row["evidence"] or "") != (getattr(doc, "evidence", "") or "")
            or (row["claim_date"] or "") != (getattr(doc, "claim_date", "") or "")
            or bool(row["is_priority"]) != bool(doc.is_priority)
            or stored_meta != parsed_meta
        )

    def upsert_doc(self, doc: Doc) -> None:
        import json

        self.db.execute(
            "INSERT INTO docs (rel_path, abs_path, title, doc_hash, mtime, is_priority, doc_type, "
            "metadata, disclosure, evidence, claim_date) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(rel_path) DO UPDATE SET abs_path=excluded.abs_path, title=excluded.title, "
            "doc_hash=excluded.doc_hash, mtime=excluded.mtime, is_priority=excluded.is_priority, "
            "doc_type=excluded.doc_type, metadata=excluded.metadata, disclosure=excluded.disclosure, "
            "evidence=excluded.evidence, claim_date=excluded.claim_date",
            (
                doc.rel_path,
                str(doc.path),
                doc.title,
                doc.doc_hash,
                doc.mtime,
                int(doc.is_priority),
                doc.doc_type,
                json.dumps(_safe_meta(doc.metadata)),
                getattr(doc, "disclosure", "") or "",
                getattr(doc, "evidence", "") or "",
                getattr(doc, "claim_date", "") or "",
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

        from pathlib import Path as _Path

        from .index import is_backup_path

        rows = self.db.execute("SELECT rel_path, abs_path FROM docs").fetchall()
        # Scope the prune to docs UNDER the imported roots. A NARROW import
        # (`lbrain import <subdir>`) walks only that subtree, so it must never
        # prune docs from OTHER configured sources it never walked — those files
        # are on disk and their docs are live. (CIO/keel brain, 2026-08-30: a
        # narrow experiment-folder import pruned 38 _COLLAB inbox docs whose files
        # were present the whole time.) `source_roots` was formerly ONLY the
        # mount-gone guard above; it now also bounds WHAT is eligible to be pruned.
        # When it is None (a deliberate whole-brain sweep) every doc stays in
        # scope, exactly as before.
        if source_roots:
            _roots = [os.path.realpath(str(r)) for r in source_roots]

            def _under_root(abs_path: str) -> bool:
                rp = os.path.realpath(abs_path)
                return any(rp == root or rp.startswith(root + os.sep) for root in _roots)

            rows = [r for r in rows if _under_root(r["abs_path"])]
        # "No longer indexable" is not the same as "no longer on disk". A doc that
        # became EXCLUDED (a backup tree) still exists, so an existence-only prune
        # left it serving forever: discover() stopped finding it, import reported
        # `pruned: 0`, and its superseded text kept ranking against the record that
        # corrected it. Verified live 2026-07-28 — the exclusion shipped without
        # this and changed nothing a user would see.
        gone = [
            r["rel_path"]
            for r in rows
            if not os.path.exists(r["abs_path"]) or is_backup_path(_Path(r["abs_path"]))
        ]
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

    def superseded_edges(self) -> list[tuple[str, str]]:
        """(superseding_doc_rel_path, target_slug) pairs. The src_path lets a
        caller resolve a basename-slug that collides across directories to the
        target in the SAME directory as the superseding doc — a bare slug alone
        is non-unique and buries every same-named doc (AX-06)."""
        return [
            (r["src_path"], r["tgt_slug"])
            for r in self.db.execute("SELECT src_path, tgt_slug FROM supersessions")
        ]

    def disclosure_classes(self) -> dict[str, str]:
        """rel_path → raw disclosure class, for docs that declare one.

        Only the classified rows are returned: on a corpus where nothing is
        classified this is an empty dict and a single query, rather than 2,000
        rows of empty string. Absence from the map IS the unclassified state.
        """
        return {
            r["rel_path"]: r["disclosure"]
            for r in self.db.execute(
                "SELECT rel_path, disclosure FROM docs WHERE disclosure != ''"
            )
        }

    # ---------- beliefs (per-agent memory; see lbrain/beliefs.py) ----------
    #
    # Deliberately primitive-only: this layer stores columns, it does not import
    # lbrain.beliefs. beliefs.py depends on search.py which depends on store.py,
    # so a reverse import would close a cycle. Keeping the store dumb about the
    # Belief type also means a schema read is never blocked on the gate logic.

    def replace_belief(self, fields: dict, evidence: list[tuple[str, str, bool]]) -> None:
        """Upsert one belief row and its evidence edges. Idempotent per doc, the
        same way replace_supersessions is, so a re-import with an edited
        frontmatter converges rather than accumulating.

        Two rows must never claim one file: a slug can be edited in place
        (`name:` changed), which leaves the OLD belief_id still holding this
        rel_path and its UNIQUE constraint. Clear that first — otherwise the
        insert fails and the belief silently stops updating.
        """
        bid = fields["belief_id"]
        self.db.execute(
            "DELETE FROM beliefs WHERE rel_path = ? AND belief_id <> ?",
            (fields["rel_path"], bid),
        )
        cols = (
            "belief_id", "rel_path", "persona", "state", "subject", "claim",
            "confidence", "impact", "created", "promoted_at", "verify_by",
            "countersigned_by",
        )
        self.db.execute(
            f"INSERT INTO beliefs ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))}) "
            "ON CONFLICT(belief_id) DO UPDATE SET "
            + ", ".join(f"{c}=excluded.{c}" for c in cols[1:]),
            tuple(fields.get(c, "") or "" for c in cols),
        )
        self.db.execute("DELETE FROM belief_evidence WHERE belief_id = ?", (bid,))
        for ref, kind, verified in evidence:
            self.db.execute(
                "INSERT OR IGNORE INTO belief_evidence (belief_id, ref, kind, verified) "
                "VALUES (?, ?, ?, ?)",
                (bid, ref, kind, int(verified)),
            )

    def delete_belief_for_path(self, rel_path: str) -> None:
        """Drop the belief projection for a doc that is no longer one (its
        `type: belief` was removed). Without this the row — and its draft
        visibility rules — would outlive the frontmatter that created it."""
        self.db.execute("DELETE FROM beliefs WHERE rel_path = ?", (rel_path,))

    def belief_states(self) -> dict[str, tuple[str, str]]:
        """rel_path → (persona, state), for the retrieval-time visibility filter.

        Keyed by rel_path rather than slug on purpose: the filter must be exact.
        A slug collision would leak one persona's draft into another's results,
        which is the one failure this whole layer exists to prevent.
        """
        return {
            r["rel_path"]: (r["persona"], r["state"])
            for r in self.db.execute("SELECT rel_path, persona, state FROM beliefs")
        }

    def belief_personas(self) -> set[str]:
        """Every persona that has authored a belief. Used to catch a mistyped
        LBRAIN_PERSONA, which otherwise fails closed SILENTLY — the author loses
        access to their own drafts and concludes the beliefs were lost."""
        return {
            r["persona"]
            for r in self.db.execute("SELECT DISTINCT persona FROM beliefs WHERE persona != ''")
        }

    def belief_rows(self) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM beliefs ORDER BY belief_id"
        ).fetchall()

    def belief_row_for_path(self, rel_path: str) -> sqlite3.Row | None:
        """The belief projection for a FILE. Used to tell "this stopped being a
        belief" from "this belief's frontmatter stopped parsing"."""
        return self.db.execute(
            "SELECT * FROM beliefs WHERE rel_path = ?", (rel_path,)
        ).fetchone()

    def belief_row(self, belief_id: str) -> sqlite3.Row | None:
        return self.db.execute(
            "SELECT * FROM beliefs WHERE belief_id = ?", (belief_id,)
        ).fetchone()

    def belief_evidence_rows(self, belief_id: str) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT ref, kind, verified FROM belief_evidence WHERE belief_id = ? ORDER BY ref",
            (belief_id,),
        ).fetchall()

    def set_belief_state(self, belief_id: str, state: str, promoted_at: str = "") -> None:
        """Move a belief's lifecycle state in the PROJECTION only.

        The file is the source of truth, so a caller that flips state here must
        also write the frontmatter (cli.belief_promote does both). This exists so
        the change is visible to retrieval immediately, without waiting for the
        next import — not as an alternative to editing the record.
        """
        self.db.execute(
            "UPDATE beliefs SET state = ?, promoted_at = ? WHERE belief_id = ?",
            (state, promoted_at, belief_id),
        )

    def insert_chunks(self, chunks: list[Chunk]) -> list[int]:
        ids: list[int] = []
        for c in chunks:
            cur = self.db.execute(
                "INSERT INTO chunks (rel_path, chunk_idx, text, token_count, chunk_hash, "
                "context, heading_path) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (c.doc_path, c.chunk_idx, c.text, c.token_count, c.chunk_hash,
                 c.context, c.heading_path),
            )
            chunk_id = cur.lastrowid
            ids.append(chunk_id)
            # FTS indexes context+heading_path+text so a chunk is findable under
            # its doc's subject and its section ancestry even when the chunk body
            # never restates either. Display still reads the raw `text` column,
            # so previews stay clean.
            fts_text = "\n".join(p for p in (c.context, c.heading_path, c.text) if p)
            self.db.execute(
                "INSERT INTO fts_chunks (rowid, text, rel_path, chunk_idx) VALUES (?, ?, ?, ?)",
                (chunk_id, fts_text, c.doc_path, c.chunk_idx),
            )
        return ids

    # Embed text = context + heading_path + text, each included only when set, so
    # the vector carries the doc macro-context and the section ancestry without
    # touching display. Both empty → exactly `text`, byte-for-byte with pre-A-513
    # builds, so a flat corpus embeds to the same vectors as before.
    EMBED_TEXT_SQL = (
        "CASE WHEN context != '' THEN context || char(10) ELSE '' END || "
        "CASE WHEN heading_path != '' THEN heading_path || char(10) ELSE '' END || "
        "text"
    )

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
        # The archive layer is optional; its table may not exist. Report 0 if absent.
        out["archives"] = (
            self.db.execute("SELECT COUNT(*) AS n FROM archives WHERE shredded = 0").fetchone()["n"]
            if self._table_exists("archives")
            else 0
        )
        return out

    def close(self) -> None:
        self.db.close()


def _safe_meta(meta: dict) -> dict:
    """Strip non-JSON-serializable values from frontmatter.

    Recurses into nested dicts/lists: a datetime (unquoted YAML date) nested
    under a container — e.g. `metadata.modified` stamped by the auto-memory
    hook — passed the old top-level isinstance check unchanged and then aborted
    json.dumps, failing the ENTIRE import batch. Every non-JSON-native leaf,
    at any depth, is coerced to str (extending the original top-level intent).
    """
    def _coerce(v):
        if isinstance(v, dict):
            return {k: _coerce(x) for k, x in v.items()}
        if isinstance(v, list):
            return [_coerce(x) for x in v]
        if isinstance(v, (str, int, float, bool)) or v is None:
            return v
        return str(v)
    return {k: _coerce(v) for k, v in meta.items()}
