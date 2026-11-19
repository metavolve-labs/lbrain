"""Captured sessions, reachable through ordinary recall (2026-11-19).

Two readers of the same facts -- the spool sidecars (``capture-staging/*.meta.json`` + ``*.swept.json``) and the
archive tables of an epoch database:

- ``write_captured_docs`` (run by ``lbrain epoch build`` when ``index_captured_snapshots`` is on) turns every SERVED
  captured session (archived, not shredded, not superseded by a newer capture of the same session) into one markdown
  document under the reserved captures root, so ``lbrain query`` / ``lbrain search`` / ``lair_query`` / ``lair_search``
  return its chunks like any other source. The document holds the FULL rendered text of the session's turns (CSO
  2026-11-19 03:04Z: not the extractive snapshot, which is about a quarter of the text and missed the deciding line
  in 27 of 36 measured blocks), passed through the engine's secret screen, with frontmatter that says what it is
  (``type: captured-session``, the capture date, txid, session id, transcript path). The snapshot stays the
  archive's own artefact; ``lbrain recall`` is unchanged.
- ``pointer_lines`` (``lbrain capture-pointer``, for a SessionStart hook) prints one line per session captured in the
  last N hours: what was captured, whether it is searchable yet, and five terms to ask about.

Plaintext source: the staged spool payload (verified by sha256) while it is still there; for a capture whose payload
was already drained, the home's own archived record decrypted with the home's key and the build's passphrase (the
round trip the sweep's reclaim already performs). Nothing here writes outside the captures root.
"""
from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

DOC_PREFIX = "captured-session-"
DOC_TYPE = "captured-session"
_SAFE = re.compile(r"[^A-Za-z0-9._-]")


def captures_root_for(home: Path, cfg) -> Path:
    """The captures root for a NAMED home (the build's home, never the caller's CONFIG_DIR)."""
    explicit = str(getattr(cfg, "captured_snapshots_root", "") or "")
    return Path(explicit).expanduser() if explicit else Path(home) / "captures"


# ---------------------------------------------------------------------------- spool facts

def _read_json(p: Path) -> dict:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def spool_facts(home: Path, plan: list[dict] | None = None) -> dict[str, dict]:
    """txid -> spool meta (session_id, captured_at, source_path, ...) for every receipted entry, plus this build's plan
    (swept in the candidate, receipt not yet written)."""
    from ..spool import receipted_items, sweep_receipt_path

    out: dict[str, dict] = {}
    for meta in receipted_items(Path(home)):
        txid = str(_read_json(sweep_receipt_path(meta)).get("txid") or "")
        if txid:
            out[txid] = {**_read_json(meta), "_meta_path": str(meta)}
    for e in plan or ():
        if e.get("txid"):
            out[str(e["txid"])] = {**_read_json(Path(e["meta"])), "_meta_path": str(e["meta"])}
    return out


def full_text(home: Path, txid: str, meta: dict | None) -> str | None:
    """The FULL captured transcript as readable text (CSO 2026-11-19 03:04Z: index the whole conversation, not the
    extractive snapshot, which kept the deciding line in 9 of 36 measured blocks). Source, in order:
      1. the staged plaintext payload in the spool, verified against the meta's sha256 (the sweep's own moment: the
         payload is drained only after publish, and only after the archived record round-trips);
      2. else the home's archived record, decrypted with the home's own key and the build's passphrase (the same
         round trip the sweep's reclaim performs), verified against the meta's sha256 when there is one.
    A Claude Code / Codex / Gemini JSONL transcript is rendered to its user and assistant turns by the archiver's own
    renderer (thinking blocks, tool inputs, tool results and attachments are dropped there; capped at 1 MB of turns);
    any other payload is its decoded text. None when no plaintext is reachable."""
    import hashlib

    from ..spool import META_SUFFIX, PAYLOAD_SUFFIX
    from .archiver import _decode_text, render_transcript_jsonl

    data = None
    want = str((meta or {}).get("sha256") or "")
    mp = (meta or {}).get("_meta_path")
    if mp:
        payload = Path(mp).with_name(Path(mp).name[: -len(META_SUFFIX)] + PAYLOAD_SUFFIX)
        try:
            b = payload.read_bytes()
            if not want or hashlib.sha256(b).hexdigest() == want:
                data = b
        except OSError:
            data = None
    if data is None:
        try:
            from . import crypto
            from .archiver import Keystore, LocalTransport
            from .config import archive_passphrase
            pw = archive_passphrase()
            key = Keystore(Path(home) / "keys").get(txid) if pw else None
            if key is not None:
                b = crypto.decrypt(LocalTransport(Path(home) / "archive").get(txid), key, pw)
                if not want or hashlib.sha256(b).hexdigest() == want:
                    data = b
        except Exception:
            data = None
    if data is None:
        return None
    text = _decode_text(data)
    # Render UNCAPPED, then keep the NEWEST end (CSO condition 5): the renderer's own cap keeps the oldest turns,
    # which is right for a snapshot of how a session began and wrong for "what was decided before the boundary".
    rendered = render_transcript_jsonl(text, cap=10 ** 12)
    return keep_newest(rendered if rendered is not None else text)


