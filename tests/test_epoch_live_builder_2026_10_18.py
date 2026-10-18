"""A live builder is never seized, and its staging is never swept (CCO seat, 2026-10-18).

Incident on the CCO seat home: builder 3359336 spent about 7 minutes in its in-process
gate + VACUUM INTO phase with no heartbeat. At 61 s a second builder, 3373886, judged
the lock stale by heartbeat AGE alone and took it over while 3359336 was alive.
3359336 then published and pruned under the lock object it no longer owned, and the
prune swept 3373886's LIVE `.building` staging as "orphaned". The second build reached
the gate with zero chunks and was refused.

Two invariants, regression tests for each:
  (a) a lock whose holder is alive on this boot is not stale, however old its heartbeat;
  (b) prune never sweeps a `.building` dir whose own builder is alive.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time

import pytest

from lbrain import epoch, epoch_build


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


def _staging(home, pid: int, *, age: float):
    d = epoch.epochs_root(home) / (f"20261018T115926Z-{pid}-0001" + epoch.BUILDING_SUFFIX)
    d.mkdir(parents=True)
    (d / "brain.db").write_bytes(b"a builder is writing this")
    t = time.time() - age
    os.utime(d / "brain.db", (t, t))
    os.utime(d, (t, t))
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
    _set_meta(a, pid=live_child, starttime=epoch._proc_starttime(live_child),
              heartbeat=time.time() - 3600)
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
    theirs = _staging(tmp_path, live_child, age=600)  # quiet for 10 min, its builder alive
    lock = epoch.BuilderLock(tmp_path).acquire()      # the first builder, holding the lock
    try:
        epoch.prune(tmp_path, keep=3, lock=lock)
    finally:
        lock.release()
    assert theirs.exists(), "prune swept a LIVE builder's staging"


def test_sweep_keeps_when_unsure_and_removes_only_the_provably_dead(tmp_path):
    _published(tmp_path)
    dead_old = _staging(tmp_path, _dead_pid(), age=600)
    dead_fresh = epoch.epochs_root(tmp_path) / (f"20261018T120000Z-{_dead_pid()}-0001" + epoch.BUILDING_SUFFIX)
    dead_fresh.mkdir()
    (dead_fresh / "brain.db").write_bytes(b"written seconds ago")
    unparseable = epoch.epochs_root(tmp_path) / ("E0" + epoch.BUILDING_SUFFIX)
    unparseable.mkdir()
    epoch.prune(tmp_path, keep=3)
    assert not dead_old.exists()
    assert dead_fresh.exists() and unparseable.exists()
