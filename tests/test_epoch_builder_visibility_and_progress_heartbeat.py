"""Builder visibility and progress-driven heartbeats: the three CSO follow-ups on PR #93.

1. `lbrain whoami` names the builder lock holder and its liveness, from the SAME rules the
   lock uses (BuilderLock.holder_is_stale, holder_provably_dead via holder_liveness):
   "builder pid N on HOST: live, last heartbeat M min ago", "stale: holder dead", or
   "unknowable: other pid namespace".
2. whoami and the `--wait-for-builder` wait line COUNT `.building` dirs with no owner stamp
   that are older than a day (the sweep keeps them; an operator removes). Count only: the
   count never deletes.
3. VACUUM INTO and integrity_check keep the lock heartbeat alive through sqlite's progress
   handler (progress-driven, so a statement that is not executing does not beat; a
   heartbeat thread was ruled out). Throttled; never raises into sqlite; aborts the
   statement only on LockLost.
"""
from __future__ import annotations

import json
import os
import socket
import sqlite3
import sys
import threading
import time
from pathlib import Path

import pytest

from lbrain import epoch, epoch_build
from test_epoch_build import _cfg, _mk_home, _seed_legacy
from test_epoch_live_builder_2026_10_18 import (
    _dead_pid, _owner, _published, _set_meta, _staging, _starttime, live_child)  # noqa: F401

HOST = socket.gethostname()


# ---------- 1. the whoami builder line ----------

def test_whoami_builder_line_live_dead_unknowable(tmp_path):
    assert epoch.builder_status(tmp_path) is None  # no lock held: no builder line
    lock = epoch.BuilderLock(tmp_path).acquire()
    try:
        _set_meta(lock, heartbeat=time.time() - 7 * 60)  # alive, in a long phase
        st = epoch.builder_status(tmp_path)
        assert st["line"] == f"builder pid {os.getpid()} on {HOST}: live, last heartbeat 7 min ago"
        assert st["state"] == "live" and st["stale"] is False

        _set_meta(lock, pid=_dead_pid(), starttime="")
        assert epoch.builder_status(tmp_path)["state"] == "stale: holder dead"
        _set_meta(lock, pid=os.getpid(), boot_id="a-previous-boot")
        st = epoch.builder_status(tmp_path)
        assert st["state"] == "stale: holder dead" and st["stale"] is True

        _set_meta(lock, boot_id=_owner(0)["boot_id"], pidns="4026531836-other-distro",
                  heartbeat=time.time() - 5)
        st = epoch.builder_status(tmp_path)
        assert st["state"] == "unknowable: other pid namespace", st
        assert st["line"].endswith("last heartbeat 5s ago"), st["line"]
        _set_meta(lock, heartbeat=time.time() - 600)  # unknowable AND old: takeover allowed
        assert "may take the lock over" in epoch.builder_status(tmp_path)["line"]
    finally:
        lock.held = False


def test_whoami_builder_line_reuses_the_lock_rules(tmp_path, monkeypatch):
    """No restated rule: the line's verdicts come from BuilderLock.holder_is_stale and
    holder_liveness. Patching either one moves the line."""
    lock = epoch.BuilderLock(tmp_path).acquire()
    try:
        monkeypatch.setattr(epoch, "holder_liveness", lambda meta: (None, "probe-reason"))
        monkeypatch.setattr(epoch.BuilderLock, "holder_is_stale", lambda self: "probe-stale")
        st = epoch.builder_status(tmp_path)
        assert st["state"] == "unknowable: probe-reason"
        assert st["stale"] == "probe-stale"
    finally:
        monkeypatch.undo()
        lock.release()


def test_holder_provably_dead_is_unchanged_by_the_refactor(tmp_path, live_child):
    assert epoch.holder_provably_dead(_owner(_dead_pid())) is True
    assert epoch.holder_provably_dead(
        _owner(live_child, starttime=_starttime(live_child))) is False
    assert epoch.holder_provably_dead(_owner(live_child, pidns="other")) is None
    assert epoch.holder_provably_dead(_owner(live_child, host="elsewhere")) is None
    assert epoch.holder_provably_dead(_owner(live_child, boot_id="old-boot")) is True


def _whoami_out(monkeypatch, home: Path) -> str:
    from click.testing import CliRunner
    from lbrain import cli as cli_mod
    from lbrain.config import Config
    cfg = Config(embedding_provider="local", db_path=home / "brain.db")
    monkeypatch.setattr(cli_mod.Config, "load", classmethod(lambda cls: cfg))
    res = CliRunner().invoke(cli_mod.main, ["whoami"])
    assert res.exit_code == 0, res.output
    return res.output


