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


def _unscanned_count(db_path: Path, sources: list[str]) -> int:
    """How many indexed docs lie under NO configured source root (A-576).

    These are served to every query, but `epoch build` walks `sources`, so their
    content can never be refreshed and their staleness can never be detected.
    `index_currency.survey()` already names this class UNREACHABLE; this records
    the same fact INTO the epoch at build time, so the serving contract can
    report measured coverage instead of a configuration flag.

    Uses store._under_roots deliberately: on a case-insensitive filesystem a root
    spelled in different case is the same directory, and a byte comparison would
    call a reachable doc unreachable.
    """
    from .store import _norm_path, _under_roots
    roots = [_norm_path(s) for s in sources]
    con = sqlite3.connect(str(db_path))
    try:
        return sum(1 for (ap,) in con.execute("SELECT abs_path FROM docs")
                   if not _under_roots(ap, roots))
    finally:
        con.close()


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


def _retain_failed(staging: Path, fdir: Path) -> None:
    """Keep a refused candidate as forensics WITHOUT its wrapped archive keys (CSO P-C, 2026-09-16: a post-sweep
    staging holds ciphertext + keys together; `.failed/` is never pruned and never shredded, so keys must not land there)."""
    shutil.copytree(staging, fdir, dirs_exist_ok=True, ignore=shutil.ignore_patterns("keys"))
    k = fdir / "keys"
    if k.exists():
        shutil.rmtree(k, ignore_errors=True)


def _carry_archive_index(prior_db: Path, staging_db: Path) -> int:
    """Copy the Tier-2 archive index (archives rows, their FTS rows and vectors) from the prior epoch into a full-build
    candidate. Returns rows carried (0 when the prior has no archive tables). Content-addressed rows: INSERT OR IGNORE."""
    import sqlite3
    # CSO X2: a prior WITHOUT archive tables carries 0 (stated); a prior whose archive index cannot be READ aborts the
    # build -- a lost index must never look like an empty one. CSO X3/X4: the width comes from the prior's own DDL
    # and every embedded row's vector must read back, or the carry aborts; no default width, no silent drop.
    width = _archive_vec_width(prior_db)
    pc = _connect_vec(prior_db); pc.row_factory = sqlite3.Row
    try:
        has = pc.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='archives'").fetchone()
        if not has:
            return 0
        rows = [dict(r) for r in pc.execute("SELECT * FROM archives")]
        vecs = {r[0]: r[1] for r in pc.execute("SELECT rowid, embedding FROM vec_archives")} if width else {}
    except Exception as e:
        raise EpochError(f"prior archive index unreadable ({type(e).__name__}: {str(e)[:160]}); refusing to publish a candidate without it")
    finally:
        pc.close()
    if not rows:
        return 0
    embedded_ids = {r["archive_id"] for r in rows if r.get("embedded") and not r.get("shredded")}
    missing_vecs = sorted(embedded_ids - set(vecs))
    if missing_vecs:
        raise EpochError(f"prior archive index: {len(missing_vecs)} embedded row(s) have no vector in vec_archives ({missing_vecs[:5]}); refusing a carry that would lose them")
    if vecs and width and any(len(b) != width * 4 for b in vecs.values()):
        raise EpochError(f"prior archive index: vector bytes do not match the declared width {width}; refusing the carry")
    con = _connect_vec(staging_db); con.row_factory = sqlite3.Row
    try:
        from .archive.storage import ArchiveStore
        if not width:
            raise EpochError("prior archive index has rows but no vec_archives DDL width; refusing to guess a width")
        st = ArchiveStore(con, width); st.ensure_schema()
        n = 0
        for r in rows:
            cols = ", ".join(r.keys()); ph = ", ".join("?" for _ in r)
            con.execute(f"INSERT OR IGNORE INTO archives ({cols}) VALUES ({ph})", tuple(r.values()))
            aid = r["archive_id"]
            if not r.get("shredded"):
                con.execute("DELETE FROM fts_archives WHERE rowid = ?", (aid,))
                con.execute("INSERT INTO fts_archives (rowid, snapshot, title, txid) VALUES (?, ?, ?, ?)", (aid, r.get("snapshot") or "", r.get("title") or "", r.get("txid") or ""))
                if aid in vecs:
                    con.execute("DELETE FROM vec_archives WHERE rowid = ?", (aid,))
                    con.execute("INSERT INTO vec_archives (rowid, embedding) VALUES (?, ?)", (aid, vecs[aid]))
            n += 1
        con.commit(); return n
    finally:
        con.close()


