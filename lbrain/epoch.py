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
import itertools
import json
import os
import re
import shutil
import socket
import time
from datetime import datetime, timezone
from pathlib import Path

EPOCHS_DIRNAME = "epochs"
CURRENT_NAME = "CURRENT"
LOCK_DIRNAME = ".builder.lock.d"
LEASES_DIRNAME = ".leases"
FAILED_SUFFIX = ".failed"
BUILDING_SUFFIX = ".building"

# Heartbeat lease (design v1.2 #6 + vendor3), amended 2026-10-18: a stale heartbeat
# is necessary but NOT sufficient. When the holder is on THIS host and THIS boot (the
# recorded boot_id matches), its PID is probed: a live PID (same process start time)
# keeps the lock however old the heartbeat. Age alone decides only when liveness is
# unknowable (foreign host, no boot_id). The reboot hazard the original rule guarded
# against is closed by the boot_id comparison: a different boot = provably dead.
# Incident: CCO seat 2026-10-18, a live builder in a 7-minute in-process phase (gate +
# VACUUM INTO, no heartbeat) was seized at 61 s by a second builder.
HEARTBEAT_INTERVAL = 15.0
STALE_AFTER = 60.0


class EpochError(RuntimeError):
    pass


class BuilderBusy(EpochError):
    """Another builder holds the lock and its heartbeat is fresh."""


class LockLost(EpochError):
    """This builder's lock was seized (stale takeover) — the build MUST abort.

    CSO R1b: without ownership verification, a deposed builder's next heartbeat
    silently recreated meta inside the usurper's lock dir — two builders each
    believing they held it, the victim feeding the usurper's staleness clock."""


# ---------- layout ----------

def epochs_root(home: Path) -> Path:
    return Path(home) / EPOCHS_DIRNAME


def epoch_dir(home: Path, epoch_id: str) -> Path:
    return epochs_root(home) / epoch_id


def epoch_db(home: Path, epoch_id: str) -> Path:
    """The published database of an epoch: a SINGLE checkpointed file (panel #1 —
    journal_mode=DELETE via VACUUM INTO; -wal/-shm are never published)."""
    return epoch_dir(home, epoch_id) / "brain.db"


_epoch_seq = itertools.count(1)


def new_epoch_id(now: float | None = None) -> str:
    # ts+pid alone COLLIDED when two builds ran in the same second of the same
    # process ("output file already exists" at VACUUM INTO) — exposed 2026-09-01
    # by the cold-CI shim making builds sub-second, masked before only because a
    # real embed takes minutes. The per-process sequence closes it; zero-padded
    # so lexical order within a second stays chronological.
    ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now if now is not None else time.time()))
    return f"{ts}-{os.getpid()}-{next(_epoch_seq):04d}"


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


def serving_db_path(cfg) -> str:
    """What a STATUS display should print as the database: the epoch db actually served,
    never the config path. After the legacy file is tombstoned, printing cfg.db_path names a
    file that no longer exists (found 2026-09-05, Wave 0 follow-up). A dangling CURRENT is
    reported as text, not raised — this is a display, and the store already fails loudly."""
    legacy = Path(getattr(cfg, "db_path", ""))
    try:
        return str(resolve_db_path(legacy.parent, legacy))
    except EpochError as e:
        return f"{legacy}  (EPOCH ERROR: {e})"


MARKER_NAME = "EPOCH-MANAGED.json"

EPOCH, LEGACY, EPOCH_TREE_MISSING = "EPOCH", "LEGACY", "EPOCH_TREE_MISSING"


def marker_path(home: Path) -> Path:
    return Path(home) / MARKER_NAME


