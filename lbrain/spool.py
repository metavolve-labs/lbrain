"""Capture-staging spool — item-6 increment 1 (CSO design 2026-09-02T06:20Z).

On an epoch-managed home, build→validate→swap is the only database write path,
so hook-driven session capture cannot open the store — and before this module
it simply refused, starving capture the day a seat home flips (the gap
``909d302`` deliberately opened). The spool is the epoch-compatible landing
strip: an ADDITIVE, CONTENT-ADDRESSED directory of raw captures that ``epoch
build`` sweeps into the next epoch (increment 2).

Contract, in order of what must never break:

- **The gates apply to the spool exactly as to the db.** A spool write is a
  file write and would naturally dodge every gate; unacceptable. W1 (home
  coherence) and W2 (seat identity) run via ``check_write_target`` BEFORE any
  byte lands. Only the EPOCH refusal is exempt — that exemption is the entire
  feature.
- **The spool path derives from the NAMED home, never from config
  ``db_path``** — foreign materialization is impossible by construction, not
  by gate (the 2026-09-01 db_path incident class).
- **Additive only, in this module.** Nothing here deletes, and shred never stages
  (a deferred shred is a lie). Sweep receipts are increment 2. The ONE reclaim of a
  payload lives in ``epoch_build._reclaim_verified`` and fires only after the archived
  record decrypts from the home's own ciphertext + key to the meta's sha256 (2026-09-16,
  CSO P-D: the spool must drain, not double); meta + receipt remain as the record.
- **Content-addressed idempotency**: the same payload spools to the same name;
  a re-fire is a skip, a concurrent race is an overwrite-with-identical.
- **Crash ordering**: payload writes tmp→fsync→rename, then the ``.meta.json``
  sidecar renames LAST. Meta presence == entry complete; a payload without
  meta is a torn spool, invisible to counts and safely re-spooled.

The spool sits at the home ROOT (``capture-staging/``), deliberately outside
``epochs/`` so epoch prune can never eat staged work.

Raw payloads land unencrypted here, like the plaintext chunks in ``brain.db``
one directory over: the home directory is already the trust boundary, and the
archiver encrypts at sweep time. Files are created 0600 (dir 0700) best-effort.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

STAGING_DIRNAME = "capture-staging"
PAYLOAD_SUFFIX = ".transcript"
META_SUFFIX = ".meta.json"
# 128 bits of the sha256 in the filename (RED-SPOOL-1, CTO tear-up 2026-09-03):
# 64 bits made an accidental prefix collision merely improbable and an
# attacker-influenced one thinkable — transcripts are exactly the
# poisoned-content surface. 128 ends the conversation, and the skip path
# below dereferences the stored FULL hash regardless: a name is a reference,
# never a verification.
NAME_HEX = 32


class SpoolIntegrityError(Exception):
    """A pre-existing spool entry's stored hash does not match the content its
    name claims — silent capture loss (collision or tampering) refused LOUD."""


def staging_dir(home: Path) -> Path:
    return Path(home) / STAGING_DIRNAME


@dataclass
class SpoolResult:
    sha256: str
    payload_path: Path
    meta_path: Path
    skipped: bool


def _write_then_rename(target: Path, data: bytes) -> None:
    """tmp + fsync + rename: a crash strands a ``.tmp``, never a partial file."""
    tmp = target.with_name(target.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    os.replace(tmp, target)


def spool_capture(cfg, home: Path, payload: bytes, *, session_id: str | None,
                  title: str | None, namespace: str | None) -> SpoolResult:
    """Stage one capture. Raises ``WriteGateError`` on W1/W2 refusal."""
    from .write_gates import check_write_target

    home = Path(home)
    # W1/W2 BEFORE any byte lands. Epoch state is deliberately not consulted:
    # the spool exists precisely so an epoch-managed home can accept capture.
    check_write_target(cfg, home)

    digest = hashlib.sha256(payload).hexdigest()
    d = staging_dir(home)
    payload_path = d / f"{digest[:NAME_HEX]}{PAYLOAD_SUFFIX}"
    meta_path = d / f"{digest[:NAME_HEX]}{META_SUFFIX}"

    # RED-SPOOL-1: the skip path must DEREFERENCE, never trust the name.
    # An entry already at this name is only "this capture" if its stored full
    # hash matches the new payload's — otherwise skipping silently LOSES the
    # new capture behind a colliding/tampered one.
    if meta_path.exists():
        try:
            stored = json.loads(meta_path.read_text(encoding="utf-8")).get("sha256", "")
        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise SpoolIntegrityError(
                f"spool meta {meta_path} is unreadable/unparseable ({exc}) — cannot "
                f"verify the existing entry against new capture sha256 {digest}; "
                "refusing to skip OR overwrite. Inspect the entry."
            )
        if stored != digest:
            raise SpoolIntegrityError(
                f"spool name collision at {payload_path.name}: existing entry stores "
                f"sha256 {stored}, new capture is sha256 {digest} — refusing to skip "
                "(silent capture loss) and refusing to overwrite (the spool is "
                "additive-only). Inspect the entry."
            )
        if payload_path.exists():
            return SpoolResult(digest, payload_path, meta_path, skipped=True)
        # meta without payload: not a shape our crash ordering produces (meta
        # renames LAST) — fall through and rewrite the payload for this meta.
    elif payload_path.exists():
        # Torn spool (payload renamed, crash before meta). The stored bytes are
        # complete (tmp+rename) — verify they ARE this content before adopting.
        stored_digest = hashlib.sha256(payload_path.read_bytes()).hexdigest()
        if stored_digest != digest:
            raise SpoolIntegrityError(
                f"torn spool entry {payload_path.name} holds sha256 {stored_digest}, "
                f"new capture is sha256 {digest} — name collision on an incomplete "
                "entry; refusing to overwrite. Inspect the entry."
            )

    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    _write_then_rename(payload_path, payload)
    meta = {
        "schema": 1,
        "sha256": digest,
        "size": len(payload),
        "session_id": session_id,
        "title": title,
        "namespace": namespace,
        "captured_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    # Meta renames LAST: its presence is the completeness marker.
    _write_then_rename(meta_path, json.dumps(meta, indent=2).encode("utf-8"))
    return SpoolResult(digest, payload_path, meta_path, skipped=False)


SWEPT_SUFFIX = ".swept.json"


def sweep_receipt_path(meta: Path) -> Path:
    """Increment 2 (2026-09-16): the sweep receipt sidecar for a staged entry. Its presence means the entry was
    archived into a published epoch; the payload and meta stay (additive: nothing here deletes)."""
    return meta.with_name(meta.name[: -len(META_SUFFIX)] + SWEPT_SUFFIX)


def write_sweep_receipt(meta: Path, receipt: dict) -> Path:
    """tmp→fsync→rename like every spool write; the receipt renames LAST so a crash leaves the entry staged."""
    p = sweep_receipt_path(meta)
    _write_then_rename(p, json.dumps(receipt, indent=2).encode("utf-8"))
    return p


def staged_items(home: Path, *, include_swept: bool = False) -> list[Path]:
    """COMPLETE entries only (payload + meta). Torn spools don't count. Swept entries (a ``.swept.json`` receipt
    beside them) are excluded unless ``include_swept``: they are archived, not awaiting a build."""
    d = staging_dir(home)
    if not d.is_dir():
        return []
    out = []
    for meta in sorted(d.glob(f"*{META_SUFFIX}")):
        stem = meta.name[: -len(META_SUFFIX)]
        if not (d / f"{stem}{PAYLOAD_SUFFIX}").is_file():
            continue
        if not include_swept and sweep_receipt_path(meta).is_file():
            continue
        out.append(meta)
    return out


def keyless_receipts(home: Path) -> list[tuple[Path, str]]:
    """Increment 2b: receipted entries whose txid has no wrapped key under <home>/keys/ -- false receipts (the record is
    undecryptable). Returned as (meta, txid) so a build can re-stage them."""
    out = []
    keys = home / "keys"
    for meta in staged_items(home, include_swept=True):
        rp = sweep_receipt_path(meta)
        if not rp.is_file():
            continue
        try:
            txid = str(json.loads(rp.read_text(encoding="utf-8")).get("txid") or "")
        except Exception:
            txid = ""
        if not txid or not (keys / f"{txid}.key").is_file():
            out.append((meta, txid))
    return out


def swept_items(home: Path) -> list[Path]:
    """Entries that carry a sweep receipt (payload + meta + receipt)."""
    return [m for m in staged_items(home, include_swept=True) if sweep_receipt_path(m).is_file()]


def staged_count(home: Path) -> int:
    return len(staged_items(home))