def _archive_vec_width(db: Path) -> int | None:
    """The declared width of vec_archives in a db, from its own DDL (`float[N]`); None when the table is absent."""
    import sqlite3
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        row = con.execute("SELECT sql FROM sqlite_master WHERE name='vec_archives'").fetchone()
    finally:
        con.close()
    if not row or not row[0]:
        return None
    m = re.search(r"float\[(\d+)\]", row[0])
    return int(m.group(1)) if m else None


def _shred_in_candidate(staging_db: Path, txid: str, dim: int) -> None:
    """Mark a keyless archive row shredded in the CANDIDATE db (never the published one): its ciphertext cannot be
    decrypted, so the index must stop presenting it and the capture must not skip its payload as already archived.
    `dim` is the configured embedding width (CSO X5: a width-0 vec0 table cannot be created; a --full candidate has
    no archive tables until this creates them at the real width)."""
    if not staging_db.exists():
        return
    import sqlite3
    if not isinstance(dim, int) or dim <= 0:
        raise EpochError(f"candidate shred: embedding width {dim!r} is not a positive integer; refusing to create a vec table of that width")
    con = _connect_vec(staging_db); con.row_factory = sqlite3.Row   # the archive tables need sqlite-vec loaded
    try:
        from .archive.storage import ArchiveStore
        st = ArchiveStore(con, dim); st.ensure_schema()
        if st.get_archive(txid) is not None:
            st.mark_archive_shredded(txid, purge_snapshot=True)
        con.commit()
    finally:
        con.close()


