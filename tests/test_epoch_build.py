"""Atomic epochs — build/validate/swap (design v1.4, increment 2).

Integration-grade: the real import/embed pipeline runs in staging subprocesses,
exactly as production does. Each test is a RED target from the combined gauntlet.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import struct
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from lbrain import epoch, epoch_build

DIM = 384
MODEL = "BAAI/bge-small-en-v1.5"


def _mk_home(tmp_path, n_sources=1):
    home = tmp_path / "home"
    home.mkdir()
    srcs = []
    for i in range(n_sources):
        s = tmp_path / f"src{i}"
        s.mkdir()
        (s / f"alpha{i}.md").write_text(f"# Alpha {i}\n\nThe quicksilver archive holds record {i}.\n")
        (s / f"beta{i}.md").write_text(f"# Beta {i}\n\nAnother distinctive passage number {i}.\n")
        srcs.append(str(s))
    lines = [
        'embedding_provider = "local"',
        f'embedding_model = "{MODEL}"',
        f"embedding_dim = {DIM}",
        f'db_path = "{home / "brain.db"}"',
        "sources = [",
        *[f'  "{s}",' for s in srcs],
        "]",
    ]
    (home / "config.toml").write_text("\n".join(lines) + "\n")
    (home / "identity.json").write_text('{"who": "test-seat"}\n')
    return home, srcs


def _cfg(home, srcs):
    return SimpleNamespace(sources=list(srcs), embedding_dim=DIM, db_path=home / "brain.db")


def _seed_legacy(home):
    env = dict(os.environ, LBRAIN_HOME=str(home))
    for args in (["import"], ["embed", "--stale"]):
        p = subprocess.run(["lbrain", *args], env=env, capture_output=True, text=True)
        assert p.returncode == 0, p.stdout + p.stderr


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


# ---------- happy path ----------

def test_full_build_publishes_watermarked_checkpointed_epoch(tmp_path):
    home, srcs = _mk_home(tmp_path)
    _seed_legacy(home)
    report = epoch_build.build(home, _cfg(home, srcs))
    assert report["published"] and report["docs"] == 2
    eid = report["epoch_id"]
    assert epoch.current_epoch_id(home) == eid
    db = epoch.resolve_db_path(home, home / "brain.db")
    assert db == epoch.epoch_db(home, eid)
    con = sqlite3.connect(str(db))
    try:
        assert con.execute("PRAGMA journal_mode").fetchone()[0] == "delete"  # single-file publish
        meta = dict(con.execute("SELECT key, value FROM meta WHERE key LIKE 'watermark%' OR key='epoch_id'"))
        assert meta["epoch_id"] == eid and meta["watermark_scan_end"]
        digests = json.loads(meta["watermark_source_digests"])
        assert set(digests) == {str(Path(s)) for s in srcs}
    finally:
        con.close()
    assert not (db.parent / "brain.db-wal").exists()  # WAL sidecars never published
    assert (db.parent / "identity.json").read_bytes() == (home / "identity.json").read_bytes()
    assert (db.parent / "build-manifest.json").exists()


def test_delta_build_never_mutates_the_prior_epoch(tmp_path):
    """CSO D1 — the rollback target must stay byte-identical through a delta build."""
    home, srcs = _mk_home(tmp_path)
    _seed_legacy(home)
    r1 = epoch_build.build(home, _cfg(home, srcs))
    prior_db = epoch.epoch_db(home, r1["epoch_id"])
    before = _sha(prior_db)
    (Path(srcs[0]) / "gamma.md").write_text("# Gamma\n\nA new arrival after epoch one.\n")
    r2 = epoch_build.build(home, _cfg(home, srcs))
    assert r2["docs"] == 3 and r2["epoch_id"] != r1["epoch_id"]
    assert _sha(prior_db) == before, "delta build mutated the retained rollback target"


# ---------- crash / interruption ----------

def test_interrupted_build_leaves_the_live_brain_untouched(tmp_path, monkeypatch):
    """The kill-mid-build RED: any death before publish must change NOTHING."""
    home, srcs = _mk_home(tmp_path)
    _seed_legacy(home)
    legacy_sha = _sha(home / "brain.db")

    _real = epoch_build._run_cli
    crashed = {"done": False}

    def die_once(args, staging_home, lbrain_bin, **kw):
        if args[0] == "embed" and not crashed["done"]:
            crashed["done"] = True
            raise epoch_build.EpochError("simulated crash mid-embed")
        return _real(args, staging_home, lbrain_bin, **kw)

    monkeypatch.setattr(epoch_build, "_run_cli", die_once)
    with pytest.raises(epoch_build.EpochError):
        epoch_build.build(home, _cfg(home, srcs))
    assert epoch.current_epoch_id(home) is None      # nothing published
    assert _sha(home / "brain.db") == legacy_sha     # live brain untouched
    assert not epoch.list_epochs(home)               # no half-epoch in the roster
    # and the lock was released — a second build can run
    r = epoch_build.build(home, _cfg(home, srcs))
    assert r["published"]


# ---------- deletion manifest (CSO v1.4 amendment) ----------

def test_hollow_source_root_refuses_without_confirmation(tmp_path):
    home, srcs = _mk_home(tmp_path, n_sources=2)
    _seed_legacy(home)
    epoch_build.build(home, _cfg(home, srcs))
    for f in Path(srcs[1]).glob("*.md"):
        f.unlink()  # the root EXISTS but enumerates empty — decoy/hollow
    with pytest.raises(epoch_build.EpochError, match="enumerated EMPTY"):
        epoch_build.build(home, _cfg(home, srcs))
    r = epoch_build.build(home, _cfg(home, srcs), confirm_source_removed=(srcs[1],))
    assert r["published"] and r["docs"] == 2


def test_vanished_source_root_refuses_without_confirmation(tmp_path):
    import shutil as _sh
    home, srcs = _mk_home(tmp_path, n_sources=2)
    _seed_legacy(home)
    epoch_build.build(home, _cfg(home, srcs))
    _sh.rmtree(srcs[1])
    with pytest.raises(epoch_build.EpochError, match="VANISHED"):
        epoch_build.build(home, _cfg(home, srcs))
    r = epoch_build.build(home, _cfg(home, srcs), confirm_source_removed=(srcs[1],))
    assert r["published"] and r["docs"] == 2


# ---------- gate v2: the right number of zeros (Grok G3) ----------

def test_zero_vector_candidate_is_refused(tmp_path):
    home, srcs = _mk_home(tmp_path)
    _seed_legacy(home)
    db = home / "brain.db"
    con = epoch_build._connect_vec(db)
    try:
        rows = [r[0] for r in con.execute("SELECT rowid FROM vec_chunks")]
        con.execute("DELETE FROM vec_chunks")
        zero = struct.pack(f"{DIM}f", *([0.0] * DIM))
        for rid in rows:
            con.execute("INSERT INTO vec_chunks (rowid, embedding) VALUES (?, ?)", (rid, zero))
        con.commit()
    finally:
        con.close()
    failures = epoch_build.validate_candidate(
        db, embedding_dim=DIM, sources=srcs, prior_inv={}, confirmed_removed=set())
    assert any("zero norm" in f for f in failures), failures


def test_wrong_dimension_is_refused(tmp_path):
    home, srcs = _mk_home(tmp_path)
    _seed_legacy(home)
    failures = epoch_build.validate_candidate(
        home / "brain.db", embedding_dim=1536, sources=srcs, prior_inv={}, confirmed_removed=set())
    assert any("dim" in f for f in failures), failures


# ---------- CSO RED/GREEN round 1 — build-side REDs ----------

def test_r1_heartbeat_runs_DURING_a_long_stage(tmp_path):
    """R1: a real embed runs minutes; the lock must stay fresh mid-stage, not
    only between stages."""
    home = tmp_path / "h"
    (home / "epochs").mkdir(parents=True)
    lock = epoch.BuilderLock(home).acquire()
    import json as _json
    hb0 = _json.loads(lock.meta.read_text())["heartbeat"]
    epoch_build._run_cli(["2"], home, "sleep", lock=lock, hb_interval=0.3)
    hb1 = _json.loads(lock.meta.read_text())["heartbeat"]
    assert hb1 > hb0, "no heartbeat happened during the stage"
    lock.release()


def test_r1b_seizure_mid_stage_aborts_the_running_build(tmp_path):
    """A builder whose lock was seized must abort its stage, not keep building."""
    home = tmp_path / "h"
    (home / "epochs").mkdir(parents=True)
    lock = epoch.BuilderLock(home).acquire()
    # simulate the usurper: rewrite meta with a different nonce
    import json as _json
    m = _json.loads(lock.meta.read_text())
    m["nonce"] = "someone-else"
    lock.meta.write_text(_json.dumps(m))
    with pytest.raises(epoch.LockLost):
        epoch_build._run_cli(["5"], home, "sleep", lock=lock, hb_interval=0.2)


def test_r3_scrambled_fts_bindings_are_refused(tmp_path):
    """R3: same texts under the WRONG docs must fail the probe — 'something
    returned' is a heartbeat, not a check."""
    home, srcs = _mk_home(tmp_path)
    _seed_legacy(home)
    db = home / "brain.db"
    con = epoch_build._connect_vec(db)
    try:
        con.execute("UPDATE fts_chunks SET rel_path = 'scrambled/wrong.md'")
        con.commit()
    finally:
        con.close()
    failures = epoch_build.validate_candidate(
        db, embedding_dim=DIM, sources=srcs, prior_inv={}, confirmed_removed=set())
    assert any("bindings suspect" in f for f in failures), failures


