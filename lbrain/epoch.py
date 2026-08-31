"""Atomic epochs — build-validate-swap as the ONLY write path into a brain.

Design: ATOMIC-EPOCHS-DESIGN-2026-08-31.md v1.4 (P3/000-PRIORITY-AGENT-X-EXOSKELETON),
hardened by a 3-reviewer external panel + the CSO consult before this file existed.
This module is the PROTOCOL layer: epoch layout, the CURRENT pointer with its full
durability chain, the builder lock, reader leases, and prune. The build/validate
orchestration lives in `epoch_build.py`; neither is reachable unless a home opts in
(`epochs = true`), so default behavior is byte-for-byte unchanged.

The four measured incident classes this exists to kill: interrupted-rebuild leaving a
live brain at 12.7% coverage; zero-vector promotion reporting success; /mnt/c per-row
write hangs; indexes going quietly stale. Every design choice below cites the finding
that forced it.
"""

from __future__ import annotations

import errno
import json
import os
import shutil
import socket
import time
from pathlib import Path

EPOCHS_DIRNAME = "epochs"
CURRENT_NAME = "CURRENT"
LOCK_DIRNAME = ".builder.lock.d"
LEASES_DIRNAME = ".leases"
FAILED_SUFFIX = ".failed"

# Heartbeat lease (design v1.2 #6 + vendor3): staleness is decided by heartbeat AGE,
# never by PID liveness — PID polling deadlocks after a reboot hands the saved PID to
# an unrelated process. pid/host/starttime are recorded for DIAGNOSTICS only.
HEARTBEAT_INTERVAL = 15.0
STALE_AFTER = 60.0


class EpochError(RuntimeError):
    pass


class BuilderBusy(EpochError):
    """Another builder holds the lock and its heartbeat is fresh."""


# ---------- layout ----------

def epochs_root(home: Path) -> Path:
    return Path(home) / EPOCHS_DIRNAME


def epoch_dir(home: Path, epoch_id: str) -> Path:
    return epochs_root(home) / epoch_id


def epoch_db(home: Path, epoch_id: str) -> Path:
    """The published database of an epoch: a SINGLE checkpointed file (panel #1 —
    journal_mode=DELETE via VACUUM INTO; -wal/-shm are never published)."""
    return epoch_dir(home, epoch_id) / "brain.db"


def new_epoch_id(now: float | None = None) -> str:
    ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now if now is not None else time.time()))
    return f"{ts}-{os.getpid()}"


def failed_dir(home: Path, epoch_id: str) -> Path:
    return epochs_root(home) / (epoch_id + FAILED_SUFFIX)


# ---------- the CURRENT pointer ----------

def current_epoch_id(home: Path) -> str | None:
    """The published epoch id, or None when the home has never published one
    (legacy layout — brain.db at the home root stays authoritative)."""
    p = epochs_root(home) / CURRENT_NAME
    try:
        eid = p.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    return eid or None


def resolve_db_path(home: Path, legacy_db_path: Path) -> Path:
    """The database a reader should open right now.

    Epoch layout wins only when CURRENT names an epoch whose db actually exists —
    a dangling pointer falls back loudly rather than silently serving nothing.
    """
    eid = current_epoch_id(home)
    if eid is None:
        return Path(legacy_db_path)
    db = epoch_db(home, eid)
    if not db.exists():
        raise EpochError(
            f"CURRENT names epoch {eid!r} but {db} does not exist — refusing to fall "
            "back silently; restore a prior epoch or remove the pointer deliberately"
        )
    return db


def _fsync_path(path: Path) -> None:
    fd = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _fsync_dir_tolerant(path: Path) -> str:
    """fsync a DIRECTORY, tolerating the documented DrvFs failure.

    v1.3 #1 (advisor maiden run, verified lead): directory fsync raises
    EINVAL/EACCES on /mnt/c — FlushFileBuffers rejects directory handles across the
    9p bridge. On such a home the rename is atomic for READERS but its durability
    across a crash cannot be forced; we tolerate, and RETURN the caveat so callers
    print it (doctor surfaces it too). On a POSIX home this must succeed.
    """
    try:
        _fsync_path(path)
        return ""
    except OSError as e:
        if e.errno in (errno.EINVAL, errno.EACCES):
            return (
                f"directory fsync unsupported on {path} (errno {e.errno}) — pointer "
                "swap is atomic for readers but NOT crash-durable on this filesystem; "
                "authoritative homes belong on ext4 (design v1.3 #1)"
            )
        raise