def _sweep_spool(home: Path, staging: Path, lbrain_bin: str, lock, report: dict, dim: int = 0) -> list[dict]:
    """Archive every staged capture into the staging candidate. Returns the plan (one dict per entry, with the txid the
    capture reported) for `_finish_sweep` after publish. Skips loudly, never silently: no passphrase = nothing swept."""
    from .spool import staged_items, keyless_receipts, sweep_receipt_path
    # increment 2b (2026-09-16, found by the CTO scoping 3b): a receipt whose txid has no local key is FALSE -- the record
    # cannot be decrypted (the first live sweep left every wrapped key in the staging home). Such entries are re-staged:
    # the false receipt is removed and the keyless index row is marked shredded in the CANDIDATE so the capture does not
    # skip it as "already archived". Additive: the orphaned ciphertext stays under home/archive/.
    healed = []; heal_errors = []
    for meta, txid in keyless_receipts(home):
        try:
            _shred_in_candidate(staging / "brain.db", txid, dim)   # first: if this fails the false receipt stays
            sweep_receipt_path(meta).unlink()
            healed.append(txid)
        except Exception as e:
            heal_errors.append(f"{meta.name}: {type(e).__name__}: {str(e)[:200]}")
    if heal_errors:
        # CSO X5: a heal that fails must never become a normal-looking report; a false receipt left standing is
        # an undecryptable record presented as archived. Fail closed: the build stops here, staging is retained.
        report["sweep_heal_errors"] = heal_errors
        raise EpochError("capture-spool heal FAILED for %d false receipt(s): %s" % (len(heal_errors), "; ".join(heal_errors)[:600]))
    items = staged_items(home)
    info: dict = {"staged": len(items), "swept": 0, "already": 0, "failed": 0, "bytes": 0, "healed_keyless": len(healed)}
    report["sweep"] = info
    if not items:
        return []
    try:
        from .archive.cli import archive_passphrase
        have_pass = bool(archive_passphrase())
    except Exception as e:  # cryptography extra absent, etc.
        have_pass = False
        info["skipped"] = f"archive extra unavailable: {e}"
    if not have_pass:
        info.setdefault("skipped", "no archive passphrase (LBRAIN_ARCHIVE_PASSPHRASE): captures stay staged")
        return []
    plan = []
    for meta in items:
        try:
            m = json.loads(meta.read_text(encoding="utf-8"))
            payload = meta.with_name(meta.name[: -len(".meta.json")] + ".transcript")
            args = ["capture", "--from-file", str(payload)]   # top-level `lbrain capture` (the `archive` command takes a SOURCE)
            if m.get("session_id"): args += ["--session-id", str(m["session_id"])]
            if m.get("title"): args += ["--title", str(m["title"])]
            if m.get("namespace"): args += ["--namespace", str(m["namespace"])]
            out = _run_cli(args, staging, lbrain_bin, lock=lock)
            # txids are base64url (LocalTransport: sha256 of the ciphertext, urlsafe), not hex: match any token
            # CSO P-A/X7: the txid comes ONLY from the capture CLI's machine line `LBRAIN-TXID <id>` (its own line, printed
            # last), never from prose that a transcript title could imitate; the id must be a 43-char base64url token.
            ids = re.findall(r"^LBRAIN-TXID ([A-Za-z0-9_-]{43})$", out, flags=re.M)
            already = "already captured" in out
            if not ids:
                info["failed"] += 1
                info.setdefault("failures", []).append(f"{meta.name}: capture printed no LBRAIN-TXID line ({'already captured' if already else 'fresh'} branch)")
                continue
            txid = ids[-1]
            plan.append({"meta": str(meta), "sha256": m.get("sha256"), "bytes": int(m.get("size") or 0),
                         "txid": txid, "already": already, "captured_at": m.get("captured_at")})
            info["bytes"] += int(m.get("size") or 0)
            info["already" if already else "swept"] += 1
        except Exception as e:   # P-B: a torn sidecar or any per-entry error is COUNTED and NAMED; the build goes on
            info["failed"] += 1
            info.setdefault("failures", []).append(f"{meta.name}: {type(e).__name__}: {str(e)[-300:]}")
    return plan


def _reclaim_verified(home: Path, plan: list[dict], report: dict) -> int:
    """Unlink a spooled payload only after the archived record round-trips: home/archive ciphertext + home/keys wrapped
    key -> decrypt with the build's passphrase -> sha256 == meta sha256. Anything short of that keeps the bytes."""
    try:
        from .archive import crypto
        from .archive.archiver import Keystore, LocalTransport
        from .archive.cli import archive_passphrase
        pw = archive_passphrase()
    except Exception as e:
        report["sweep"].setdefault("reclaim_skipped", str(e)[:120]); return 0
    if not pw:
        report["sweep"].setdefault("reclaim_skipped", "no passphrase"); return 0
    ks = Keystore(home / "keys"); tr = LocalTransport(home / "archive"); n = 0
    for e in plan:
        meta = Path(e["meta"]); payload = meta.with_name(meta.name[: -len(".meta.json")] + ".transcript")
        try:
            key_env = ks.get(e["txid"])
            if key_env is None: continue
            data = crypto.decrypt(tr.get(e["txid"]), key_env, pw)
            if hashlib.sha256(data).hexdigest() != str(e.get("sha256") or ""): 
                report["sweep"].setdefault("reclaim_mismatch", []).append(e["txid"]); continue
            if payload.is_file():
                payload.unlink(); n += 1
        except Exception as ex:
            report["sweep"].setdefault("reclaim_errors", []).append(f"{e['txid']}: {type(ex).__name__}: {str(ex)[:120]}")
    return n