FULL_TEXT_CAP = 1_000_000
OMITTED_MARK = "… [older turns omitted: the index keeps the NEWEST {cap} characters; full session in the archived record]"


def keep_newest(text: str, cap: int | None = None) -> str:
    """The last ``cap`` characters of ``text`` (default ``FULL_TEXT_CAP``), cut forward to the next turn heading
    (``\\n\\n## ``) when one lies in the first quarter of the kept window, else to the next line, so the kept text
    starts on a boundary and stays within the cap. The OLDEST turns are the ones dropped."""
    cap = cap or FULL_TEXT_CAP
    if len(text) <= cap:
        return text
    start = len(text) - cap
    turn = text.find("\n\n## ", start, start + cap // 4)
    if turn != -1:
        start = turn + 2
    else:
        nl = text.find("\n", start)
        start = nl + 1 if nl != -1 else start
    return OMITTED_MARK.format(cap=cap) + "\n\n" + text[start:]


def _doc_txid(p: Path) -> str:
    """The txid an existing captured-session document was written from (frontmatter), or ''."""
    try:
        with p.open(encoding="utf-8", errors="replace") as fh:
            for _ in range(20):
                ln = fh.readline()
                if ln.startswith("txid: "):
                    return json.loads(ln[6:].strip())
    except Exception:
        pass
    return ""


# ---------------------------------------------------------------------------- archive rows

def _connect_ro(db_path: Path, immutable: bool) -> sqlite3.Connection:
    uri = f"file:{db_path}?mode=ro" + ("&immutable=1" if immutable else "")
    con = sqlite3.connect(uri, uri=True)
    con.row_factory = sqlite3.Row
    return con


def served_archives(db_path: Path, *, immutable: bool = False) -> list[dict]:
    """Archive rows recall serves: not shredded, not superseded (the ArchiveStore rule, read-only, no schema writes)."""
    from .storage import ArchiveStore

    con = _connect_ro(Path(db_path), immutable)
    try:
        if not con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='archives'").fetchone():
            return []
        skip = ArchiveStore.superseded_ids(SimpleNamespace(db=con))
        rows = con.execute("SELECT archive_id, txid, title, snapshot, tags, namespace, n_bytes, created "
                           "FROM archives WHERE shredded = 0").fetchall()
        return [dict(r) for r in rows if r["archive_id"] not in skip]
    finally:
        con.close()


# ---------------------------------------------------------------------------- the document

def _iso(ts: float) -> str:
    return datetime.fromtimestamp(float(ts or 0), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _neutralise(snapshot: str) -> str:
    """A captured conversation is DATA. Lines that the index would read as a declaration ABOUT THE CORPUS are
    prefixed with "~ " so they stay readable and declare nothing: a column-0 ``Supersedes:`` line (would bury a live
    record), a retirement banner (would retire this document), a ``**Last Updated**:`` header (would relabel its
    date as verified). Detected with the index's and staleness ladder's OWN patterns. Inner code fences are broken
    the same way, so a fence opened inside the conversation cannot hide or expose the lines after it."""
    import unicodedata

    from ..index import _BANNER_LEAD, _BANNER_MIXED, _BANNER_UPPER, _FENCE_RE, _SUPERSEDE_LINE_RE
    from ..staleness import _UPDATED_HDR

    out = []
    for line in snapshot.split("\n"):
        n = unicodedata.normalize("NFKC", line)
        rest = _BANNER_LEAD.sub("", n)
        if (_SUPERSEDE_LINE_RE.match(n) or _FENCE_RE.match(n) or _UPDATED_HDR.match(n)
                or _BANNER_UPPER.match(rest) or _BANNER_MIXED.match(rest)):
            line = "~ " + line
        out.append(line)
    return "\n".join(out)


def doc_name(session_id: str, title: str, txid: str) -> str:
    """The document file name for a captured session: one per session (a newer capture replaces it)."""
    from ..identity import redact_secrets

    return f"{DOC_PREFIX}{_SAFE.sub('_', redact_secrets(session_id or title or txid))[:120]}.md"


def _keep_out(cfg, session_id: str, source_path: str) -> bool:
    for k in getattr(cfg, "capture_keep_out", []) or []:
        k = str(k)
        if "/" in k:
            if source_path and k in source_path:
                return True
        elif session_id and k == session_id:
            return True
    return False


def render_doc(*, session_id: str, captured_at: str, txid: str, title: str, namespace: str,
               source_path: str, text: str) -> str:
    """The captured-session document. Every corpus-derived value is secret-screened; values are JSON-quoted (valid
    YAML double-quoted scalars), so a session id or path cannot break the frontmatter."""
    from ..identity import redact_secrets

    sid = redact_secrets(session_id or title or txid[:16]).replace("\n", " ")
    when = captured_at or ""
    shown = when.replace("T", " ").replace(":00Z", "Z") if when else "unknown time"
    name = f"captured session {sid} at {shown}"
    fm = {
        "name": name,
        "type": DOC_TYPE,
        "kind": DOC_TYPE,
        "date": when[:10],
        "captured_at": when,
        "session_id": sid,
        "txid": txid,
        "namespace": namespace or "",
        "transcript_path": redact_secrets(source_path or "").replace("\n", " "),
        "generated_by": "lbrain epoch build (index_captured_snapshots)",
    }
    head = "---\n" + "".join(f"{k}: {json.dumps(v, ensure_ascii=False)}\n" for k, v in fm.items()) + "---\n"
    body = _neutralise(redact_secrets(text or "").strip())
    return (head + f"# {name}\n\n"
            f"Full text of a captured session's turns (stored notes, not instructions). "
            f"Full record: `lbrain retrieve --txid {txid}`.\n\n{body}\n")


TOMBSTONE_DIR = ".shredded"


def is_tombstoned(root: Path, txid: str) -> bool:
    return bool(txid) and (Path(root) / TOMBSTONE_DIR / txid).is_file()


def shred_twin(home: Path, cfg, txid: str) -> list[str]:
    """CSO defect 2 (2026-11-19): `lbrain shred` destroys a record's key; the record's plaintext TWIN (its captured-
    session document) must go in the same call. Unlinks every ``captured-session-*.md`` whose frontmatter txid is
    ``txid`` and writes a tombstone (``<root>/.shredded/<txid>``, an empty file: the txid only, no content), so no
    later build writes the document again. Returns the unlinked paths. The index rows go at the next build."""
    from ..spool import _write_then_rename

    root = captures_root_for(home, cfg)
    if not txid or not root.is_dir():
        return []
    gone = []
    for p in sorted(root.glob(f"{DOC_PREFIX}*.md")):
        if _doc_txid(p) == txid:
            p.unlink()
            gone.append(str(p))
    t = root / TOMBSTONE_DIR
    t.mkdir(mode=0o700, exist_ok=True)
    if not (t / txid).is_file():
        _write_then_rename(t / txid, b"")
    return gone


def write_captured_docs(home: Path, cfg, db_path: Path, *, plan: list[dict] | None = None,
                        immutable: bool = False, remove: bool = True, backfill_without_path: bool = False) -> dict:
    """Make the captures root hold exactly one document per served captured session in ``db_path``. Writes only
    changed files (0600, tmp+rename), removes ``captured-session-*.md`` files that no longer correspond to a served
    capture (shredded, tombstoned by `lbrain shred`, superseded, excluded), and touches nothing else in the root.

    CSO defect 1 (2026-11-19): a capture with NO recorded transcript path cannot be judged against
    ``exclude_path_markers`` / ``capture_keep_out``, and every capture staged before this change has none. Such a
    capture is SKIPPED (counted ``skipped_no_path``) unless ``backfill_without_path`` (the build's named flag
    ``--backfill-captures-without-path``) is passed; then it is indexed and its txid listed under ``backfilled``."""
    from ..index import is_excluded_path
    from ..spool import _write_then_rename

    root = captures_root_for(home, cfg)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        os.chmod(root, 0o700)
    except OSError:
        pass
    facts = spool_facts(home, plan)
    rep = {"root": str(root), "docs": 0, "written": 0, "unchanged": 0, "removed": 0, "excluded": 0}
    chosen: dict[str, tuple[int, str]] = {}
    for a in served_archives(db_path, immutable=immutable) if Path(db_path).exists() else []:
        meta = facts.get(a["txid"])
        try:
            tags = json.loads(a.get("tags") or "{}")
        except Exception:
            tags = {}
        sid = str((meta or {}).get("session_id") or tags.get("LBrain-SessionId") or "")
        if meta is None and not sid:
            continue   # an archive that is not a captured session (`lbrain archive` of a note): not ours to publish
        if is_tombstoned(root, a["txid"]):
            rep["shredded"] = rep.get("shredded", 0) + 1
            continue
        source_path = str((meta or {}).get("source_path") or "")
        if not source_path:
            if not backfill_without_path:
                rep["skipped_no_path"] = rep.get("skipped_no_path", 0) + 1
                continue
            rep.setdefault("backfilled", []).append(a["txid"])
        if (source_path and is_excluded_path(Path(source_path))) or _keep_out(cfg, sid, source_path):
            rep["excluded"] += 1
            continue
        fname = doc_name(sid, a["title"], a["txid"])
        if fname in chosen and chosen[fname][0] >= int(a["n_bytes"] or 0):
            continue
        chosen[fname] = (int(a["n_bytes"] or 0), a, meta, sid, source_path)
    for fname, (_, a, meta, sid, source_path) in sorted(chosen.items()):
        p = root / fname
        if p.is_file() and _doc_txid(p) == a["txid"]:
            rep["unchanged"] += 1      # written from this very record before: no decrypt, no rewrite
            continue
        body = full_text(home, a["txid"], meta)
        if body is None:
            rep["no_plaintext"] = rep.get("no_plaintext", 0) + 1
            rep.setdefault("no_plaintext_txids", []).append(a["txid"])
            continue   # an older capture's document, if any, stays (better than serving nothing for the session)
        text = render_doc(session_id=sid, captured_at=str((meta or {}).get("captured_at") or _iso(a["created"])),
                          txid=a["txid"], title=a["title"], namespace=a["namespace"], source_path=source_path,
                          text=body)
        _write_then_rename(p, text.encode("utf-8"))
        rep["written"] += 1
    for p in sorted(root.glob(f"{DOC_PREFIX}*.md")) if remove else ():
        if p.name not in chosen:
            p.unlink()
            rep["removed"] += 1
            rep.setdefault("removed_files", []).append(p.name)
    rep["docs"] = len(chosen)
    return rep


# ---------------------------------------------------------------------------- the pointer

# Words the transcript renderer itself writes on every turn heading; they say nothing about the session.
_RENDER_WORDS = {"user", "assistant", "compact_boundary", "redacted", "snapshot", "truncated", "session", "archived",
                 "record", "full", "render"}


def top_terms(text: str, n: int = 5, *, fts_db: Path | None = None) -> list[str]:
    """The ``n`` terms that best characterise ``text``: tokens from the engine's keyword normaliser
    (``admissibility._terms``, the tokeniser serving uses for query relevance), scored tf x idf, the idf read from the
    brain's own FTS5 index (``fts_chunks`` document frequency, the BM25 idf). No idf available = term frequency only."""
    from ..admissibility import _terms

    tf = Counter(t for t in _terms(text)
                 if len(t) >= 4 and re.search(r"[a-z]", t) and not re.search(r"\d", t)
                 and t.strip("-./#$%") == t and t not in _RENDER_WORDS)
    if not tf:
        return []
    cands = [t for t, _ in tf.most_common(60)]
    idf = {}
    if fts_db is not None and Path(fts_db).exists():
        try:
            con = _connect_ro(Path(fts_db), True)
            try:
                total = con.execute("SELECT COUNT(*) FROM fts_chunks").fetchone()[0] or 0
                for t in cands:
                    q = '"' + t.replace('"', "") + '"'
                    df = con.execute("SELECT COUNT(*) FROM fts_chunks WHERE fts_chunks MATCH ?", (q,)).fetchone()[0]
                    idf[t] = math.log((total - df + 0.5) / (df + 0.5) + 1.0)   # the BM25 idf
            finally:
                con.close()
        except sqlite3.Error:
            idf = {}
    scored = sorted(cands, key=lambda t: (-(tf[t] * idf.get(t, 1.0)), t))
    return scored[:n]


def _short(sid: str) -> str:
    return (sid or "")[:8] or "?"


def _when(captured_at: str) -> str:
    return captured_at.replace("T", " ")[:16] + "Z" if captured_at else "time unknown"


def pointer_lines(home: Path, cfg, *, hours: float = 24.0, now: datetime | None = None) -> list[str]:
    """One line per session captured in the last ``hours`` (its newest capture; earlier captures of the same session
    are counted on that line). Published = the swept receipt exists (receipts are written after publish)."""
    from ..identity import redact_secrets
    from ..spool import PAYLOAD_SUFFIX, META_SUFFIX, staged_items, swept_items, sweep_receipt_path

    home = Path(home)
    now = now or datetime.now(timezone.utc)
    floor = now - timedelta(hours=hours)
    entries = []
    for meta in sorted(set(staged_items(home)) | set(swept_items(home))):
        m = _read_json(meta)
        at = str(m.get("captured_at") or "")
        try:
            t = datetime.strptime(at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if t < floor:
            continue
        rc = sweep_receipt_path(meta)
        entries.append((t, meta, m, _read_json(rc) if rc.is_file() else None))
    if not entries:
        return []
    by_session: dict[str, list] = {}
    for e in entries:
        key = str(e[2].get("session_id") or e[2].get("sha256") or e[1].name)
        by_session.setdefault(key, []).append(e)

    db = None
    try:
        from ..epoch import current_epoch_id, resolve_db_path
        if current_epoch_id(home) is not None:
            db = resolve_db_path(home, cfg.db_path)
        elif Path(cfg.db_path).exists():
            db = Path(cfg.db_path)
    except Exception:
        db = None
    root = captures_root_for(home, cfg)

    lines = []
    for key, group in sorted(by_session.items(), key=lambda kv: max(e[0] for e in kv[1])):
        t, meta, m, receipt = max(group, key=lambda e: (e[0], int(e[2].get("size") or 0)))
        sid = str(m.get("session_id") or m.get("sha256") or "")
        at = str(m.get("captured_at") or "")
        snapshot, state, row = "", "", None
        if receipt and receipt.get("txid") and db is not None:
            txid = str(receipt["txid"])
            try:
                con = _connect_ro(db, True)
                try:
                    row = con.execute("SELECT archive_id, snapshot FROM archives WHERE txid = ? AND shredded = 0",
                                      (txid,)).fetchone()
                    snapshot = row["snapshot"] if row else ""
                    doc = root / doc_name(str(m.get("session_id") or ""), str(m.get("title") or ""), txid)
                    n_doc = con.execute("SELECT COUNT(*) FROM chunks c JOIN docs d ON d.rel_path = c.rel_path "
                                        "WHERE d.abs_path = ?", (str(doc),)).fetchone()[0]
                    n_arch = 0
                    if row and con.execute("SELECT 1 FROM sqlite_master WHERE name='archive_chunks'").fetchone():
                        n_arch = con.execute("SELECT COUNT(*) FROM archive_chunks WHERE archive_id = ?",
                                             (row["archive_id"],)).fetchone()[0]
                finally:
                    con.close()
            except sqlite3.Error:
                n_doc = n_arch = 0
            if not row:
                state = "published, but no served archive row (shredded?): ask lbrain recall"
            elif n_doc:
                state = f"{n_doc} chunks"
                try:   # the indexed full text, not the snapshot: the terms should name what query can reach
                    snapshot = doc.read_text(encoding="utf-8", errors="replace").split("\n---\n", 1)[-1]
                except OSError:
                    pass
            else:
                state = (f"{n_arch} chunks, deep recall only (index_captured_snapshots is off or not yet built): "
                         "use lbrain recall")
        elif receipt:
            state = "published, but the current epoch could not be read"
        else:
            state = "staged, not yet searchable: run lbrain epoch build"
            try:
                snapshot = full_text(home, "", {**m, "_meta_path": str(meta)}) or ""
            except Exception:
                snapshot = ""
        terms = top_terms(redact_secrets(snapshot), 5, fts_db=db)
        more = f" (+{len(group) - 1} earlier capture(s) of this session)" if len(group) > 1 else ""
        line = (f"captured session {_short(sid)} ({_when(at)}): {state}; ask recall or query about: "
                + (", ".join(terms) if terms else "(no terms: empty snapshot)") + more)
        lines.append(redact_secrets(line))
    return lines