def test_r4_tail_zeroed_vector_is_caught(tmp_path):
    """R4: the realistic embed-failure shape is tail-shaped; a prefix sample is
    blind to it. Full scan must catch a single zeroed LAST row."""
    home, srcs = _mk_home(tmp_path)
    _seed_legacy(home)
    db = home / "brain.db"
    con = epoch_build._connect_vec(db)
    try:
        last = con.execute("SELECT MAX(rowid) FROM vec_chunks").fetchone()[0]
        con.execute("DELETE FROM vec_chunks WHERE rowid = ?", (last,))
        con.execute("INSERT INTO vec_chunks (rowid, embedding) VALUES (?, ?)",
                    (last, struct.pack(f"{DIM}f", *([0.0] * DIM))))
        con.commit()
    finally:
        con.close()
    failures = epoch_build.validate_candidate(
        db, embedding_dim=DIM, sources=srcs, prior_inv={}, confirmed_removed=set())
    assert any("zero norm" in f for f in failures), failures


def test_orphan_vectors_are_swept_not_tolerated(tmp_path):
    """First production build was refused over 14 chunk-less vectors the legacy
    brain had carried invisibly. The build sweeps and REPORTS them; the gate's
    embedded==chunks bar stays strict."""
    home, srcs = _mk_home(tmp_path)
    _seed_legacy(home)
    con = epoch_build._connect_vec(home / "brain.db")
    try:
        con.execute("INSERT INTO vec_chunks (rowid, embedding) VALUES (999999, ?)",
                    (struct.pack(f"{DIM}f", *([0.5] * DIM)),))
        con.commit()
    finally:
        con.close()
    r = epoch_build.build(home, _cfg(home, srcs))
    assert r["published"] and r["orphan_vectors_swept"] == 1