def _finish_sweep(home: Path, staging: Path, eid: str, plan: list[dict], report: dict) -> None:
    """After publish: bring the candidate's new ciphertext into the home's archive/ (content-addressed; existing files are
    left alone) and write one receipt per swept entry. Receipts are the last write, so a crash here re-sweeps next time."""
    from .spool import write_sweep_receipt
    copied = {"archive": 0, "keys": 0}
    for sub in ("archive", "keys"):   # the ciphertext AND its wrapped key: a record without its key is unrecoverable
        src = staging / sub; dst = home / sub
        if src.is_dir():
            dst.mkdir(parents=True, exist_ok=True)
            for f in sorted(src.iterdir()):
                if f.is_file() and not (dst / f.name).exists():
                    shutil.copy2(f, dst / f.name); copied[sub] += 1
    at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    receipts = 0; keyless = []
    for e in plan:
        # a receipt is written ONLY when the record's key is on the home: fail closed, the entry stays staged otherwise
        if not e["txid"] or not (home / "keys" / f"{e['txid']}.key").is_file():
            keyless.append(e["txid"] or Path(e["meta"]).name); continue
        write_sweep_receipt(Path(e["meta"]), {"schema": 1, "epoch_id": eid, "txid": e["txid"], "already_archived": e["already"],
                                              "bytes": e["bytes"], "sha256": e["sha256"], "swept_at": at})
        receipts += 1
    report["sweep"]["ciphertext_files_copied"] = copied["archive"]
    report["sweep"]["key_files_copied"] = copied["keys"]
    report["sweep"]["receipts"] = receipts
    if keyless:
        report["sweep"]["no_receipt_key_missing"] = keyless
    # P-D (CSO 2026-09-16): the spool must DRAIN, not double. A receipted payload is reclaimed only after this build
    # decrypts the archived record from the HOME's own ciphertext + key and the plaintext hashes to the meta's sha256.
    # meta + receipt stay (the record of what was captured and where it went); a failed round trip keeps the payload.
    report["sweep"]["reclaimed"] = _reclaim_verified(home, [e for e in plan if e["txid"] and e["txid"] not in keyless], report)


def build(
    home: Path,
    cfg,
    *,
    delta: bool = True,
    confirm_source_removed: tuple[str, ...] = (),
    prune_unreachable: bool = False,
    keep: int = 3,
    max_bytes: int | None = None,
    lbrain_bin: str = "lbrain",
    scratch: Path | None = None,
    sweep: bool = True,
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

            _run_cli(["import", "--prune"] + (["--prune-unreachable"] if prune_unreachable else []),
                     staging, lbrain_bin, lock=lock)
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

            # increment 2 (2026-09-16, CSO mine 2026-09-15T18:46Z: 306 MB / 29 captures staged and read by nothing):
            # sweep the home's capture spool into the CANDIDATE through the legacy `archive capture` path, which the
            # staging home takes because it carries no epochs/CURRENT. Ciphertext lands in staging/archive/ and is copied
            # into home/archive/ only after publish; receipts are written only then, so a failed build leaves every
            # entry staged and the next build re-sweeps (the Archiver skips an already-captured payload).
            # P-F (CSO 2026-09-16): the archive index is not derived from sources, so a --full candidate would drop it;
            # carry the prior epoch's archive tables into the candidate before the sweep (discoverability survives).
            if prior_db is not None and not delta:
                report["archive_index_carried"] = _carry_archive_index(prior_db, staging_db)
            swept_plan = _sweep_spool(home, staging, lbrain_bin, lock, report, dim=int(getattr(cfg, "embedding_dim", 0) or 0)) if sweep else []
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
                _retain_failed(staging, fdir)
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
                # A-576: measured coverage, stamped into the candidate. An epoch
                # without these keys predates the measurement and the contract
                # must report coverage as unverified rather than assume zero.
                con.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                            ("coverage_unscanned", str(_unscanned_count(staging_db, sources))))
                con.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                            ("coverage_checked_at", scan_end))
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
            if swept_plan:
                _finish_sweep(home, staging, eid, swept_plan, report)
            # A-592: the marker is written only after the swap succeeded, beside config.toml, outside the
            # tree it describes; a crash between swap and marker leaves today's (unmarked, served) state.
            try:
                from . import __version__ as _v
            except Exception:
                _v = "unknown"
            from .epoch import write_marker
            write_marker(home, eid, str(_v))
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