def test_whoami_renders_the_builder_line_and_the_debris_count(tmp_path, monkeypatch):
    _published(tmp_path)  # an epoch-managed home
    out = _whoami_out(monkeypatch, tmp_path)
    assert "builder" in out and "no builder lock held" in out
    assert "unstamped staging" not in out

    _staging(tmp_path, _dead_pid(), age=2 * 86400)  # no stamp, two days old
    lock = epoch.BuilderLock(tmp_path).acquire()
    try:
        _set_meta(lock, heartbeat=time.time() - 3 * 60)
        out = _whoami_out(monkeypatch, tmp_path)
    finally:
        lock.release()
    assert f"builder pid {os.getpid()} on {HOST}: live, last heartbeat 3 min ago" in out, out
    assert "1 unstamped staging dir older than a day (not swept; operator removes)" in out, out


# ---------- 2. the debris count ----------

def test_debris_count_counts_only_unstamped_and_old_and_never_deletes(tmp_path):
    _published(tmp_path)
    p = _dead_pid()
    counted = _staging(tmp_path, p, age=2 * 86400)
    young = _staging(tmp_path, _dead_pid(), age=3600, name_ts="20261018T120000Z")
    stamped = _staging(tmp_path, p, age=2 * 86400, stamp=_owner(p), name_ts="20261018T120100Z")
    unparseable_old = epoch.epochs_root(tmp_path) / ("E0" + epoch.BUILDING_SUFFIX)
    unparseable_old.mkdir()
    t = time.time() - 3 * 86400
    os.utime(unparseable_old, (t, t))
    failed_old = epoch.epochs_root(tmp_path) / ("E9" + epoch.FAILED_SUFFIX)
    failed_old.mkdir()
    os.utime(failed_old, (t, t))
    assert epoch.unstamped_staging_count(tmp_path) == 2  # `counted` and `unparseable_old`
    for d in (counted, young, stamped, unparseable_old, failed_old):
        assert d.exists(), f"the count deleted {d.name}"
    assert epoch.debris_line(2) == ("2 unstamped staging dirs older than a day "
                                    "(not swept; operator removes)")


def test_debris_count_rides_the_wait_for_builder_line(tmp_path):
    home, srcs = _mk_home(tmp_path)
    _published(home)
    _staging(home, _dead_pid(), age=2 * 86400)
    said: list[str] = []
    lock = epoch.BuilderLock(home).acquire()  # a live holder with a fresh heartbeat
    try:
        with pytest.raises(epoch.BuilderBusy):
            epoch_build.build_single_flight(home, _cfg(home, srcs), wait=0.3, poll=0.05,
                                            invoked_ns=time.time_ns(), echo=said.append)
    finally:
        lock.release()
    notes = [s for s in said if "unstamped staging" in s]
    assert notes == ["  note: 1 unstamped staging dir older than a day "
                     "(not swept; operator removes)"], said


# ---------- 3. progress-driven heartbeats ----------

def _db(path: Path, rows: int = 3000) -> sqlite3.Connection:
    con = sqlite3.connect(str(path))
    con.execute("CREATE TABLE t (x TEXT)")
    con.executemany("INSERT INTO t VALUES (?)", [("x" * 200,)] * rows)
    con.commit()
    return con


class _SpyLock:
    def __init__(self, exc: BaseException | None = None):
        self.beats, self.exc = 0, exc

    def heartbeat(self):
        if self.exc is not None:
            raise self.exc
        self.beats += 1


def test_build_beats_the_lock_from_sqlite_progress_during_vacuum_and_integrity_check(
        tmp_path, monkeypatch):
    """End to end: build() installs the handler on both long phases, and the lock beats
    that land come from inside sqlite (the handler), not from a stage boundary."""
    home, srcs = _mk_home(tmp_path)
    _seed_legacy(home)
    monkeypatch.setattr(epoch_build, "PROGRESS_BEAT_EVERY", 0.0)
    monkeypatch.setattr(epoch_build, "PROGRESS_OPCODES", 100)
    from_handler = {"n": 0}
    real = epoch.BuilderLock.heartbeat

    def spy(self):
        if sys._getframe(1).f_code.co_name == "_beat":
            from_handler["n"] += 1
        return real(self)

    monkeypatch.setattr(epoch.BuilderLock, "heartbeat", spy)
    threads_before = threading.active_count()
    report = epoch_build.build(home, _cfg(home, srcs))
    assert report["published"]
    beats = report.get("progress_heartbeats", {})
    assert beats.get("vacuum_into", 0) > 0, beats
    assert beats.get("integrity_check", 0) > 0, beats
    assert from_handler["n"] == beats["vacuum_into"] + beats["integrity_check"]
    assert threading.active_count() == threads_before, "a heartbeat thread was left running"