def publish(home: Path, epoch_id: str, *, retries: int = 5, backoff: float = 0.2) -> str:
    """Atomically repoint CURRENT at `epoch_id`. Returns "" or a durability caveat.

    Durability chain (panel #1, the LWN/Ts'o hole): write tmp → fsync the FILE and
    check the error → rename → fsync the PARENT DIRECTORY. A crash after renaming an
    un-fsynced pointer boots into an incomplete epoch — the elegant version of
    "promoted zero vectors and reported success".

    The rename retries with backoff (CSO D3): on DrvFs, replacing a file a Windows
    process holds open can raise a sharing violation dressed as EACCES/EPERM.
    """
    root = epochs_root(home)
    if not epoch_db(home, epoch_id).exists():
        raise EpochError(f"refusing to publish {epoch_id!r}: {epoch_db(home, epoch_id)} missing")
    root.mkdir(parents=True, exist_ok=True)
    tmp = root / (CURRENT_NAME + ".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        os.write(fd, (epoch_id + "\n").encode("utf-8"))
        os.fsync(fd)  # a failed fsync here MUST abort — check the error, don't pray
    finally:
        os.close(fd)
    target = root / CURRENT_NAME
    last: Exception | None = None
    for attempt in range(retries):
        try:
            os.replace(str(tmp), str(target))
            last = None
            break
        except OSError as e:
            last = e
            time.sleep(backoff * (attempt + 1))
    if last is not None:
        raise EpochError(f"pointer rename failed after {retries} attempts: {last}")
    return _fsync_dir_tolerant(root)


# ---------- the builder lock ----------

class BuilderLock:
    """mkdir spin-lock + heartbeat lease. One builder per brain home.

    mkdir/rmdir is the classic network-fs-safe mutual exclusion (v1.3 #2) — O_EXCL
    is NFS-class unreliable on 9p and stays banned here. Staleness = heartbeat age
    only (reboot-safe, PID-reuse-safe). Takeover renames the stale lock aside
    rather than deleting into a race.

    Stated assumption, unchanged from the design: one kernel per brain home. This
    lock arbitrates processes on one kernel; hazard #11 says nothing can arbitrate
    two kernels over an async store, and we do not pretend otherwise.
    """

    def __init__(self, home: Path, *, stale_after: float = STALE_AFTER):
        self.dir = epochs_root(home) / LOCK_DIRNAME
        self.meta = self.dir / "builder.json"
        self.stale_after = stale_after
        self.held = False

    def _write_meta(self) -> None:
        body = json.dumps({
            "host": socket.gethostname(),
            "pid": os.getpid(),
            "heartbeat": time.time(),
        })
        tmp = self.dir / "builder.json.tmp"
        tmp.write_text(body, encoding="utf-8")
        os.replace(str(tmp), str(self.meta))

    def _meta_age(self) -> float | None:
        try:
            m = json.loads(self.meta.read_text(encoding="utf-8"))
            return time.time() - float(m.get("heartbeat", 0))
        except FileNotFoundError:
            return None
        except Exception:
            return float("inf")  # unreadable meta = treat as stale, not as live

    def acquire(self) -> "BuilderLock":
        self.dir.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(2):  # second pass only after a stale takeover
            try:
                self.dir.mkdir()
                self._write_meta()
                self.held = True
                return self
            except FileExistsError:
                age = self._meta_age()
                if age is None:
                    # dir exists but no meta yet: the other builder is mid-acquire —
                    # that is a LIVE builder, not a stale one.
                    raise BuilderBusy("another builder is acquiring the lock right now")
                if age <= self.stale_after:
                    raise BuilderBusy(
                        f"another builder holds the lock (heartbeat {age:.0f}s ago)")
                # Stale: rename aside (atomic claim of the takeover — two takeover
                # racers cannot both succeed at renaming the same directory).
                aside = self.dir.with_name(
                    LOCK_DIRNAME + f".stale-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{os.getpid()}")
                try:
                    os.rename(str(self.dir), str(aside))
                except OSError:
                    raise BuilderBusy("lost the stale-lock takeover race — retry later")
        raise BuilderBusy("could not acquire the builder lock")

    def heartbeat(self) -> None:
        if self.held:
            self._write_meta()

    def release(self) -> None:
        if not self.held:
            return
        try:
            self.meta.unlink(missing_ok=True)
            self.dir.rmdir()
        finally:
            self.held = False

    def __enter__(self) -> "BuilderLock":
        return self.acquire()

    def __exit__(self, *exc) -> None:
        self.release()


# ---------- reader leases + prune ----------

def lease_path(home: Path, epoch_id: str) -> Path:
    return epochs_root(home) / LEASES_DIRNAME / epoch_id / f"{socket.gethostname()}-{os.getpid()}"


def lease_acquire(home: Path, epoch_id: str) -> Path:
    p = lease_path(home, epoch_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(str(time.time()), encoding="utf-8")
    return p


def lease_release(home: Path, epoch_id: str) -> None:
    p = lease_path(home, epoch_id)
    try:
        p.unlink()
        if not any(p.parent.iterdir()):
            p.parent.rmdir()
    except OSError:
        pass


def leased_epochs(home: Path) -> set[str]:
    root = epochs_root(home) / LEASES_DIRNAME
    if not root.is_dir():
        return set()
    return {d.name for d in root.iterdir() if d.is_dir() and any(d.iterdir())}


def list_epochs(home: Path) -> list[str]:
    """Published-shape epoch dirs, oldest first. Excludes .failed (kept as forensics,
    pruned only by explicit human intent — the .failed-wholesale-* precedent)."""
    root = epochs_root(home)
    if not root.is_dir():
        return []
    out = []
    for d in root.iterdir():
        if not d.is_dir() or d.name.startswith(".") or d.name.endswith(FAILED_SUFFIX):
            continue
        out.append(d.name)
    return sorted(out)


def _dir_bytes(path: Path) -> int:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def prune(home: Path, *, keep: int = 3, max_bytes: int | None = None) -> list[str]:
    """Remove old epochs beyond `keep` (and beyond `max_bytes` total — Grok #5: N=3
    of large vector DBs is the first quota incident; cap BYTES, not just count).

    Never removed: CURRENT, leased epochs (reference-counted — Lucene's
    IndexDeletionPolicy lesson), .failed forensics. A deletion failure (DrvFs
    sharing violation) is logged loudly and SKIPPED — never retried in a loop that
    blocks rebuilds for the lifetime of the longest reader (Grok G2).
    """
    current = current_epoch_id(home)
    leased = leased_epochs(home)
    epochs = list_epochs(home)
    # `keep` = how many PRIOR (non-current, non-leased) epochs to retain, newest wins.
    candidates = [e for e in epochs if e != current and e not in leased]
    doomed = candidates[:-keep] if keep > 0 else candidates[:]
    survivors = [e for e in candidates if e not in doomed]  # oldest first
    if max_bytes is not None:
        total = sum(_dir_bytes(epoch_dir(home, e)) for e in epochs if e not in doomed)
        for e in survivors[:]:
            if total <= max_bytes:
                break
            total -= _dir_bytes(epoch_dir(home, e))
            doomed.append(e)
            survivors.remove(e)
    removed: list[str] = []
    for e in doomed:
        try:
            shutil.rmtree(epoch_dir(home, e))
            removed.append(e)
        except OSError as err:
            print(f"[lbrain] WARNING: prune of epoch {e} failed ({err}) — skipped, "
                  "not retried; a reader or a Windows-side process may hold it open")
    return removed
