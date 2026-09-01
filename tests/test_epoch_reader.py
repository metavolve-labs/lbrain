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
    env = dict(os.environ, LBRAIN_HOME=str(home))
    for args in (["import"], ["embed", "--stale"]):
        p = subprocess.run(["lbrain", *args], env=env, capture_output=True, text=True)
        assert p.returncode == 0, p.stdout + p.stderr
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
