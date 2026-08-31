"""Atomic epochs — protocol layer (design v1.4). Pointer, lock, leases, prune.

Each test is one RED target from the combined gauntlet (panel + CSO + v1.3/v1.4);
the build/validate orchestration has its own suite.
"""
from __future__ import annotations

import errno
import os
import time

import pytest

from lbrain import epoch


def _mk_epoch(home, eid, size=0):
    d = epoch.epoch_dir(home, eid)
    d.mkdir(parents=True)
    (d / "brain.db").write_bytes(b"x" * size)
    return d


# ---------- CURRENT pointer ----------

def test_no_pointer_means_legacy_layout(tmp_path):
    assert epoch.current_epoch_id(tmp_path) is None
    legacy = tmp_path / "brain.db"
    assert epoch.resolve_db_path(tmp_path, legacy) == legacy


def test_publish_and_resolve_roundtrip(tmp_path):
    _mk_epoch(tmp_path, "E1", size=10)
    caveat = epoch.publish(tmp_path, "E1")
    assert caveat == ""  # POSIX tmp: dir fsync must succeed
    assert epoch.current_epoch_id(tmp_path) == "E1"
    assert epoch.resolve_db_path(tmp_path, tmp_path / "brain.db") == epoch.epoch_db(tmp_path, "E1")


def test_publish_refuses_a_missing_epoch(tmp_path):
    with pytest.raises(epoch.EpochError, match="refusing to publish"):
        epoch.publish(tmp_path, "GHOST")


def test_dangling_pointer_refuses_loudly_not_silently(tmp_path):
    _mk_epoch(tmp_path, "E1", size=1)
    epoch.publish(tmp_path, "E1")
    os.remove(epoch.epoch_db(tmp_path, "E1"))
    with pytest.raises(epoch.EpochError, match="does not exist"):
        epoch.resolve_db_path(tmp_path, tmp_path / "brain.db")


def test_pointer_swap_is_atomic_for_a_reader_loop(tmp_path):
    _mk_epoch(tmp_path, "E1", size=1)
    _mk_epoch(tmp_path, "E2", size=1)
    epoch.publish(tmp_path, "E1")
    seen = set()
    for _ in range(50):
        seen.add(epoch.current_epoch_id(tmp_path))
    epoch.publish(tmp_path, "E2")
    for _ in range(50):
        seen.add(epoch.current_epoch_id(tmp_path))
    assert seen == {"E1", "E2"}  # never None, never a torn read


def test_drvfs_dir_fsync_failure_is_tolerated_with_caveat(tmp_path, monkeypatch):
    """v1.3 #1: EINVAL on directory fsync (the 9p bridge) must not abort the
    publish — but it must be NAMED, not swallowed."""
    _mk_epoch(tmp_path, "E1", size=1)
    real_fsync = os.fsync
    def fake_fsync(fd):
        import stat
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError(errno.EINVAL, "Invalid argument")
        return real_fsync(fd)
    monkeypatch.setattr(os, "fsync", fake_fsync)
    caveat = epoch.publish(tmp_path, "E1")
    assert "NOT crash-durable" in caveat
    assert epoch.current_epoch_id(tmp_path) == "E1"


def test_file_fsync_failure_aborts_the_publish(tmp_path, monkeypatch):
    """The Ts'o rule: fsync-and-CHECK. A failed data fsync must abort, never
    proceed to the rename."""
    _mk_epoch(tmp_path, "E1", size=1)
    def fake_fsync(fd):
        raise OSError(errno.EIO, "I/O error")
    monkeypatch.setattr(os, "fsync", fake_fsync)
    with pytest.raises(OSError):
        epoch.publish(tmp_path, "E1")
    assert epoch.current_epoch_id(tmp_path) is None  # pointer never landed


# ---------- builder lock ----------

def test_two_builders_exactly_one_wins(tmp_path):
    a = epoch.BuilderLock(tmp_path).acquire()
    with pytest.raises(epoch.BuilderBusy):
        epoch.BuilderLock(tmp_path).acquire()
    a.release()
    b = epoch.BuilderLock(tmp_path).acquire()  # released lock is acquirable
    b.release()


def test_stale_lock_is_taken_over_by_heartbeat_age_not_pid(tmp_path):
    a = epoch.BuilderLock(tmp_path, stale_after=0.05).acquire()
    time.sleep(0.1)  # heartbeat goes stale; the holding PID is alive — irrelevant
    b = epoch.BuilderLock(tmp_path, stale_after=0.05).acquire()
    assert b.held
    b.release()


def test_fresh_heartbeat_blocks_takeover(tmp_path):
    a = epoch.BuilderLock(tmp_path, stale_after=5.0).acquire()
    a.heartbeat()
    with pytest.raises(epoch.BuilderBusy, match="heartbeat"):
        epoch.BuilderLock(tmp_path, stale_after=5.0).acquire()
    a.release()


def test_unreadable_lock_meta_counts_as_stale_not_live(tmp_path):
    a = epoch.BuilderLock(tmp_path).acquire()
    a.meta.write_text("{corrupt", encoding="utf-8")
    b = epoch.BuilderLock(tmp_path, stale_after=5.0).acquire()
    assert b.held
    b.release()


# ---------- leases + prune ----------

def test_prune_keeps_current_leased_and_newest(tmp_path):
    for i in range(1, 7):
        _mk_epoch(tmp_path, f"E{i}", size=1)
    epoch.publish(tmp_path, "E6")
    epoch.lease_acquire(tmp_path, "E2")
    removed = epoch.prune(tmp_path, keep=2)
    remaining = epoch.list_epochs(tmp_path)
    assert "E6" in remaining          # CURRENT never pruned
    assert "E2" in remaining          # leased never pruned (reference-counted)
    assert "E4" in remaining and "E5" in remaining  # newest 2 candidates kept
    assert set(removed) == {"E1", "E3"}


def test_prune_byte_cap_removes_beyond_count_keep(tmp_path):
    for i in range(1, 5):
        _mk_epoch(tmp_path, f"E{i}", size=100)
    epoch.publish(tmp_path, "E4")
    removed = epoch.prune(tmp_path, keep=3, max_bytes=250)
    assert removed  # count-keep alone would keep all three priors; bytes forced more


def test_failed_epochs_are_never_pruned(tmp_path):
    _mk_epoch(tmp_path, "E1", size=1)
    epoch.publish(tmp_path, "E1")
    f = epoch.failed_dir(tmp_path, "E0")
    f.mkdir(parents=True)
    (f / "brain.db").write_bytes(b"forensics")
    epoch.prune(tmp_path, keep=0)
    assert f.exists()  # forensics survive any prune


def test_lease_release_cleans_up(tmp_path):
    _mk_epoch(tmp_path, "E1", size=1)
    epoch.lease_acquire(tmp_path, "E1")
    assert epoch.leased_epochs(tmp_path) == {"E1"}
    epoch.lease_release(tmp_path, "E1")
    assert epoch.leased_epochs(tmp_path) == set()
