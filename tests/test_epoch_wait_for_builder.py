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
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from lbrain import epoch, epoch_build

from _coldembed import seed_brain

DIM = 384
MODEL = "BAAI/bge-small-en-v1.5"


# The CLI under test, run in a subprocess exactly as a seat runs it, with ONE difference on
# cold CI: no [local] embedding extra is installed, so the staging `embed` stage writes
# deterministic vectors instead (tests/_coldembed.py, the same shim conftest applies to
# in-process builds). Warm, the shim is a no-op and the real pipeline runs. Nothing about
# locking, waiting, following or publishing is replaced.
_BOOT = f"""
import sys
sys.path.insert(0, {str(Path(__file__).resolve().parent)!r})
from _coldembed import HAVE_LOCAL_EMBED, fill_vectors_cold
if not HAVE_LOCAL_EMBED:
    from pathlib import Path
    from lbrain import epoch_build
    _real = epoch_build._run_cli
    def _run(args, staging_home, lbrain_bin, **kw):
        if args and args[0] == "embed":
            fill_vectors_cold(Path(staging_home) / "brain.db")
            return ""
        return _real(args, staging_home, lbrain_bin, **kw)
    epoch_build._run_cli = _run
from lbrain.cli import main
sys.argv = ["lbrain"] + sys.argv[1:]
main()
"""
_LBRAIN_CLI = [sys.executable, "-c", _BOOT]


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
        self.p = subprocess.Popen(_LBRAIN_CLI + ["epoch", "build", *args], env=env, text=True,
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
    p = subprocess.run(_LBRAIN_CLI + ["epoch", "build"], env=env, capture_output=True, text=True)
    assert time.monotonic() - t0 < 30
    assert p.returncode == 3
    assert re.fullmatch(r"✗ another builder holds the lock \(heartbeat \d+s ago\)\n", p.stdout), p.stdout
    assert p.stderr == "" or "waiting" not in p.stderr


def test_no_flag_with_a_free_lock_builds_as_before(tmp_path):
    home, _ = _mk_home(tmp_path)
    env = dict(os.environ, LBRAIN_HOME=str(home))
    p = subprocess.run(_LBRAIN_CLI + ["epoch", "build"], env=env, capture_output=True, text=True)
    assert p.returncode == 0, p.stdout + p.stderr
    assert re.search(r"^✓ epoch \S+ PUBLISHED", p.stdout, re.M)


def test_manifest_records_a_subsecond_scan_start(tmp_path):
    home, cfg = _mk_home(tmp_path)
    before = time.time_ns()
    r = epoch_build.build(home, cfg)
    man = json.loads((epoch.epoch_dir(home, r["epoch_id"]) / "build-manifest.json").read_text())
    assert isinstance(man["scan_start_ns"], int) and man["scan_start_ns"] > before


# ---------- unit: what counts as satisfying ----------

def _sat(home, invoked, prior, **over):
    """satisfying_epoch as a follower with DEFAULT build flags and the home's current config."""
    kw = dict(flags=epoch_build.build_flags(), config_sha=epoch_build.config_sha(home),
              runtime=epoch_build.runtime_fingerprint(home))
    kw.update(over)
    return epoch_build.satisfying_epoch(home, invoked, prior, **kw)


def test_satisfying_epoch_rules(tmp_path):
    home, cfg = _mk_home(tmp_path)
    invoked = time.time_ns()
    r = epoch_build.build(home, cfg)
    eid = r["epoch_id"]
    assert _sat(home, invoked, None)["epoch_id"] == eid
    # the epoch that was already CURRENT when the follower began never counts
    assert _sat(home, invoked, eid) is None
    # a scan that began before the invocation never counts
    assert _sat(home, time.time_ns(), None) is None
    # a manifest without scan_start_ns falls back to whole seconds, strictly later only
    mp = epoch.epoch_dir(home, eid) / "build-manifest.json"
    man = json.loads(mp.read_text())
    ns = man.pop("scan_start_ns")
    mp.write_text(json.dumps(man))
    sec_ns = (ns // 1_000_000_000) * 1_000_000_000
    assert _sat(home, sec_ns, None) is None             # same second: unordered
    assert _sat(home, sec_ns - 1, None) is not None     # strictly earlier second
    # a manifest naming another epoch, or an epoch db that is missing, never counts
    man["epoch_id"] = "someone-else"
    mp.write_text(json.dumps(man))
    assert _sat(home, 0, None) is None


def test_satisfying_epoch_requires_the_published_db(tmp_path):
    home, cfg = _mk_home(tmp_path)
    invoked = time.time_ns()
    eid = epoch_build.build(home, cfg)["epoch_id"]
    db = epoch.epoch_db(home, eid)
    db.rename(db.with_name("moved.db"))
    assert _sat(home, invoked, None) is None
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
    assert _sat(home, invoked, None) is None


# ---------- CSO review of #89 (2026-10-17T2234Z): follow only an IDENTICAL build ----------

def _epoch_docs(home):
    import sqlite3
    db = epoch.epoch_db(home, epoch.current_epoch_id(home))
    con = sqlite3.connect(f"file:{db}?immutable=1", uri=True)
    try:
        return sorted(r[0] for r in con.execute("SELECT abs_path FROM docs"))
    finally:
        con.close()


def _follow_while_leader_held(monkeypatch, home, cfg, *follower_args, between=None):
    go, t, out = _leader_held_at(monkeypatch, home, cfg, "new_epoch_id")
    if between:
        between()
    f = _Follower(home, "--wait-for-builder", "120", *follower_args)
    f.wait_for("waiting for builder pid")
    go.set()
    rc, text = f.finish()
    t.join(300)
    assert "error" not in out, out
    return rc, text, out["report"]["epoch_id"]


def test_d1_follower_with_prune_unreachable_does_not_adopt_a_leader_without_it(tmp_path, monkeypatch):
    """CSO probe test_flag_drift_prune_unreachable."""
    home, cfgX = _mk_home(tmp_path)
    old = tmp_path / "old"
    old.mkdir()
    (old / "stale.md").write_text("# Stale\n\nThis source was removed from config.\n")
    raw1 = (home / "config.toml").read_text()
    (home / "config.toml").write_text(raw1.replace(f'sources = ["{cfgX.sources[0]}"]',
                                                   f'sources = ["{cfgX.sources[0]}", "{old}"]'))
    epoch_build.build(home, SimpleNamespace(sources=[cfgX.sources[0], str(old)], embedding_dim=DIM,
                                            db_path=cfgX.db_path))
    (home / "config.toml").write_text(raw1)                 # old source dropped from config
    assert any(p.endswith("stale.md") for p in _epoch_docs(home))
    rc, text, leader = _follow_while_leader_held(monkeypatch, home, cfgX, "--prune-unreachable")
    assert rc == 0, text
    assert "SATISFIED" not in text and re.search(r"epoch (\S+) PUBLISHED", text), text
    assert not any(p.endswith("stale.md") for p in _epoch_docs(home))


def test_d1_follower_with_full_does_not_adopt_a_delta_leader(tmp_path, monkeypatch):
    """CSO probe test_flag_drift_full."""
    home, cfg = _mk_home(tmp_path)
    epoch_build.build(home, cfg)
    rc, text, leader = _follow_while_leader_held(monkeypatch, home, cfg, "--full")
    assert rc == 0, text
    assert "SATISFIED" not in text, text
    man = json.loads((epoch.epoch_dir(home, epoch.current_epoch_id(home)) / "build-manifest.json").read_text())
    assert man["delta"] is False and man["build_flags"]["delta"] is False


def test_d1_follower_with_no_sweep_does_not_adopt_a_sweeping_leader(tmp_path, monkeypatch):
    home, cfg = _mk_home(tmp_path)
    rc, text, leader = _follow_while_leader_held(monkeypatch, home, cfg, "--no-sweep")
    assert rc == 0 and "SATISFIED" not in text, text


def test_d2_config_changed_after_the_leader_loaded_it_is_not_adopted(tmp_path, monkeypatch):
    """CSO probe a1: an exclude marker added after the leader loaded its config."""
    home, cfg = _mk_home(tmp_path)
    epoch_build.build(home, cfg)
    add_marker = lambda: (home / "config.toml").write_text(
        (home / "config.toml").read_text() + 'exclude_path_markers = ["/beta.md"]\n')
    rc, text, leader = _follow_while_leader_held(monkeypatch, home, cfg, between=add_marker)
    assert rc == 0, text
    assert "SATISFIED" not in text, text
    assert not any(p.endswith("beta.md") for p in _epoch_docs(home))


def test_d2_source_added_after_the_leader_loaded_config_is_not_adopted(tmp_path, monkeypatch):
    """CSO probe a2: the leader's manifest would omit a source its db serves."""
    home, cfg = _mk_home(tmp_path)
    epoch_build.build(home, cfg)
    src2 = tmp_path / "src2"
    src2.mkdir()
    (src2 / "newsrc.md").write_text("# New\n\nA brand new source document, words.\n")

    def add_source():
        raw = (home / "config.toml").read_text()
        (home / "config.toml").write_text(raw.replace(f'sources = ["{cfg.sources[0]}"]',
                                                      f'sources = ["{cfg.sources[0]}", "{src2}"]'))
    rc, text, leader = _follow_while_leader_held(monkeypatch, home, cfg, between=add_source)
    assert rc == 0 and "SATISFIED" not in text, text
    man = json.loads((epoch.epoch_dir(home, epoch.current_epoch_id(home)) / "build-manifest.json").read_text())
    assert str(src2) in man["source_digests"]


def test_manifest_records_flags_config_and_builder_identity(tmp_path):
    home, cfg = _mk_home(tmp_path)
    eid = epoch_build.build(home, cfg, prune_unreachable=True, keep=5)["epoch_id"]
    man = json.loads((epoch.epoch_dir(home, eid) / "build-manifest.json").read_text())
    assert man["build_flags"] == json.loads(json.dumps(
        epoch_build.build_flags(prune_unreachable=True, keep=5)))
    assert man["config_sha256"] == man["config_sha256_staged"] == epoch_build.config_sha(home)
    assert man["builder_host"] == socket.gethostname()
    assert man["builder_boot_id"] == epoch._boot_id()


def test_unit_each_identity_field_must_match(tmp_path):
    home, cfg = _mk_home(tmp_path)
    invoked = time.time_ns()
    eid = epoch_build.build(home, cfg)["epoch_id"]
    assert _sat(home, invoked, None)["epoch_id"] == eid                       # control
    for flag, val in (("delta", False), ("prune_unreachable", True), ("confirm_delist", True),
                      ("sweep", False), ("keep", 4), ("max_bytes", 10),
                      ("confirm_source_removed", ("/x",))):
        assert _sat(home, invoked, None, flags=epoch_build.build_flags(**{flag: val})) is None, flag
    assert _sat(home, invoked, None, config_sha="0" * 64) is None
    assert _sat(home, invoked, None, config_sha=epoch_build.UNSTABLE_CONFIG) is None
    mp = epoch.epoch_dir(home, eid) / "build-manifest.json"
    base = mp.read_text()
    for key, val in (("builder_host", "elsewhere"), ("builder_boot_id", "other-boot"),
                     ("builder_boot_id", ""), ("config_sha256_staged", "f" * 64)):
        man = json.loads(base)
        man[key] = val
        mp.write_text(json.dumps(man))
        assert _sat(home, invoked, None) is None, (key, val)
    for key in ("build_flags", "config_sha256", "builder_host", "builder_boot_id"):
        man = json.loads(base)
        del man[key]
        mp.write_text(json.dumps(man))
        assert _sat(home, invoked, None) is None, key                        # an older manifest never counts
    mp.write_text(base)
    assert _sat(home, invoked, None)["epoch_id"] == eid


def test_unit_an_unstable_config_never_matches_even_itself(tmp_path):
    home, cfg = _mk_home(tmp_path)
    invoked = time.time_ns()
    eid = epoch_build.build(home, cfg, config_sha=epoch_build.UNSTABLE_CONFIG)["epoch_id"]
    man = json.loads((epoch.epoch_dir(home, eid) / "build-manifest.json").read_text())
    man["config_sha256_staged"] = epoch_build.UNSTABLE_CONFIG
    (epoch.epoch_dir(home, eid) / "build-manifest.json").write_text(json.dumps(man))
    assert _sat(home, invoked, None, config_sha=epoch_build.UNSTABLE_CONFIG) is None


def test_unit_an_unknown_boot_id_never_matches_even_itself(tmp_path, monkeypatch):
    """Off Linux there is no boot_id; two empty strings are not proof of one clock."""
    monkeypatch.setattr(epoch_build, "_boot_id", lambda: "")
    home, cfg = _mk_home(tmp_path)
    invoked = time.time_ns()
    eid = epoch_build.build(home, cfg)["epoch_id"]
    man = json.loads((epoch.epoch_dir(home, eid) / "build-manifest.json").read_text())
    assert man["builder_boot_id"] == ""
    assert _sat(home, invoked, None) is None


def test_cli_a_config_written_during_load_is_unstable(monkeypatch):
    """The CLI brackets Config.load() with two reads; a write between them must yield UNSTABLE
    (no epoch can match it), never the pre-write sha (CSO #89 D2, the ms race)."""
    import lbrain.cli as cli
    from click.testing import CliRunner
    cfgp = cli.CONFIG_DIR / "config.toml"
    cfgp.write_text('sources = []\n')
    real_load = cli.Config.load

    def load_and_change(*a, **k):
        c = real_load(*a, **k)
        cfgp.write_text('sources = []\n# changed during load\n')
        return c

    seen = {}

    def fake_single_flight(home, cfg, **kw):
        seen.update(kw)
        return {"followed": True, "epoch_id": "x", "scan_start": "s", "scan_end": "e"}

    monkeypatch.setattr(cli.Config, "load", staticmethod(load_and_change))
    monkeypatch.setattr(epoch_build, "build_single_flight", fake_single_flight)
    res = CliRunner().invoke(cli.main, ["epoch", "build", "--wait-for-builder", "1"])
    assert res.exit_code == 0, res.output
    assert seen["config_sha"] == epoch_build.UNSTABLE_CONFIG, seen


def test_cli_a_stable_config_passes_its_sha(monkeypatch):
    """Control for the bracket: no write during load -> the real sha."""
    import lbrain.cli as cli
    from click.testing import CliRunner
    (cli.CONFIG_DIR / "config.toml").write_text('sources = []\n')
    seen = {}

    def fake_single_flight(home, cfg, **kw):
        seen.update(kw)
        return {"followed": True, "epoch_id": "x", "scan_start": "s", "scan_end": "e"}

    monkeypatch.setattr(epoch_build, "build_single_flight", fake_single_flight)
    res = CliRunner().invoke(cli.main, ["epoch", "build", "--wait-for-builder", "1"])
    assert res.exit_code == 0, res.output
    assert seen["config_sha"] == epoch_build.config_sha(cli.CONFIG_DIR)


# ---------- CSO round 2 on #89 (2026-10-18T0008Z) ----------

def _man(home):
    return json.loads((epoch.epoch_dir(home, epoch.current_epoch_id(home)) / "build-manifest.json").read_text())


def test_r2_explicit_defaults_follow_implicit_defaults(tmp_path, monkeypatch):
    """CSO r2 (a), control: `--keep 3` spelled out still adopts a leader that passed nothing."""
    home, cfg = _mk_home(tmp_path)
    epoch_build.build(home, cfg)
    go, t, out = _leader_held_at(monkeypatch, home, cfg, "new_epoch_id")
    f = _Follower(home, "--wait-for-builder", "120", "--keep", "3")
    f.wait_for("waiting for builder pid")
    go.set()
    rc, text = f.finish()
    t.join(300)
    assert rc == 0 and "SATISFIED" in text, text


def test_r2_d1_a_leader_whose_config_changed_before_build_entry_is_not_adopted(tmp_path, monkeypatch):
    """CSO r2 (c): the leader holds config X in memory; config.toml became Y before build()
    entry. Stamping the sha AT ENTRY recorded sha(Y) twice and the follower adopted an epoch
    whose manifest omits a source its config names."""
    home, cfgX = _mk_home(tmp_path)
    epoch_build.build(home, cfgX)
    src2 = tmp_path / "src2"
    src2.mkdir()
    (src2 / "newsrc.md").write_text("# New\n\nA brand new source document, words.\n")
    raw = (home / "config.toml").read_text()
    (home / "config.toml").write_text(raw.replace(f'sources = ["{cfgX.sources[0]}"]',
                                                  f'sources = ["{cfgX.sources[0]}", "{src2}"]'))
    go, t, out = _leader_held_at(monkeypatch, home, cfgX, "new_epoch_id")
    f = _Follower(home, "--wait-for-builder", "120")
    f.wait_for("waiting for builder pid")
    go.set()
    rc, text = f.finish()
    t.join(300)
    assert rc == 0, text
    assert "SATISFIED" not in text, text
    assert str(src2) in _man(home)["source_digests"]


def test_r2_d1_end_to_end_an_unstable_leader_is_not_adopted(tmp_path, monkeypatch):
    """End to end for the UNSTABLE path: a leader whose config sha is unstable publishes, and a
    waiting follower with the same flags and the same config builds instead of adopting it."""
    home, cfg = _mk_home(tmp_path)
    go = threading.Event()
    held = threading.Event()
    orig = epoch_build.new_epoch_id

    def blocked(*a, **k):
        held.set()
        assert go.wait(120)
        return orig(*a, **k)

    monkeypatch.setattr(epoch_build, "new_epoch_id", blocked)
    out = {}
    t = threading.Thread(target=lambda: out.update(
        report=epoch_build.build(home, cfg, config_sha=epoch_build.UNSTABLE_CONFIG)), daemon=True)
    t.start()
    assert held.wait(60)
    f = _Follower(home, "--wait-for-builder", "120")
    f.wait_for("waiting for builder pid")
    go.set()
    rc, text = f.finish()
    t.join(300)
    leader = out["report"]["epoch_id"]
    assert json.loads((epoch.epoch_dir(home, leader) / "build-manifest.json").read_text())[
        "config_sha256"] == epoch_build.UNSTABLE_CONFIG
    assert rc == 0 and "SATISFIED" not in text and re.search(r"epoch (\S+) PUBLISHED", text), text


def test_r2_d1_plain_cli_build_records_the_bracketed_load_sha(tmp_path):
    """Every CLI build path brackets Config.load(): a plain `lbrain epoch build` records the
    sha of the config it loaded (== the file, nothing changed), not a None-at-entry read."""
    home, _ = _mk_home(tmp_path)
    p = subprocess.run(_LBRAIN_CLI + ["epoch", "build"], env=dict(os.environ, LBRAIN_HOME=str(home)),
                       capture_output=True, text=True)
    assert p.returncode == 0, p.stdout + p.stderr
    man = _man(home)
    assert man["config_sha256"] == epoch_build.config_sha(home) == man["config_sha256_staged"]


def test_r2_d1_plain_cli_build_passes_the_bracket_into_build(monkeypatch):
    """The plain path (no flag) must hand build() the bracketed sha, and an unstable load must
    reach it as UNSTABLE (not None, which would be re-read at entry)."""
    import lbrain.cli as cli
    from click.testing import CliRunner
    cfgp = cli.CONFIG_DIR / "config.toml"
    cfgp.write_text('sources = []\n')
    real_load = cli.Config.load

    def load_and_change(*a, **k):
        c = real_load(*a, **k)
        cfgp.write_text('sources = []\n# changed during load\n')
        return c

    seen = {}

    def fake_build(home, cfg, **kw):
        seen.update(kw)
        return {"epoch_id": "x", "docs": 0, "scan_start": "s", "scan_end": "e"}

    monkeypatch.setattr(cli.Config, "load", staticmethod(load_and_change))
    monkeypatch.setattr(epoch_build, "build", fake_build)
    res = CliRunner().invoke(cli.main, ["epoch", "build"])
    assert res.exit_code == 0, res.output
    assert seen["config_sha"] == epoch_build.UNSTABLE_CONFIG, seen


def test_r2_d2_a_different_embedding_base_url_in_env_is_not_adopted(tmp_path, monkeypatch):
    """GEMINI_BASE_URL comes from the environment, outside config.toml: a leader that ran with a
    different one may have produced different vectors."""
    home, cfg = _mk_home(tmp_path)
    monkeypatch.setenv("GEMINI_BASE_URL", "https://leader.example.invalid/v1")
    go, t, out = _leader_held_at(monkeypatch, home, cfg, "new_epoch_id")   # fingerprint taken at entry
    monkeypatch.delenv("GEMINI_BASE_URL")
    f = _Follower(home, "--wait-for-builder", "120")
    f.wait_for("waiting for builder pid")
    go.set()
    rc, text = f.finish()
    t.join(300)
    assert rc == 0 and "SATISFIED" not in text, text


def test_r2_d2_a_different_credential_set_is_not_adopted(tmp_path, monkeypatch):
    home, cfg = _mk_home(tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "placeholder-not-a-key")
    go, t, out = _leader_held_at(monkeypatch, home, cfg, "new_epoch_id")
    monkeypatch.delenv("OPENAI_API_KEY")
    f = _Follower(home, "--wait-for-builder", "120")
    f.wait_for("waiting for builder pid")
    go.set()
    rc, text = f.finish()
    t.join(300)
    assert rc == 0 and "SATISFIED" not in text, text


def test_r2_d2_runtime_inputs_carry_names_never_values(tmp_path, monkeypatch):
    home, cfg = _mk_home(tmp_path)
    marker = "do-not-record-" + "this-value-123"
    monkeypatch.setenv("GEMINI_API_KEY", marker)
    (home / "env").write_text(f"OPENAI_API_KEY={marker}\n")
    eid = epoch_build.build(home, cfg)["epoch_id"]
    text = (epoch.epoch_dir(home, eid) / "build-manifest.json").read_text()
    assert marker not in text
    rt = json.loads(text)["runtime_inputs"]
    assert "GEMINI_API_KEY" in rt["credential_names"]
    assert rt["env_file_present"] is True
    assert rt["engine_version"] and rt["engine_stamp"]
    assert json.loads(text)["runtime_fingerprint"] == epoch_build.runtime_fingerprint(home)


def test_r2_d2_unit_runtime_fingerprint_must_match(tmp_path):
    home, cfg = _mk_home(tmp_path)
    invoked = time.time_ns()
    eid = epoch_build.build(home, cfg)["epoch_id"]
    assert _sat(home, invoked, None)["epoch_id"] == eid                      # control
    mp = epoch.epoch_dir(home, eid) / "build-manifest.json"
    base = json.loads(mp.read_text())
    for bad in ("0" * 64, None):
        man = dict(base)
        if bad is None:
            del man["runtime_fingerprint"]
        else:
            man["runtime_fingerprint"] = bad
        mp.write_text(json.dumps(man))
        assert _sat(home, invoked, None) is None, bad


def test_r2_d1_an_in_process_follower_with_a_stale_cfg_is_unstable(tmp_path, monkeypatch):
    """build_single_flight called in-process without a sha must use the same entry check as
    build(): a cfg the file no longer describes yields UNSTABLE, never the file's sha."""
    home, cfg = _mk_home(tmp_path)
    raw = (home / "config.toml").read_text()
    (home / "config.toml").write_text(raw.replace(f'sources = ["{cfg.sources[0]}"]',
                                                  f'sources = ["{cfg.sources[0]}", "/elsewhere"]'))
    seen = {}

    def fake_build(home_, cfg_, **kw):
        seen.update(kw)
        return {"epoch_id": "x"}

    monkeypatch.setattr(epoch_build, "build", fake_build)
    epoch_build.build_single_flight(home, cfg, wait=0, invoked_ns=time.time_ns())
    assert seen["config_sha"] == epoch_build.UNSTABLE_CONFIG, seen


def test_r2_d1_entry_sha_is_the_file_sha_when_the_file_describes_cfg(tmp_path):
    """Control for the entry check."""
    home, cfg = _mk_home(tmp_path)
    assert epoch_build._entry_config_sha(home, cfg) == epoch_build.config_sha(home)
