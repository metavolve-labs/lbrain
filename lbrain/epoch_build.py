"""Atomic epoch BUILD — orchestrate build → validate → swap (design v1.4, increment 2).

The only write path into an epoch-enabled brain. Builds a complete candidate in a
staging home (LOCAL scratch when the brain home is on a slow-random-write mount —
the staged-local recovery of 2026-08-31, institutionalized), runs the import/embed
pipeline against it via the installed CLI (same code, subprocess-isolated so the
caller's module-global config paths never cross-wire), validates with gate v2, then
publishes a CHECKPOINTED SINGLE FILE via VACUUM INTO on the destination filesystem
and repoints CURRENT.

Gate v2 (panel + CSO, every check is a named incident):
  integrity_check           — structural (Lucene CheckIndex analog)
  deletion manifest         — mass-absence must NOT count as deletion (CSO v1.4
                              amendment: vanished/empty roots refuse without a
                              ledgered --confirm-source-removed; /mnt/* roots are
                              checked against /proc/mounts before absence is believed)
  embedded == chunks        — necessary, never sufficient…
  norm floor + NaN + dim    — …because zero-vector returns as "the right number of
                              zeros" (Grok G3)
  vector self-match ≈ 0     — the vector space answers about itself (vendor3)
  FTS count + token probe   — the keyword path serves (CSO D4; his phase-1 false
                              RED was an FTS-empty brain that looked structurally fine)
  identity carry-forward    — byte-identical, or refuse
  free disk ≥ candidate     — GOV.UK preflight
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import sqlite3
import struct
import subprocess
import tempfile
import time
from pathlib import Path

from .epoch import (
    BuilderLock,
    EpochError,
    epoch_db,
    epoch_dir,
    epochs_root,
    failed_dir,
    new_epoch_id,
    publish,
    resolve_db_path,
)
from .index import discover


# ---------- helpers ----------

def _is_slow_home(home: Path) -> bool:
    """DrvFs heuristic: /mnt/* per-row SQLite writes hang (measured >10min vs 59s)."""
    return str(home).startswith("/mnt/")


def _mount_present(src: str) -> bool:
    """For /mnt/<x> sources: believe absence only after /proc/mounts confirms the
    mount is there (CSO v1.4: an unmounted 9p bridge presents as mass deletion)."""
    if not src.startswith("/mnt/"):
        return True
    parts = src.split("/")
    mnt = "/".join(parts[:3])  # /mnt/c
    try:
        with open("/proc/mounts", encoding="utf-8") as f:
            return any(line.split()[1] == mnt for line in f if len(line.split()) > 1)
    except OSError:
        return True  # cannot read /proc — do not invent a failure


def _sqlite_snapshot(src_db: Path, dst_db: Path) -> None:
    """Byte-consistent live copy via the backup API — NEVER a file cp (howtocorrupt
    §1.2/§1.4) and NEVER a hardlink (CSO D1: a hardlinked delta base mutates the
    retained rollback target in place)."""
    src = sqlite3.connect(str(src_db))
    dst = sqlite3.connect(str(dst_db))
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()


def _connect_vec(db_path: Path) -> sqlite3.Connection:
    import sqlite_vec
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    con.enable_load_extension(True)
    sqlite_vec.load(con)
    con.enable_load_extension(False)
    return con


def _inventory(db_path: Path, sources: list[str]) -> dict[str, dict[str, str]]:
    """{source_root: {rel_path: doc_hash}} — docs mapped to their configured source
    by longest-prefix match on abs_path."""
    roots = sorted((str(Path(s)) for s in sources), key=len, reverse=True)
    inv: dict[str, dict[str, str]] = {str(Path(s)): {} for s in sources}
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    try:
        for r in con.execute("SELECT rel_path, abs_path, doc_hash FROM docs"):
            ap = r["abs_path"]
            for root in roots:
                if ap == root or ap.startswith(root.rstrip("/") + "/"):
                    inv[root][r["rel_path"]] = r["doc_hash"]
                    break
    finally:
        con.close()
    return inv


def _source_digest(docs: dict[str, str]) -> str:
    """Content digest of one source's inventory — (rel_path, doc_hash) lines,
    sorted. Deliberately mtime-free (rescope rule 5)."""
    body = "\n".join(f"{k}\t{v}" for k, v in sorted(docs.items()))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _run_cli(args: list[str], staging_home: Path, lbrain_bin: str,
             lock=None, hb_interval: float = 10.0) -> str:
    """Run a pipeline stage in the staging home, HEARTBEATING the builder lock
    while it runs (CSO R1: a real embed runs minutes; heartbeating only between
    stages let a live builder go stale mid-stage and be seized). A LockLost from
    the heartbeat propagates and aborts the build — the R1b-correct outcome."""
    env = dict(os.environ)
    env["LBRAIN_HOME"] = str(staging_home)
    proc = subprocess.Popen([lbrain_bin, *args], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    last_hb = time.monotonic()
    try:
        while True:
            try:
                out, err = proc.communicate(timeout=1.0)
                break
            except subprocess.TimeoutExpired:
                if lock is not None and time.monotonic() - last_hb >= hb_interval:
                    lock.heartbeat()  # LockLost aborts — never build without the lock
                    last_hb = time.monotonic()
    except BaseException:
        proc.kill()
        proc.communicate()
        raise
    if proc.returncode != 0:
        tail = (out + "\n" + err)[-2000:]
        raise EpochError(f"`lbrain {' '.join(args)}` failed in staging (rc {proc.returncode}):\n{tail}")
    return out


def _purge_source(db_path: Path, src_root: str, embedding_dim: int) -> int:
    """Remove every doc under a CONFIRMED-removed source root, full cascade."""
    from .store import Store

    store = Store(Path(db_path), embedding_dim=embedding_dim)
    try:
        rels = [r["rel_path"] for r in store.db.execute(
            "SELECT rel_path, abs_path FROM docs")
            if r["abs_path"] == src_root or r["abs_path"].startswith(src_root.rstrip("/") + "/")]
        with store.transaction():
            for rel in rels:
                store.delete_doc_chunks(rel)
                store.db.execute("DELETE FROM wikilinks WHERE src_path = ?", (rel,))
                store.db.execute("DELETE FROM supersessions WHERE src_path = ?", (rel,))
                store.db.execute("DELETE FROM claim_spans WHERE src_path = ?", (rel,))
                store.db.execute("DELETE FROM docs WHERE rel_path = ?", (rel,))
        return len(rels)
    finally:
        store.close()


# ---------- gate v2 ----------

def validate_candidate(
    db_path: Path,
    *,
    embedding_dim: int,
    sources: list[str],
    prior_inv: dict[str, dict[str, str]],
    confirmed_removed: set[str],
) -> list[str]:
    """Every failure is a string naming what refused and why. Empty list = pass."""
    failures: list[str] = []
    con = _connect_vec(db_path)
    try:
        ok = con.execute("PRAGMA integrity_check").fetchone()[0]
        if ok != "ok":
            failures.append(f"integrity_check: {ok!r}")

        docs = con.execute("SELECT COUNT(*) c FROM docs").fetchone()["c"]
        chunks = con.execute("SELECT COUNT(*) c FROM chunks").fetchone()["c"]
        vecs = con.execute("SELECT COUNT(*) c FROM vec_chunks").fetchone()["c"]
        if chunks < 1:
            failures.append(f"chunks: {chunks} (< 1)")
        if vecs != chunks:
            failures.append(f"embedded {vecs} != chunks {chunks}")

        # -- deletion manifest (CSO v1.4 amendment) --
        # Judged against the FILESYSTEM, not db diffs: a delta base can quietly
        # retain a vanished root's docs (prune's own mount-gone guard skips them),
        # which would make a db-diff check read "no deletions" while the corpus is
        # gone — mass-absence hidden by its own safety net. The test that found
        # this: test_vanished_source_root_refuses_without_confirmation.
        for src in (str(Path(s)) for s in sources):
            prior_docs = prior_inv.get(src, {})
            if not prior_docs or src in confirmed_removed:
                continue
            if not _mount_present(src):
                failures.append(
                    f"deletion-manifest: source {src} sits on a mount ABSENT from "
                    "/proc/mounts — an unmounted bridge is not a deletion")
            elif not os.path.isdir(src):
                failures.append(
                    f"deletion-manifest: source root {src} VANISHED while the prior epoch "
                    f"held {len(prior_docs)} docs — refuse; pass --confirm-source-removed "
                    "to assert intent")
            elif not discover([Path(src)]):
                failures.append(
                    f"deletion-manifest: source root {src} enumerated EMPTY while the prior "
                    f"epoch held {len(prior_docs)} docs (decoy/hollow root) — refuse; pass "
                    "--confirm-source-removed to assert intent")

        # -- vector sanity (Grok G3: the right number of zeros) --
        # FULL scan, not first-32 (CSO R4: the realistic embed-failure shape is
        # TAIL-shaped — rows insert in order and failures hit the end; a prefix
        # sample is blind to exactly the incident it exists to catch). At our
        # scale the full pass is cheap; correctness beats a millisecond.
        if vecs:
            stored_dim = None
            bad_norm = nan = scanned = 0
            first = None
            for r in con.execute("SELECT rowid, embedding FROM vec_chunks"):
                blob = r["embedding"]
                n = len(blob) // 4
                stored_dim = stored_dim or n
                v = struct.unpack(f"{n}f", blob)
                scanned += 1
                if any(x != x for x in v):
                    nan += 1
                if math.sqrt(sum(x * x for x in v)) < 1e-6:
                    bad_norm += 1
                if first is None:
                    first = (r["rowid"], blob)
            dim_ok = stored_dim == embedding_dim
            if not dim_ok:
                failures.append(f"vector dim {stored_dim} != configured {embedding_dim}")
            if nan:
                failures.append(f"{nan}/{scanned} vectors contain NaN")
            if bad_norm:
                failures.append(f"{bad_norm}/{scanned} vectors have ~zero norm")
            # self-match: the space must answer about itself with distance ≈ 0
            if first is not None and dim_ok and not bad_norm:
                rowid, blob = first
                try:
                    hit = con.execute(
                        "SELECT rowid, distance FROM vec_chunks WHERE embedding MATCH ? AND k = 1",
                        (blob,)).fetchone()
                except sqlite3.OperationalError:
                    hit = con.execute(
                        "SELECT rowid, distance FROM vec_chunks WHERE embedding MATCH ? "
                        "ORDER BY distance LIMIT 1", (blob,)).fetchone()
                if hit is None or hit["rowid"] != rowid or hit["distance"] > 1e-3:
                    failures.append(
                        f"vector self-match failed (got rowid {hit['rowid'] if hit else None}, "
                        f"distance {hit['distance'] if hit else 'n/a'})")

        # -- the keyword path serves (CSO D4) --
        fts = con.execute("SELECT COUNT(*) c FROM fts_chunks").fetchone()["c"]
        if fts != chunks:
            failures.append(f"fts rows {fts} != chunks {chunks}")
        if chunks:
            row = con.execute("SELECT rel_path, text FROM chunks LIMIT 1").fetchone()
            token = next((w for w in re.findall(r"[A-Za-z]{4,}", row["text"] or "")), None)
            if token:
                got = {r["rel_path"] for r in con.execute(
                    "SELECT rel_path FROM fts_chunks WHERE fts_chunks MATCH ? LIMIT 25",
                    (f'"{token}"',))}
                # CSO R3: "something returned" passes scrambled text↔doc bindings.
                # The probe asserts the DOC we took the token from is among the hits
                # (panel rule: golden queries with ASSERTED ids, not heartbeats).
                if row["rel_path"] not in got:
                    failures.append(
                        f"fts token probe {token!r} did not return its own doc "
                        f"{row['rel_path']!r} (got {sorted(got)[:3]}) — bindings suspect")
    finally:
        con.close()
    return failures


# ---------- the build ----------

def build(
    home: Path,
    cfg,
    *,
    delta: bool = True,
    confirm_source_removed: tuple[str, ...] = (),
    keep: int = 3,
    max_bytes: int | None = None,
    lbrain_bin: str = "lbrain",
    scratch: Path | None = None,
) -> dict:
    """Build → validate → swap. Returns a report dict; raises EpochError with the
    staging retained as .failed forensics on any gate refusal."""
    home = Path(home)
    sources = [str(Path(s)) for s in cfg.sources]
    confirmed = {str(Path(s)) for s in confirm_source_removed}
    report: dict = {"home": str(home)}

    with BuilderLock(home) as lock:
        eid = new_epoch_id()
        report["epoch_id"] = eid
        scan_start = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

        # staging locality (v1.1 #3 / lesson: stage local when the home is DrvFs)
        if scratch is not None:
            staging = Path(scratch) / f"epoch-{eid}"
        elif _is_slow_home(home):
            staging = Path(tempfile.mkdtemp(prefix=f"lbrain-epoch-{eid}-"))
        else:
            staging = epochs_root(home) / (eid + ".building")
        staging.mkdir(parents=True, exist_ok=True)
        staging_db = staging / "brain.db"

        try:
            # prior state (for delta base + the deletion manifest)
            prior_db: Path | None = None
            try:
                p = resolve_db_path(home, cfg.db_path)
                prior_db = p if p.exists() and p.stat().st_size > 0 else None
            except EpochError:
                prior_db = None  # dangling CURRENT: full build, publish will heal it
            prior_inv = _inventory(prior_db, sources) if prior_db else {}

            if delta and prior_db is not None:
                _sqlite_snapshot(prior_db, staging_db)  # byte-copy, never hardlink (D1)

            # staging home config = the home's, with db_path repointed
            raw = (home / "config.toml").read_text(encoding="utf-8")
            raw = re.sub(r"^db_path = .*$", f'db_path = "{staging_db}"', raw, count=1, flags=re.M)
            (staging / "config.toml").write_text(raw, encoding="utf-8")

            _run_cli(["import", "--prune"], staging, lbrain_bin, lock=lock)
            lock.heartbeat()
            # A CONFIRMED-removed root's docs are purged deliberately here — prune's
            # own mount-gone guard (correctly) refuses to drop them, so intent has
            # to be executed as an explicit, ledgered act, never a side effect.
            for src in confirmed:
                _purge_source(staging_db, src, cfg.embedding_dim)
            if prior_db is not None:
                _run_cli(["embed", "--reuse-from", str(prior_db)], staging, lbrain_bin, lock=lock)
            else:
                _run_cli(["embed", "--stale"], staging, lbrain_bin, lock=lock)
            lock.heartbeat()
            # Orphan derived-state sweep: the FIRST production build was refused by
            # the gate over 14 vectors with no chunk — historical debris the live
            # main brain had carried invisibly (an old delete path missed vec
            # rows). A candidate must be a pure function of (sources, config), so
            # orphans are swept here, counted, and reported — never tolerated by
            # loosening the gate.
            oc = _connect_vec(staging_db)
            try:
                orphans = oc.execute(
                    "SELECT COUNT(*) FROM vec_chunks WHERE rowid NOT IN "
                    "(SELECT rowid FROM chunks)").fetchone()[0]
                if orphans:
                    oc.execute("DELETE FROM vec_chunks WHERE rowid NOT IN "
                               "(SELECT rowid FROM chunks)")
                    oc.commit()
                    print(f"[lbrain] epoch build: swept {orphans} orphan vector(s) "
                          "(no owning chunk — inherited debris)")
                report["orphan_vectors_swept"] = orphans
            finally:
                oc.close()
            scan_end = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

            failures = validate_candidate(
                staging_db, embedding_dim=cfg.embedding_dim, sources=sources,
                prior_inv=prior_inv, confirmed_removed=confirmed)
            if failures:
                fdir = failed_dir(home, eid)
                fdir.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(staging, fdir, dirs_exist_ok=True)
                raise EpochError(
                    "gate v2 REFUSED promotion:\n  - " + "\n  - ".join(failures)
                    + f"\n  candidate retained: {fdir}")

            # watermark (mtime-free), stamped INTO the candidate before publication
            new_inv = _inventory(staging_db, sources)
            con = sqlite3.connect(str(staging_db))
            try:
                con.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                            ("epoch_id", eid))
                con.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                            ("watermark_scan_start", scan_start))
                con.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                            ("watermark_scan_end", scan_end))
                digests = {s: _source_digest(d) for s, d in new_inv.items()}
                con.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                            ("watermark_source_digests", json.dumps(digests, sort_keys=True)))
                con.commit()
                # publication: checkpointed SINGLE FILE on the destination fs (panel #1)
                dest = epoch_db(home, eid)
                dest.parent.mkdir(parents=True, exist_ok=True)
                con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                con.execute("PRAGMA journal_mode=DELETE")
                con.execute("VACUUM INTO ?", (str(dest),))
            finally:
                con.close()

            # post-vacuum re-check: VACUUM INTO interrupted = corrupt, so prove it.
            # Compare the published file against the CANDIDATE's own counts — the
            # first main-brain pilot build failed here because this line compared
            # against the source-mapped inventory instead, and a real brain holds
            # docs (abstractions, historical roots) outside source prefixes.
            scon = sqlite3.connect(str(staging_db))
            try:
                staging_docs = scon.execute("SELECT COUNT(*) FROM docs").fetchone()[0]
            finally:
                scon.close()
            vcon = sqlite3.connect(str(dest))
            try:
                if vcon.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise EpochError(f"published file failed integrity_check: {dest}")
                published_docs = vcon.execute("SELECT COUNT(*) FROM docs").fetchone()[0]
                if published_docs != staging_docs:
                    raise EpochError(
                        f"published doc count {published_docs} != candidate {staging_docs}: {dest}")
            finally:
                vcon.close()

            # identity carry-forward, byte-identical (gate rule)
            ident = home / "identity.json"
            if ident.exists():
                shutil.copy2(ident, epoch_dir(home, eid) / "identity.json")

            (epoch_dir(home, eid) / "build-manifest.json").write_text(json.dumps({
                "epoch_id": eid, "scan_start": scan_start, "scan_end": scan_end,
                "delta": bool(delta and prior_db is not None),
                "prior_db": str(prior_db) if prior_db else None,
                "docs": sum(len(d) for d in new_inv.values()),
                "source_digests": {s: _source_digest(d) for s, d in new_inv.items()},
                "confirmed_removed": sorted(confirmed),
            }, indent=2) + "\n", encoding="utf-8")

            caveat = publish(home, eid)
            report.update({"published": True, "docs": sum(len(d) for d in new_inv.values()),
                           "durability_caveat": caveat, "scan_start": scan_start,
                           "scan_end": scan_end})
            # prune runs INSIDE the lock (CSO R8): rmtree must never race a live
            # build's staging, and lock-held is what makes .building orphan-sweep safe.
            from .epoch import prune
            report["pruned"] = prune(home, keep=keep, max_bytes=max_bytes, lock=lock)
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    return report