def home_shape(home: Path) -> str:
    """ONE answer to "what kind of home is this", shared by every read AND write path (A-592).

    EPOCH               epochs/CURRENT is readable — the epoch tree is present.
    EPOCH_TREE_MISSING  no readable pointer, but <home>/EPOCH-MANAGED.json says a publish happened here:
                        the tree is gone (or a restore is mid-flight). Serving the root database now would
                        be the silent "0 hits" the A5 row measured, and writing it would be buried by the
                        next Route 1 restore. Every caller REFUSES, naming both routes.
    LEGACY              neither pointer nor marker: a home that never published, served as it always was.
    An absent pointer alone is never read as lost history; only the marker, written by a publish that
    happened, distinguishes the second case from the third (CCO, 2026-09-14).
    """
    if current_epoch_id(home) is not None:
        return EPOCH
    if marker_path(home).exists():
        return EPOCH_TREE_MISSING
    return LEGACY


def read_marker(home: Path) -> dict:
    try:
        return json.loads(marker_path(home).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def write_marker(home: Path, epoch_id: str, engine_version: str) -> None:
    """Written by a SUCCESSFUL publish, after the swap, beside config.toml — outside the tree it describes.
    first_epoch_id is preserved across publishes; the rest track the latest."""
    prev = read_marker(home)
    rec = {"first_epoch_id": prev.get("first_epoch_id") or epoch_id, "last_epoch_id": epoch_id,
           "last_published_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
           "engine_version": engine_version}
    tmp = marker_path(home).with_suffix(".json.tmp")
    tmp.write_text(json.dumps(rec, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, marker_path(home))


def refuse_tree_missing(home: Path) -> "EpochError":
    m = read_marker(home)
    return EpochError(
        f"epochs/ is missing on an epoch-managed home (marker {MARKER_NAME}: last published "
        f"{m.get('last_epoch_id', '?')} at {m.get('last_published_at', '?')}) — refusing to fall back to the "
        f"root database. Route 1: restore a backup (scripts/lbrain-restore.sh <backup>); Route 2: "
        f"`lbrain epoch build` to rebuild the epoch from sources; or remove {MARKER_NAME} deliberately to "
        f"declare this a legacy home.")


def resolve_db_path(home: Path, legacy_db_path: Path) -> Path:
    """The database a reader should open right now.

    Epoch layout wins only when CURRENT names an epoch whose db actually exists —
    a dangling pointer falls back loudly rather than silently serving nothing.
    A-592: a MISSING tree on a marked home refuses too, instead of serving the root db.
    """
    shape = home_shape(home)
    if shape == EPOCH_TREE_MISSING:
        raise refuse_tree_missing(home)
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
    # CSO R2: existence is not vetting. build-manifest.json is written ONLY after
    # the post-vacuum integrity re-check, so it is the vetted-marker — without it,
    # publish() would happily point CURRENT at a torn kill-mid-VACUUM partial, and
    # manual rollback is exactly the emergency where an operator would do that.
    if not (epoch_dir(home, epoch_id) / "build-manifest.json").exists():
        raise EpochError(
            f"refusing to publish {epoch_id!r}: no build-manifest.json — this epoch "
            "was never gate-vetted (a torn or hand-built dir must not reach CURRENT)")
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


def _boot_id() -> str:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
    except OSError:
        return ""


def _proc_starttime(pid: int) -> str:
    """Process start time in clock ticks since boot (/proc/<pid>/stat field 22), or ""
    when unreadable. Distinguishes a live holder from an unrelated process that was
    handed the same PID later in the same boot."""
    try:
        stat = Path(f"/proc/{int(pid)}/stat").read_text(encoding="ascii", errors="replace")
        return stat.rpartition(")")[2].split()[19]
    except (OSError, IndexError, ValueError):
        return ""


def _pid_ns() -> str:
    """Inode of this process's PID namespace (/proc/self/ns/pid), or "" when unreadable.
    Two WSL distros share one kernel, so they share boot_id and (by default) hostname,
    but not a PID namespace: a PID recorded in another namespace cannot be probed here."""
    try:
        return str(os.stat("/proc/self/ns/pid").st_ino)
    except OSError:
        return ""


def owner_record() -> dict:
    """Who this process is, for liveness checks by other processes: host, boot, PID
    namespace, PID and process start time. Written into the lock meta and stamped into
    a build's staging dir."""
    return {"host": socket.gethostname(), "boot_id": _boot_id(), "pidns": _pid_ns(),
            "pid": os.getpid(), "starttime": _proc_starttime(os.getpid())}


OWNER_STAMP = ".builder-owner.json"


def stamp_staging_owner(staging: Path) -> None:
    """Record the owning builder inside its staging dir at creation, so the prune sweep
    can judge the owner by boot_id and PID namespace rather than by clock readings."""
    (Path(staging) / OWNER_STAMP).write_text(json.dumps(owner_record()), encoding="utf-8")


def pid_alive_here(pid: int, starttime: str = "") -> bool:
    """True when `pid` names a live process on this kernel (and, when a start time was
    recorded and is readable now, the SAME process). PermissionError = exists = alive."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    except OSError:
        return True  # cannot tell: the safe answer is "alive"
    if starttime:
        now = _proc_starttime(pid)
        if now and now != starttime:
            return False  # the PID was reused by a different process
    return True


def holder_provably_dead(meta: dict) -> bool | None:
    """Liveness of a builder from its owner record (lock meta or staging stamp).
    True = provably dead (same host and a different boot, or same host, boot and PID
    namespace with no such live process); False = provably alive; None = unknowable
    here (foreign host, no boot_id on either side, or a different PID namespace, e.g.
    another WSL distro on the same kernel). A record without `pidns` (written by an
    engine before this field) is probed as if in this namespace."""
    if meta.get("host") != socket.gethostname():
        return None
    theirs, ours = str(meta.get("boot_id") or ""), _boot_id()
    if not theirs or not ours:
        return None
    if theirs != ours:
        return True
    their_ns, our_ns = str(meta.get("pidns") or ""), _pid_ns()
    if their_ns and our_ns and their_ns != our_ns:
        return None
    return not pid_alive_here(meta.get("pid"), str(meta.get("starttime") or ""))


# ---------- the builder lock ----------

class BuilderLock:
    """mkdir spin-lock + heartbeat lease. One builder per brain home.

    mkdir/rmdir is the classic network-fs-safe mutual exclusion (v1.3 #2) — O_EXCL
    is NFS-class unreliable on 9p and stays banned here. Staleness = an old heartbeat
    AND a holder that is not provably alive (holder_provably_dead: boot_id makes it
    reboot-safe, the recorded process start time makes it PID-reuse-safe). Takeover
    renames the stale lock aside rather than deleting into a race.

    Stated assumption, unchanged from the design: one kernel per brain home. This
    lock arbitrates processes on one kernel; hazard #11 says nothing can arbitrate
    two kernels over an async store, and we do not pretend otherwise.
    """

    def __init__(self, home: Path, *, stale_after: float = STALE_AFTER):
        self.dir = epochs_root(home) / LOCK_DIRNAME
        self.meta = self.dir / "builder.json"
        self.stale_after = stale_after
        self.held = False
        # Ownership nonce (CSO R1b): every heartbeat verifies THIS builder still
        # owns the lock before writing; a deposed builder aborts instead of
        # stomping the usurper's lease.
        self.nonce = os.urandom(16).hex()

    def _write_meta(self) -> None:
        # host/pid/boot_id (CSO round-3 rider: pid+boot disambiguates reboots), plus
        # starttime (PID reuse) and pidns (another PID namespace = unknowable).
        body = json.dumps({**owner_record(), "nonce": self.nonce, "heartbeat": time.time()})
        tmp = self.dir / "builder.json.tmp"
        tmp.write_text(body, encoding="utf-8")
        os.replace(str(tmp), str(self.meta))

    def _owns(self) -> bool:
        try:
            m = json.loads(self.meta.read_text(encoding="utf-8"))
            return m.get("nonce") == self.nonce
        except OSError:
            return False
        except Exception:
            return False

    def _meta_age(self) -> float | None:
        try:
            m = json.loads(self.meta.read_text(encoding="utf-8"))
            return time.time() - float(m.get("heartbeat", 0))
        except FileNotFoundError:
            return None
        except Exception:
            return float("inf")  # unreadable meta = treat as stale, not as live

    def _holder_dead(self) -> bool | None:
        try:
            m = json.loads(self.meta.read_text(encoding="utf-8"))
        except Exception:
            return None
        return holder_provably_dead(m) if isinstance(m, dict) else None

    def holder_is_stale(self) -> bool | None:
        """The ONE staleness rule, shared by acquire() and the `--wait-for-builder`
        follower. None = no meta yet (mid-acquire, LIVE). False = live: a fresh
        heartbeat, or an old heartbeat whose holder is provably alive on this boot.
        True = old heartbeat and the holder is dead or its liveness is unknowable."""
        age = self._meta_age()
        if age is None:
            return None
        if age <= self.stale_after:
            return False
        return self._holder_dead() is not False

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
                if self._holder_dead() is False:
                    # Old heartbeat, LIVE holder (a long in-process phase): never seize it.
                    raise BuilderBusy(
                        f"another builder holds the lock (heartbeat {age:.0f}s ago; "
                        "its process is alive on this boot)")
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
        if not self.held:
            return
        if not self._owns():
            self.held = False
            raise LockLost(
                "builder lock was seized by another process (stale takeover) — "
                "aborting this build; the usurper's lease is not ours to touch")
        self._write_meta()

    def release(self) -> None:
        if not self.held:
            return
        try:
            if self._owns():  # R1b: never delete a lock we no longer own
                self.meta.unlink(missing_ok=True)
                self.dir.rmdir()
        except OSError:
            pass
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
    """Epochs with at least one LIVE lease. A lease whose (same-host) pid is dead
    is cleaned here — a crashed reader must not pin an epoch forever. Cross-host
    leases are believed as-is (we cannot probe a foreign pid; over-retention is
    the safe direction). PID-reuse risk also lands on the safe side: worst case
    an epoch is retained longer than needed, never deleted early."""
    root = epochs_root(home) / LEASES_DIRNAME
    if not root.is_dir():
        return set()
    me = socket.gethostname()
    out: set[str] = set()
    for d in root.iterdir():
        if not d.is_dir():
            continue
        live = False
        for f in list(d.iterdir()):
            host, _, pid_s = f.name.rpartition("-")
            if host == me and pid_s.isdigit():
                try:
                    os.kill(int(pid_s), 0)
                except ProcessLookupError:
                    try:
                        f.unlink()
                    except OSError:
                        pass
                    continue
                except PermissionError:
                    pass  # exists, not ours to signal — alive
            live = True
        if live:
            out.add(d.name)
        else:
            try:
                d.rmdir()
            except OSError:
                pass
    return out


def open_store(cfg, *, for_write: bool = False):
    """THE reader/writer entry point for every CLI command and MCP tool call.

    Resolves the effective database (epoch CURRENT, or the legacy db_path when no
    epoch was ever published) and opens it correctly for the home's era:

    - Legacy home → the plain writable Store, byte-for-byte the old behavior.
    - Epoch-managed home, read → IMMUTABLE open (no -wal/-shm, no last-closer
      checkpoint — the DrvFs zombie-reader fix) plus a reader LEASE so prune can
      never delete the epoch underneath; the lease releases on close().
    - Epoch-managed home, for_write=True → REFUSES. Build-validate-swap is the
      only write path; that is the entire design.

    CSO D2 (per-query re-resolve) is satisfied structurally: every CLI command is
    its own process and the MCP server constructs its Store per tool call, so
    each invocation passes through this resolution and picks up a new CURRENT
    without any daemon restart.
    """
    from .config import CONFIG_DIR
    from .store import Store

    home = Path(CONFIG_DIR)
    if for_write:
        # W1/W2 write gates (CSO spec 2026-09-01): dereference the write target
        # BEFORE any store opens — home coherence, then seat identity. Runs for
        # legacy AND epoch homes (an epoch home refuses below anyway, but a
        # misdirected config deserves the more diagnostic refusal first).
        from .write_gates import check_write_target
        check_write_target(cfg, home)
    # A-592: the SAME shape rule as resolve_db_path, for reads AND writes. Without this, `import`/`embed`/
    # `capture` on a marked home whose tree is gone wrote the root database (CSO C8), and the next Route 1
    # restore buried those writes silently. `epoch build` is unaffected: it imports inside a marker-less
    # staging home and publishes by rename, never through this path on the live home.
    if home_shape(home) == EPOCH_TREE_MISSING:
        raise refuse_tree_missing(home)
    eid = current_epoch_id(home)
    if eid is None:
        return Store(cfg.db_path, embedding_dim=cfg.embedding_dim)
    if for_write:
        raise EpochError(
            "this home is epoch-managed (epochs/CURRENT exists) — direct writes are "
            "disabled. Run `lbrain epoch build` instead: build → validate → swap is "
            "the only write path into an epoch-managed brain.")
    # AMBER-1 (CSO inc-3 verdict): resolve→lease is a TOCTOU window, and his P2
    # probe proved DrvFs will NOT save us — rmtree succeeds under an open handle,
    # so a reader entering an epoch just as prune dooms it would zombie-serve a
    # deleted index for its whole session. Two-sided narrowing (his design, both
    # sides): the reader RE-VERIFIES the db exists after lease+open and retries
    # once on miss; prune re-reads leases immediately before each rmtree. Not a
    # lock — sub-ms detection with a deterministic loser, hazard-#11-honest.
    for attempt in range(2):
        db = epoch_db(home, eid)
        if not db.exists():
            raise EpochError(
                f"CURRENT names epoch {eid!r} but {db} does not exist — refusing to "
                "fall back silently; restore a prior epoch or remove the pointer deliberately")
        lease_acquire(home, eid)
        import sqlite3 as _sq
        try:
            store = Store(db, embedding_dim=cfg.embedding_dim, immutable=True)
            doomed = not db.exists()  # DrvFs shape: open succeeds on a deleted file
        except _sq.OperationalError:
            store = None
            doomed = True             # ext4 shape: the open itself fails
        if doomed:  # deterministic loser: release, re-resolve, retry once
            if store is not None:
                store.close()
            lease_release(home, eid)
            new_eid = current_epoch_id(home)
            if attempt == 0 and new_eid and new_eid != eid:
                eid = new_eid
                continue
            raise EpochError(
                f"epoch {eid!r} was pruned during open and no newer CURRENT exists — "
                "retry the operation")
        break
    store.epoch_id = eid
    _orig_close = store.close
    _eid = eid

    def _close_with_lease():
        try:
            _orig_close()
        finally:
            lease_release(home, _eid)

    store.close = _close_with_lease
    return store


def list_epochs(home: Path) -> list[str]:
    """Published-shape epoch dirs, oldest first. Excludes .failed (kept as forensics,
    pruned only by explicit human intent — the .failed-wholesale-* precedent)."""
    root = epochs_root(home)
    if not root.is_dir():
        return []
    out = []
    for d in root.iterdir():
        if (not d.is_dir() or d.name.startswith(".") or d.name.endswith(FAILED_SUFFIX)
                or d.name.endswith(BUILDING_SUFFIX)):
            continue  # CSO R8: a kill -9'd .building dir is not a publishable epoch
        out.append(d.name)
    return sorted(out)


def _dir_bytes(path: Path) -> int:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def prune(home: Path, *, keep: int = 3, max_bytes: int | None = None,
          lock: "BuilderLock | None" = None) -> list[str]:
    """Remove old epochs beyond `keep` (and beyond `max_bytes` total — Grok #5: N=3
    of large vector DBs is the first quota incident; cap BYTES, not just count).

    Never removed: CURRENT, leased epochs (reference-counted — Lucene's
    IndexDeletionPolicy lesson), .failed forensics. A deletion failure (DrvFs
    sharing violation) is logged loudly and SKIPPED — never retried in a loop that
    blocks rebuilds for the lifetime of the longest reader (Grok G2).

    CSO R8: prune runs UNDER the builder lock — pass a held lock, or one is
    acquired (and BuilderBusy refuses the prune while a build is live, instead of
    racing rmtree against a live build's staging). Holding the lock is NOT proof
    that no other builder is live (2026-10-18: a builder seized while alive kept
    building in its own staging), so each `.building` dir is swept only when its
    own builder is provably dead (`_building_owner_dead`); when unsure it is kept.
    """
    own_lock = None
    if lock is None:
        own_lock = BuilderLock(home).acquire()
    try:
        return _prune_locked(home, keep=keep, max_bytes=max_bytes)
    finally:
        if own_lock is not None:
            own_lock.release()


_BUILDING_RE = re.compile(r"^\d{8}T\d{6}Z-(\d+)-\d+" + re.escape(BUILDING_SUFFIX) + r"$")


def _building_owner_dead(d: Path, *, fresh_within: float = STALE_AFTER) -> bool:
    """True only when the builder that owns staging dir `d` is provably dead, judged
    from the owner stamp build() writes at creation (OWNER_STAMP: boot_id, PID
    namespace, PID, start time). Dead = a different boot, or the same boot and PID
    namespace with the PID gone (or reused). Kept (False) when the name does not parse,
    the stamp is missing or unreadable, the stamp's PID disagrees with the name, the
    owner is in another PID namespace, the PID is alive, or anything in the dir was
    written within `fresh_within` seconds. Clock readings such as /proc/stat btime are
    not used: btime drifts forward on WSL2 (CSO, PR #93 review)."""
    m = _BUILDING_RE.match(d.name)
    if not m:
        return False
    try:
        owner = json.loads((d / OWNER_STAMP).read_text(encoding="utf-8"))
    except Exception:
        return False  # no stamp (or a pre-stamp engine's dir): unsure means keep
    if not isinstance(owner, dict) or str(owner.get("pid")) != m.group(1):
        return False
    if owner.get("host") == socket.gethostname() and owner.get("boot_id") \
            and _boot_id() and owner.get("boot_id") != _boot_id():
        return True  # stamped on a previous boot: its builder cannot be running
    try:
        mtimes = [d.stat().st_mtime] + [c.stat().st_mtime for c in d.iterdir()]
    except OSError:
        return False
    if time.time() - max(mtimes) <= fresh_within:
        return False
    return holder_provably_dead(owner) is True


def _prune_locked(home: Path, *, keep: int, max_bytes: int | None) -> list[str]:
    # Sweep .building dirs whose OWN builder is provably dead (kill -9 debris). The lock
    # does not prove that: never sweep a live builder's staging.
    root = epochs_root(home)
    if root.is_dir():
        for d in root.iterdir():
            if d.is_dir() and d.name.endswith(BUILDING_SUFFIX):
                own = _BUILDING_RE.match(d.name)
                if own and int(own.group(1)) == os.getpid():
                    continue  # this builder's own staging: build() removes it in its finally
                if not _building_owner_dead(d):
                    print(f"[lbrain] kept build staging {d.name}: its builder is alive "
                          "or not provably dead")
                    continue
                try:
                    shutil.rmtree(d)
                    print(f"[lbrain] swept orphaned build staging: {d.name}")
                except OSError as err:
                    print(f"[lbrain] WARNING: orphan sweep of {d.name} failed ({err}) — skipped")
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
        # AMBER-1, prune side: re-read leases IMMEDIATELY before each rmtree — a
        # reader's lease may have landed since the sweep started, and on DrvFs
        # the filesystem will happily delete under an open handle (P2, measured).
        if e in leased_epochs(home):
            print(f"[lbrain] prune: epoch {e} gained a reader lease mid-prune — spared")
            continue
        try:
            shutil.rmtree(epoch_dir(home, e))
            removed.append(e)
        except OSError as err:
            print(f"[lbrain] WARNING: prune of epoch {e} failed ({err}) — skipped, "
                  "not retried; NOTE: on DrvFs deletion can SUCCEED under an open "
                  "handle, so this fallback is not a backstop — the lease is the wall")
    return removed
