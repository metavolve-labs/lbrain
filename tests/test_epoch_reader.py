"""Atomic epochs — reader integration (design v1.4, increment 3; CSO D2).

open_store() is THE entry point: legacy homes byte-identical; epoch homes get
immutable opens + leases + per-invocation CURRENT resolution; direct writes
refuse (build-validate-swap is the only write path).
"""
from __future__ import annotations

import importlib
import os
import sqlite3
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from lbrain import epoch, epoch_build

DIM = 384
MODEL = "BAAI/bge-small-en-v1.5"


def _mk_home(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    s = tmp_path / "src"
    s.mkdir()
    (s / "a.md").write_text("# A\n\nThe luminous ledger entry.\n")
    (s / "b.md").write_text("# B\n\nAnother permanent record.\n")
    (home / "config.toml").write_text("\n".join([
        'embedding_provider = "local"',
        f'embedding_model = "{MODEL}"',
        f"embedding_dim = {DIM}",
        f'db_path = "{home / "brain.db"}"',
        f'sources = [\n  "{s}",\n]',
    ]) + "\n")
    return home, str(s)


def _seed_and_build(home, src):
    # Real import always; real embed warm, deterministic vectors cold — see
    # tests/_coldembed.py for the CI cold contract.
    from _coldembed import seed_brain
    seed_brain(home, DIM)
    cfg = SimpleNamespace(sources=[src], embedding_dim=DIM, db_path=home / "brain.db")
    return epoch_build.build(home, cfg), cfg


def _bind_home(monkeypatch, home):
    monkeypatch.setenv("LBRAIN_HOME", str(home))
    import lbrain.config
    importlib.reload(lbrain.config)


def test_legacy_home_reader_is_byte_for_byte_unchanged(tmp_path, monkeypatch):
    home, src = _mk_home(tmp_path)
    _bind_home(monkeypatch, home)
    cfg = SimpleNamespace(sources=[src], embedding_dim=DIM, db_path=home / "brain.db")
    st = epoch.open_store(cfg)
    assert st.db_path == home / "brain.db" and not st.immutable
    st.db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('t','1')")  # writable
    st.close()


def test_epoch_home_reader_protocol(tmp_path, monkeypatch):
    home, src = _mk_home(tmp_path)
    report, cfg = _seed_and_build(home, src)
    _bind_home(monkeypatch, home)
    st = epoch.open_store(cfg)
    try:
        assert st.immutable and st.epoch_id == report["epoch_id"]
        assert st.db_path == epoch.epoch_db(home, report["epoch_id"])
        # a lease pins the epoch while open
        assert report["epoch_id"] in epoch.leased_epochs(home)
        # immutable means immutable — writes refuse loudly
        with pytest.raises(sqlite3.OperationalError):
            st.db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('t','1')")
        # no WAL sidecars appear next to a published epoch
        assert not (st.db_path.parent / "brain.db-wal").exists()
    finally:
        st.close()
    assert report["epoch_id"] not in epoch.leased_epochs(home)  # lease released


def test_d2_next_invocation_picks_up_a_new_epoch(tmp_path, monkeypatch):
    """CSO D2: per-invocation resolution — no daemon restart needed."""
    home, src = _mk_home(tmp_path)
    r1, cfg = _seed_and_build(home, src)
    _bind_home(monkeypatch, home)
    a = epoch.open_store(cfg)
    eid_a = a.epoch_id
    a.close()
    (Path(src) / "c.md").write_text("# C\n\nA third arrival.\n")
    r2 = epoch_build.build(home, cfg)
    b = epoch.open_store(cfg)
    try:
        assert b.epoch_id == r2["epoch_id"] != eid_a
    finally:
        b.close()


def test_direct_import_and_embed_refuse_on_an_epoch_home(tmp_path):
    home, src = _mk_home(tmp_path)
    _seed_and_build(home, src)
    env = dict(os.environ, LBRAIN_HOME=str(home))
    for args in (["import"], ["embed", "--stale"]):
        p = subprocess.run(["lbrain", *args], env=env, capture_output=True, text=True)
        assert p.returncode != 0, f"lbrain {args} should refuse on an epoch home"
        assert "only write path" in (p.stdout + p.stderr)


def test_stats_prints_the_watermark_on_an_epoch_home(tmp_path):
    home, src = _mk_home(tmp_path)
    _seed_and_build(home, src)
    env = dict(os.environ, LBRAIN_HOME=str(home))
    p = subprocess.run(["lbrain", "stats"], env=env, capture_output=True, text=True)
    assert p.returncode == 0, p.stdout + p.stderr
    assert "index current as of:" in p.stdout


def test_dead_pid_lease_is_cleaned_not_pinning_forever(tmp_path):
    home, _ = _mk_home(tmp_path)
    d = epoch.epochs_root(home) / epoch.LEASES_DIRNAME / "E1"
    d.mkdir(parents=True)
    import socket as _s
    (d / f"{_s.gethostname()}-999999").write_text("0")  # dead pid
    assert epoch.leased_epochs(home) == set()
    assert not d.exists()


# ---------- increment 4 (CSO AMBER-1 / AMBER-2) ----------

def test_amber1_reader_lands_on_new_current_when_epoch_doomed_mid_open(tmp_path, monkeypatch):
    """TOCTOU: the epoch is pruned between CURRENT-read and open — the reader
    detects, releases, and retries onto the NEW CURRENT. Deterministic loser."""
    home, src = _mk_home(tmp_path)
    r1, cfg = _seed_and_build(home, src)
    (Path(src) / "c.md").write_text("# C\n\nlater\n")
    r2 = epoch_build.build(home, cfg)  # E2 = CURRENT; E1 retained
    _bind_home(monkeypatch, home)
    # simulate: reader resolved E1 (stale read), E1 vanishes before its open lands
    real_lease = epoch.lease_acquire
    def doom_then_lease(h, eid):
        p = real_lease(h, eid)
        if eid == r1["epoch_id"]:
            import shutil as _sh
            _sh.rmtree(epoch.epoch_dir(h, eid), ignore_errors=True)
        return p
    monkeypatch.setattr(epoch, "lease_acquire", doom_then_lease)
    monkeypatch.setattr(epoch, "current_epoch_id",
                        _StaleOnce(home, r1["epoch_id"]))
    st = epoch.open_store(cfg)
    try:
        assert st.epoch_id == r2["epoch_id"]  # landed on the survivor
    finally:
        st.close()


class _StaleOnce:
    """First call returns the stale (doomed) epoch id; later calls tell the truth."""
    def __init__(self, home, stale_eid):
        self.home, self.stale, self.called = home, stale_eid, False
    def __call__(self, home):
        if not self.called:
            self.called = True
            return self.stale
        p = epoch.epochs_root(self.home) / "CURRENT"
        return p.read_text(encoding="utf-8").strip()


def test_amber2_belief_gate_refuses_with_curated_message(tmp_path):
    home, src = _mk_home(tmp_path)
    _seed_and_build(home, src)
    env = dict(os.environ, LBRAIN_HOME=str(home))
    p = subprocess.run(["lbrain", "belief", "gate", "nonexistent-slug"],
                       env=env, capture_output=True, text=True)
    assert p.returncode == 1
    assert "only write path" in (p.stdout + p.stderr)
    assert "Traceback" not in (p.stdout + p.stderr)  # curated, not a stack dump


def test_amber2_consolidate_refuses_with_curated_message(tmp_path):
    home, src = _mk_home(tmp_path)
    _seed_and_build(home, src)
    env = dict(os.environ, LBRAIN_HOME=str(home))
    p = subprocess.run(["lbrain", "consolidate", "--dry-run"],
                       env=env, capture_output=True, text=True)
    assert p.returncode == 1
    assert "only write path" in (p.stdout + p.stderr)


def test_inc4_doctor_prints_epoch_and_watermark(tmp_path):
    home, src = _mk_home(tmp_path)
    _seed_and_build(home, src)
    env = dict(os.environ, LBRAIN_HOME=str(home))
    p = subprocess.run(["lbrain", "doctor"], env=env, capture_output=True, text=True)
    assert "index current as of" in p.stdout and "epoch:" in p.stdout


def test_doctor_core_memory_health_reports_truncation_and_staleness(tmp_path):
    """CORE monitoring (Tad, 2026-09-01): the always-served layer had no
    freshness check — a CORE saying 'L0 read-only' through three promotions
    muted a seat on every query. doctor now reports truncation (A-421) and
    review-staleness."""
    home, src = _mk_home(tmp_path)
    core = home / "CORE.md"
    core.write_text("X" * 500)
    os.utime(core, (1, 1))  # ancient mtime → staleness warning
    with open(home / "config.toml", "a") as f:
        f.write(f'core_memory_path = "{core}"\ncore_memory_chars = 100\n')
    _seed_and_build(home, src)
    env = dict(os.environ, LBRAIN_HOME=str(home))
    p = subprocess.run(["lbrain", "doctor"], env=env, capture_output=True, text=True)
    out = p.stdout + p.stderr
    assert "core memory TRUNCATING" in out       # 500 chars > 100 budget
    assert "unedited for" in out                  # served always, reviewed never
