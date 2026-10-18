"""A live builder is never seized, and its staging is never swept (CCO seat, 2026-10-18).

Incident on the CCO seat home: builder 3359336 spent about 7 minutes in its in-process
gate + VACUUM INTO phase with no heartbeat. At 61 s a second builder, 3373886, judged
the lock stale by heartbeat AGE alone and took it over while 3359336 was alive.
3359336 then published and pruned under the lock object it no longer owned, and the
prune swept 3373886's LIVE `.building` staging as "orphaned". The second build reached
the gate with zero chunks and was refused.

Invariants, regression tests for each:
  (a) a lock whose holder is alive on this boot is not stale, however old its heartbeat;
  (b) prune never sweeps a `.building` dir whose own builder is alive;
  (c) a builder that lost its lock aborts before publish() and prune() (CSO B1 on #93);
  (d) a holder in another PID namespace (another WSL distro, same kernel) is unknowable,
      not "provably dead" (CSO N1);
  (e) the sweep judges a staging dir by the boot_id stamped into it, never by the
      drifting /proc/stat btime, and keeps a dir with no stamp (CSO N2).

The owner records below are built from /proc directly, not from engine helpers, so a
negative control against an engine without the fix fails on behaviour, not on a
missing attribute.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from lbrain import epoch, epoch_build
from test_epoch_build import _cfg, _mk_home, _seed_legacy

STAMP = ".builder-owner.json"


def _starttime(pid: int) -> str:
    stat = Path(f"/proc/{pid}/stat").read_text(encoding="ascii", errors="replace")
    return stat.rpartition(")")[2].split()[19]


def _owner(pid: int, **over) -> dict:
    rec = {"host": socket.gethostname(),
           "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
           "pidns": str(os.stat("/proc/self/ns/pid").st_ino),
           "pid": pid, "starttime": ""}
    rec.update(over)
    return rec


@pytest.fixture
def live_child():
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    try:
        yield p.pid
    finally:
        p.kill()
        p.wait()


def _dead_pid() -> int:
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


def _staging(home, pid: int, *, age: float, stamp: dict | None = None, name_ts="20261018T115926Z"):
    d = epoch.epochs_root(home) / (f"{name_ts}-{pid}-0001" + epoch.BUILDING_SUFFIX)
    d.mkdir(parents=True)
    (d / "brain.db").write_bytes(b"a builder is writing this")
    if stamp is not None:
        (d / STAMP).write_text(json.dumps(stamp), encoding="utf-8")
    t = time.time() - age
    for f in list(d.iterdir()) + [d]:
        os.utime(f, (t, t))
    return d


def _published(home):
    d = epoch.epoch_dir(home, "E1")
    d.mkdir(parents=True)
    (d / "brain.db").write_bytes(b"x")
    (d / "build-manifest.json").write_text("{}", encoding="utf-8")
    epoch.publish(home, "E1")


def _set_meta(lock, **fields):
    m = json.loads(lock.meta.read_text(encoding="utf-8"))
    m.update(fields)
    lock.meta.write_text(json.dumps(m), encoding="utf-8")


# ---------- (a) the lock ----------

def test_a_live_holder_in_a_long_phase_is_not_treated_as_stale(tmp_path):
    a = epoch.BuilderLock(tmp_path, stale_after=0.05).acquire()
    time.sleep(0.2)  # the heartbeat is 4x stale_after old; the holder (this process) is alive
    b = epoch.BuilderLock(tmp_path, stale_after=0.05)
    with pytest.raises(epoch.BuilderBusy, match="alive on this boot"):
        b.acquire()
    assert not any(".stale-" in p.name for p in epoch.epochs_root(tmp_path).iterdir()), \
        "the live holder's lock was renamed aside"
    a.heartbeat()  # still the owner: no LockLost
    assert b.holder_is_stale() is False
    a.release()


def test_the_wait_for_builder_follower_uses_the_same_rule(tmp_path):
    a = epoch.BuilderLock(tmp_path).acquire()
    _set_meta(a, heartbeat=time.time() - 3600)
    assert epoch_build._lock_holder(tmp_path)["stale"] is False
    _set_meta(a, boot_id="a-previous-boot")
    assert epoch_build._lock_holder(tmp_path)["stale"] is True
    a.release()


def test_a_live_holder_in_another_process_is_not_seized(tmp_path, live_child):
    a = epoch.BuilderLock(tmp_path, stale_after=0.05).acquire()
    _set_meta(a, pid=live_child, starttime=_starttime(live_child), heartbeat=time.time() - 3600)
    with pytest.raises(epoch.BuilderBusy, match="alive on this boot"):
        epoch.BuilderLock(tmp_path, stale_after=0.05).acquire()


def test_a_dead_holder_or_a_previous_boot_is_still_taken_over(tmp_path):
    for i, fields in enumerate(({"pid": _dead_pid(), "starttime": ""}, {"boot_id": "a-previous-boot"})):
        home = tmp_path / f"h{i}"  # one home each: the stale-aside name is per second + pid
        a = epoch.BuilderLock(home, stale_after=0.05).acquire()
        _set_meta(a, heartbeat=time.time() - 3600, **fields)
        b = epoch.BuilderLock(home, stale_after=0.05).acquire()
        assert b.held
        b.release()


def test_a_reused_pid_is_not_mistaken_for_the_holder(tmp_path, live_child):
    a = epoch.BuilderLock(tmp_path, stale_after=0.05).acquire()
    _set_meta(a, pid=live_child, starttime="1", heartbeat=time.time() - 3600)  # wrong start time
    b = epoch.BuilderLock(tmp_path, stale_after=0.05).acquire()
    assert b.held
    b.release()


# ---------- (b) the sweep ----------

def test_a_live_second_builders_staging_survives_the_first_builders_publish_prune(tmp_path, live_child):
    _published(tmp_path)
    theirs = _staging(tmp_path, live_child, age=600,  # quiet for 10 min, its builder alive
                      stamp=_owner(live_child, starttime=_starttime(live_child)))
    lock = epoch.BuilderLock(tmp_path).acquire()      # the first builder, holding the lock
    try:
        epoch.prune(tmp_path, keep=3, lock=lock)
    finally:
        lock.release()
    assert theirs.exists(), "prune swept a LIVE builder's staging"


def test_sweep_keeps_when_unsure_and_removes_only_the_provably_dead(tmp_path):
    _published(tmp_path)
    p = _dead_pid()
    dead_old = _staging(tmp_path, p, age=600, stamp=_owner(p))
    p = _dead_pid()
    dead_fresh = _staging(tmp_path, p, age=0, stamp=_owner(p), name_ts="20261018T120000Z")
    p = _dead_pid()
    dead_unstamped = _staging(tmp_path, p, age=600, name_ts="20261018T120100Z")
    unparseable = epoch.epochs_root(tmp_path) / ("E0" + epoch.BUILDING_SUFFIX)
    unparseable.mkdir()
    epoch.prune(tmp_path, keep=3)
    assert not dead_old.exists()
    assert dead_fresh.exists(), "swept a dir written seconds ago"
    assert dead_unstamped.exists(), "swept a dir with no owner stamp (unsure means keep)"
    assert unparseable.exists(), "swept a dir whose name does not parse"


# ---------- (c) the pre-publish ownership check (CSO B1) ----------

def test_a_deposed_builder_aborts_before_publish_and_prune(tmp_path, monkeypatch):
    home, srcs = _mk_home(tmp_path)
    _seed_legacy(home)
    assert epoch_build.build(home, _cfg(home, srcs))["published"]
    before = epoch.current_epoch_id(home)
    calls = {"publish": 0, "prune": 0, "stamped": 0}
    real_validate, real_publish, real_prune = epoch_build.validate_candidate, epoch_build.publish, epoch.prune

    def usurp_after_gate(db_path, *a, **k):
        out = real_validate(db_path, *a, **k)
        calls["stamped"] += (Path(db_path).parent / STAMP).exists()
        meta = epoch.epochs_root(home) / epoch.LOCK_DIRNAME / "builder.json"
        m = json.loads(meta.read_text(encoding="utf-8"))
        m["nonce"] = "builder-B"  # another builder now owns the lock
        meta.write_text(json.dumps(m), encoding="utf-8")
        return out

    def counted(name, fn):
        def wrapper(*a, **k):
            calls[name] += 1
            return fn(*a, **k)
        return wrapper

    monkeypatch.setattr(epoch_build, "validate_candidate", usurp_after_gate)
    monkeypatch.setattr(epoch_build, "publish", counted("publish", real_publish))
    monkeypatch.setattr(epoch, "prune", counted("prune", real_prune))
    with pytest.raises(epoch.LockLost):
        epoch_build.build(home, _cfg(home, srcs))
    assert calls["publish"] == 0 and calls["prune"] == 0, calls
    assert epoch.current_epoch_id(home) == before
    assert calls["stamped"] == 1, "build() did not stamp its staging dir with its owner"


# ---------- (d) another PID namespace is unknowable (CSO N1) ----------

def test_a_holder_in_another_pid_namespace_is_unknowable_not_dead(tmp_path):
    other_ns = _owner(_dead_pid(), pidns="4026531836-other-distro")
    assert epoch.holder_provably_dead(other_ns) is None
    assert epoch.holder_provably_dead(_owner(_dead_pid())) is True  # same namespace: provable
    # the lock falls back to the age rule: fresh heartbeat blocks, old heartbeat is taken over
    a = epoch.BuilderLock(tmp_path, stale_after=5.0).acquire()
    _set_meta(a, **other_ns, heartbeat=time.time())
    with pytest.raises(epoch.BuilderBusy, match=r"heartbeat \d+s ago\)$"):
        epoch.BuilderLock(tmp_path, stale_after=5.0).acquire()
    a.held = False


def test_the_sweep_keeps_a_staging_dir_owned_from_another_pid_namespace(tmp_path):
    _published(tmp_path)
    p = _dead_pid()  # gone HERE, but it was another distro's PID: we cannot tell
    theirs = _staging(tmp_path, p, age=600, stamp=_owner(p, pidns="4026531836-other-distro"))
    epoch.prune(tmp_path, keep=3)
    assert theirs.exists(), "swept a staging dir whose builder lives in another PID namespace"


# ---------- (e) boot stamp, not btime (CSO N2) ----------

def test_the_sweep_ignores_btime_and_trusts_the_boot_stamp(tmp_path, live_child):
    _published(tmp_path)
    # A live builder's staging last written long ago, before any plausible (or drifted)
    # btime: still this boot by its stamp, so it must survive.
    theirs = _staging(tmp_path, live_child, age=30 * 86400,
                      stamp=_owner(live_child, starttime=_starttime(live_child)))
    p = _dead_pid()
    prior_boot = _staging(tmp_path, p, age=0, stamp=_owner(p, boot_id="a-previous-boot"),
                          name_ts="20261018T120200Z")
    epoch.prune(tmp_path, keep=3)
    assert theirs.exists(), "swept a live builder's staging on a clock reading"
    assert not prior_boot.exists(), "kept a dir stamped on a previous boot"
