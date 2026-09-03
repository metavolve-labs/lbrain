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
- **Additive only.** Nothing in this module deletes, and shred never stages
  (a deferred shred is a lie). Sweep receipts are increment 2.
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
    payload_path = d / f"{digest[:16]}{PAYLOAD_SUFFIX}"
    meta_path = d / f"{digest[:16]}{META_SUFFIX}"
    if payload_path.exists() and meta_path.exists():
        return SpoolResult(digest, payload_path, meta_path, skipped=True)

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


def staged_items(home: Path) -> list[Path]:
    """COMPLETE entries only (payload + meta). Torn spools don't count."""
    d = staging_dir(home)
    if not d.is_dir():
        return []
    out = []
    for meta in sorted(d.glob(f"*{META_SUFFIX}")):
        stem = meta.name[: -len(META_SUFFIX)]
        if (d / f"{stem}{PAYLOAD_SUFFIX}").is_file():
            out.append(meta)
    return out


def staged_count(home: Path) -> int:
    return len(staged_items(home))
