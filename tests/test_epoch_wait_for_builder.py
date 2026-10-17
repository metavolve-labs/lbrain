"""`lbrain epoch build --wait-for-builder SECONDS`: single-flight wake refresh.

Without the flag, a build that finds a LIVE builder holding the lock prints
``✗ another builder holds the lock (heartbeat Ns ago)`` and exits 3, so a seat's launcher
fails closed even though the other builder is about to publish exactly what it needs.

With the flag, the follower waits (bounded) for the live builder, then:
  * the builder published an epoch whose scan began AFTER the follower was invoked:
    exit 0 on that epoch, no second build;
  * the builder failed, or its scan began BEFORE the follower was invoked: build;
  * the wait timed out: the existing error, unchanged.
A stale lock is taken over exactly as without the flag. Without the flag nothing changes.

The leader runs in-process (a thread) so the test can hold it at a precise point; the
follower is the real CLI in a subprocess.
"""
from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from lbrain import epoch, epoch_build

from _coldembed import seed_brain

DIM = 384
MODEL = "BAAI/bge-small-en-v1.5"


def _mk_home(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    src = tmp_path / "src"
    src.mkdir()
    (src / "alpha.md").write_text("# Alpha\n\nThe quicksilver archive holds record one.\n")
    (src / "beta.md").write_text("# Beta\n\nAnother distinctive passage, number two.\n")
    (home / "config.toml").write_text(
        'embedding_provider = "local"\n'
        f'embedding_model = "{MODEL}"\n'
        f"embedding_dim = {DIM}\n"
        f'db_path = "{home / "brain.db"}"\n'
        f'sources = ["{src}"]\n')
    (home / "identity.json").write_text('{"who": "test-seat"}\n')
    seed_brain(home, DIM)
    cfg = SimpleNamespace(sources=[str(src)], embedding_dim=DIM, db_path=home / "brain.db")
    return home, cfg


def _published(home: Path) -> list[str]:
    root = epoch.epochs_root(home)
    return sorted(d.name for d in root.iterdir()
                  if d.is_dir() and (d / "brain.db").exists() and (d / "build-manifest.json").exists())


class _Follower:
    """The real CLI, its stdout collected line by line (unbuffered) as it runs."""

    def __init__(self, home: Path, *args: str):
        env = dict(os.environ, LBRAIN_HOME=str(home), PYTHONUNBUFFERED="1")
        self.p = subprocess.Popen(["lbrain", "epoch", "build", *args], env=env, text=True,
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        self.lines: list[str] = []
        self._t = threading.Thread(target=self._pump, daemon=True)
        self._t.start()

    def _pump(self):
        for ln in self.p.stdout:
            self.lines.append(ln)

    def wait_for(self, needle: str, timeout: float = 60.0) -> None:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if any(needle in ln for ln in self.lines):
                return
            if self.p.poll() is not None:
                break
            time.sleep(0.05)
        raise AssertionError(f"follower never printed {needle!r}: {''.join(self.lines)}")

    def finish(self, timeout: float = 300.0) -> tuple[int, str]:
        try:
            rc = self.p.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.p.kill()
            self.p.wait(10)
            raise AssertionError(f"follower did not finish in {timeout}s: {''.join(self.lines)}")
        self._t.join(timeout=10)
        return rc, "".join(self.lines)


def _leader_held_at(monkeypatch, home, cfg, hook: str, *, fail: bool = False):
    """Start an in-process leader build that blocks inside the lock at `hook` until released.
    hook='new_epoch_id' blocks BEFORE the scan starts; hook='_is_slow_home' blocks AFTER."""
    go = threading.Event()
    held = threading.Event()
    orig = getattr(epoch_build, hook)

    def blocked(*a, **k):
        held.set()
        assert go.wait(120), "leader never released"
        if fail:
            raise epoch.EpochError("simulated leader failure inside the lock")
        return orig(*a, **k)

    monkeypatch.setattr(epoch_build, hook, blocked)
    out: dict = {}

    def run():
        try:
            out["report"] = epoch_build.build(home, cfg)
        except Exception as e:  # noqa: BLE001 -- the test inspects it
            out["error"] = e

    t = threading.Thread(target=run, daemon=True)
    t.start()
    assert held.wait(60), "leader never took the lock"
    return go, t, out


def _fake_lock(home: Path, heartbeat: float) -> None:
    d = epoch.epochs_root(home) / epoch.LOCK_DIRNAME
    d.mkdir(parents=True)
    (d / "builder.json").write_text(json.dumps({
        "host": socket.gethostname(), "pid": 999999, "boot_id": "x", "nonce": "fake",
        "heartbeat": heartbeat}))


# ---------- the defect, fixed ----------

def test_follower_exits_0_on_the_leaders_epoch_without_a_second_build(tmp_path, monkeypatch):
    home, cfg = _mk_home(tmp_path)
    go, t, out = _leader_held_at(monkeypatch, home, cfg, "new_epoch_id")  # leader holds lock, scan not started
    f = _Follower(home, "--wait-for-builder", "120")
    f.wait_for("waiting for builder pid")
    go.set()
    rc, text = f.finish()
    t.join(300)
    assert "error" not in out, out
    leader = out["report"]["epoch_id"]
    assert rc == 0, text
    assert f"epoch {leader} SATISFIED this build" in text, text
    assert "PUBLISHED" not in text, text                         # no second build
    assert _published(home) == [leader]                          # exactly one epoch exists
    assert epoch.current_epoch_id(home) == leader


def test_follower_builds_when_the_leader_fails(tmp_path, monkeypatch):
    home, cfg = _mk_home(tmp_path)
    go, t, out = _leader_held_at(monkeypatch, home, cfg, "new_epoch_id", fail=True)
    f = _Follower(home, "--wait-for-builder", "120")
    f.wait_for("waiting for builder pid")
    go.set()
    rc, text = f.finish()
    t.join(60)
    assert isinstance(out.get("error"), epoch.EpochError), out
    assert rc == 0, text
    m = re.search(r"epoch (\S+) PUBLISHED", text)
    assert m, text
    assert "SATISFIED" not in text
    assert epoch.current_epoch_id(home) == m.group(1)


def test_follower_builds_when_the_leaders_scan_began_before_it(tmp_path, monkeypatch):
    home, cfg = _mk_home(tmp_path)
    go, t, out = _leader_held_at(monkeypatch, home, cfg, "_is_slow_home")  # scan already started
    f = _Follower(home, "--wait-for-builder", "120")
    f.wait_for("waiting for builder pid")
    go.set()
    rc, text = f.finish()
    t.join(300)
    leader = out["report"]["epoch_id"]
    assert rc == 0, text
    m = re.search(r"epoch (\S+) PUBLISHED", text)
    assert m and m.group(1) != leader, text
    assert "SATISFIED" not in text
    assert sorted(_published(home)) == sorted([leader, m.group(1)])
    assert epoch.current_epoch_id(home) == m.group(1)


def test_timeout_is_the_existing_error(tmp_path):
    home, _ = _mk_home(tmp_path)
    _fake_lock(home, time.time())
    t0 = time.monotonic()
    f = _Follower(home, "--wait-for-builder", "2")
    rc, text = f.finish(60)
    assert time.monotonic() - t0 >= 2.0
    assert rc == 3, text
    assert re.search(r"^✗ another builder holds the lock \(heartbeat \d+s ago\)$", text, re.M), text
    assert "waiting for builder pid 999999" in text
    assert epoch.current_epoch_id(home) is None


def test_stale_lock_with_the_flag_is_taken_over_as_before(tmp_path):
    home, _ = _mk_home(tmp_path)
    _fake_lock(home, time.time() - 3600)
    rc, text = _Follower(home, "--wait-for-builder", "120").finish()
    assert rc == 0, text
    assert "waiting for builder" not in text
    assert re.search(r"epoch (\S+) PUBLISHED", text), text
    assert any(".stale-" in d.name for d in epoch.epochs_root(home).iterdir())


def test_a_lock_that_goes_stale_during_the_wait_is_taken_over(tmp_path):
    """The leader dies mid-wait (heartbeat stops): the follower must not sit out its whole
    budget; existing stale takeover applies as soon as the heartbeat crosses STALE_AFTER."""
    home, _ = _mk_home(tmp_path)
    _fake_lock(home, time.time() - (epoch.STALE_AFTER - 3))   # live now, stale in ~3s
    t0 = time.monotonic()
    rc, text = _Follower(home, "--wait-for-builder", "240").finish(200)
    assert rc == 0, text
    assert "waiting for builder pid 999999" in text
    assert re.search(r"epoch (\S+) PUBLISHED", text), text
    assert time.monotonic() - t0 < 120, "waited out the budget instead of taking over the stale lock"


def test_wait_zero_fails_like_no_flag(tmp_path):
    home, _ = _mk_home(tmp_path)
    _fake_lock(home, time.time())
    rc, text = _Follower(home, "--wait-for-builder", "0").finish(60)
    assert rc == 3, text
    assert re.search(r"^✗ another builder holds the lock \(heartbeat \d+s ago\)$", text, re.M), text


# ---------- controls ----------

def test_no_flag_keeps_the_existing_fail_closed_behavior(tmp_path):
    home, _ = _mk_home(tmp_path)
    _fake_lock(home, time.time())
    t0 = time.monotonic()
    env = dict(os.environ, LBRAIN_HOME=str(home))
    p = subprocess.run(["lbrain", "epoch", "build"], env=env, capture_output=True, text=True)
    assert time.monotonic() - t0 < 30
    assert p.returncode == 3
    assert re.fullmatch(r"✗ another builder holds the lock \(heartbeat \d+s ago\)\n", p.stdout), p.stdout
    assert p.stderr == "" or "waiting" not in p.stderr


def test_no_flag_with_a_free_lock_builds_as_before(tmp_path):
    home, _ = _mk_home(tmp_path)
    env = dict(os.environ, LBRAIN_HOME=str(home))
    p = subprocess.run(["lbrain", "epoch", "build"], env=env, capture_output=True, text=True)
    assert p.returncode == 0, p.stdout + p.stderr
    assert re.search(r"^✓ epoch \S+ PUBLISHED", p.stdout, re.M)


def test_manifest_records_a_subsecond_scan_start(tmp_path):
    home, cfg = _mk_home(tmp_path)
    before = time.time_ns()
    r = epoch_build.build(home, cfg)
    man = json.loads((epoch.epoch_dir(home, r["epoch_id"]) / "build-manifest.json").read_text())
    assert isinstance(man["scan_start_ns"], int) and man["scan_start_ns"] > before


# ---------- unit: what counts as satisfying ----------

def test_satisfying_epoch_rules(tmp_path):
    home, cfg = _mk_home(tmp_path)
    invoked = time.time_ns()
    r = epoch_build.build(home, cfg)
    eid = r["epoch_id"]
    assert epoch_build.satisfying_epoch(home, invoked, None)["epoch_id"] == eid
    # the epoch that was already CURRENT when the follower began never counts
    assert epoch_build.satisfying_epoch(home, invoked, eid) is None
    # a scan that began before the invocation never counts
    assert epoch_build.satisfying_epoch(home, time.time_ns(), None) is None
    # a manifest without scan_start_ns falls back to whole seconds, strictly later only
    mp = epoch.epoch_dir(home, eid) / "build-manifest.json"
    man = json.loads(mp.read_text())
    ns = man.pop("scan_start_ns")
    mp.write_text(json.dumps(man))
    sec_ns = (ns // 1_000_000_000) * 1_000_000_000
    assert epoch_build.satisfying_epoch(home, sec_ns, None) is None             # same second: unordered
    assert epoch_build.satisfying_epoch(home, sec_ns - 1, None) is not None     # strictly earlier second
    # a manifest naming another epoch, or an epoch db that is missing, never counts
    man["epoch_id"] = "someone-else"
    mp.write_text(json.dumps(man))
    assert epoch_build.satisfying_epoch(home, 0, None) is None


def test_satisfying_epoch_requires_the_published_db(tmp_path):
    home, cfg = _mk_home(tmp_path)
    invoked = time.time_ns()
    eid = epoch_build.build(home, cfg)["epoch_id"]
    db = epoch.epoch_db(home, eid)
    db.rename(db.with_name("moved.db"))
    assert epoch_build.satisfying_epoch(home, invoked, None) is None
    # and checking must never CREATE it (an sqlite open of a missing path makes an empty file)
    assert not db.exists()


def test_satisfying_epoch_requires_the_db_to_name_itself(tmp_path):
    import sqlite3
    home, cfg = _mk_home(tmp_path)
    invoked = time.time_ns()
    eid = epoch_build.build(home, cfg)["epoch_id"]
    con = sqlite3.connect(str(epoch.epoch_db(home, eid)))
    con.execute("UPDATE meta SET value='other' WHERE key='epoch_id'")
    con.commit()
    con.close()
    assert epoch_build.satisfying_epoch(home, invoked, None) is None