def test_progress_heartbeat_is_throttled(tmp_path):
    con = _db(tmp_path / "a.db")
    ticks = {"n": 0}

    def clock():  # one fake second per handler call
        ticks["n"] += 1
        return float(ticks["n"])

    lock, report = _SpyLock(), {}
    with epoch_build._heartbeat_while(con, lock, "vacuum_into", report,
                                      every=5.0, opcodes=50, clock=clock):
        con.execute("VACUUM INTO ?", (str(tmp_path / "b.db"),))
    calls = ticks["n"]
    assert calls >= 20, f"too few progress calls to measure a throttle: {calls}"
    assert 1 <= lock.beats <= calls // 5 + 1, (lock.beats, calls)
    assert report["progress_heartbeats"]["vacuum_into"] == lock.beats


def test_a_statement_that_is_not_executing_does_not_beat(tmp_path):
    """Progress-driven, not time-driven: a statement blocked for a second (here on another
    connection's write lock, standing in for a hang) beats no more than the few opcodes it
    ran before blocking. A heartbeat thread would have beaten throughout."""
    path = tmp_path / "a.db"
    _db(path, rows=10).close()
    holder = sqlite3.connect(str(path), isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    try:
        con = sqlite3.connect(str(path), timeout=1.0)
        lock, report = _SpyLock(), {}
        t0 = time.monotonic()
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            with epoch_build._heartbeat_while(con, lock, "vacuum_into", report,
                                              every=0.0, opcodes=1):
                con.execute("INSERT INTO t VALUES ('y')")
                con.commit()
        assert time.monotonic() - t0 >= 0.9
        assert lock.beats < 50, f"{lock.beats} beats while the statement was blocked"
    finally:
        holder.execute("ROLLBACK")
        holder.close()


def test_losing_the_lock_aborts_the_vacuum(tmp_path):
    home = tmp_path / "home"
    con = _db(tmp_path / "a.db")
    lock = epoch.BuilderLock(home).acquire()
    _set_meta(lock, nonce="builder-B")  # another builder now owns the lock
    with pytest.raises(epoch.LockLost):
        with epoch_build._heartbeat_while(con, lock, "vacuum_into", {}, every=0.0, opcodes=50):
            con.execute("VACUUM INTO ?", (str(tmp_path / "b.db"),))
    with pytest.raises(sqlite3.DatabaseError):
        sqlite3.connect(str(tmp_path / "b.db")).execute("SELECT COUNT(*) FROM t").fetchone()


def test_a_deposed_builder_stops_mid_vacuum_and_leaves_no_half_epoch(tmp_path, monkeypatch):
    home, srcs = _mk_home(tmp_path)
    _seed_legacy(home)
    assert epoch_build.build(home, _cfg(home, srcs))["published"]
    before, epochs_before = epoch.current_epoch_id(home), epoch.list_epochs(home)
    monkeypatch.setattr(epoch_build, "PROGRESS_BEAT_EVERY", 0.0)
    monkeypatch.setattr(epoch_build, "PROGRESS_OPCODES", 100)
    real_validate = epoch_build.validate_candidate

    def usurp_after_gate(*a, **k):
        out = real_validate(*a, **k)
        meta = epoch.epochs_root(home) / epoch.LOCK_DIRNAME / "builder.json"
        m = json.loads(meta.read_text(encoding="utf-8"))
        m["nonce"] = "builder-B"
        meta.write_text(json.dumps(m), encoding="utf-8")
        return out

    monkeypatch.setattr(epoch_build, "validate_candidate", usurp_after_gate)
    with pytest.raises(epoch.LockLost):
        epoch_build.build(home, _cfg(home, srcs))
    assert epoch.current_epoch_id(home) == before
    assert epoch.list_epochs(home) == epochs_before, \
        "the deposed builder ran VACUUM INTO to completion (or left its half-written epoch)"


def test_the_handler_never_raises_into_sqlite(tmp_path):
    con = _db(tmp_path / "a.db")
    report = {}
    with epoch_build._heartbeat_while(con, _SpyLock(exc=OSError("9p hiccup")), "vacuum_into",
                                      report, every=0.0, opcodes=50):
        con.execute("VACUUM INTO ?", (str(tmp_path / "b.db"),))
    assert sqlite3.connect(str(tmp_path / "b.db")).execute(
        "SELECT COUNT(*) FROM t").fetchone()[0] == 3000
    assert report["progress_heartbeats"]["vacuum_into_errors"] > 0
    # and the handler is removed afterwards
    lock = _SpyLock()
    with epoch_build._heartbeat_while(con, lock, "x", {}, every=0.0, opcodes=50):
        pass
    con.execute("VACUUM INTO ?", (str(tmp_path / "c.db"),))
    assert lock.beats == 0
