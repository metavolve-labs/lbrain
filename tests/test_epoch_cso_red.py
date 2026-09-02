"""CSO adversarial fixtures — epochs (touchstone, rounds 1-2, 2026-08-31/09-01).

Round 1 (pre-944fd4e) these six found six real defects: live-builder lock seizure
(R1), lease-stomp by the deposed builder (R1b), publish() trusting unvetted epoch
dirs (R2), scrambled FTS bindings passing the gate (R3), tail-blind vector
sampling (R4). R6 verified the deletion-manifest mount-absent amendment. They stay
in the tree as regression fixtures in their post-fix (invariant-asserting) form.
Staged separately, not in this file: D3 probes on a real /mnt/c home with a
Windows-side holder, and real kill -9 builds (roster exclusion + orphan sweep).
"""

import hashlib
import os
import sqlite3
import struct
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from lbrain import epoch, epoch_build

DIM = 384
MODEL = "BAAI/bge-small-en-v1.5"


def _mk_home(tmp_path, n_docs=2, n_sources=1):
    home = tmp_path / "home"
    home.mkdir()
    srcs = []
    for i in range(n_sources):
        s = tmp_path / f"src{i}"
        s.mkdir()
        for j in range(n_docs):
            (s / f"doc{i}_{j}.md").write_text(
                f"# Doc {i}.{j}\n\nDistinctive passage marker{i}x{j} in the quicksilver archive.\n")
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
    (home / "identity.json").write_text('{"who": "cso-red"}\n')
    return home, srcs


def _seed(home):
    # Real import always; real embed warm, deterministic vectors cold — see
    # tests/_coldembed.py for the CI cold contract.
    from _coldembed import seed_brain
    seed_brain(home, DIM)


def _cfg(home, srcs):
    return SimpleNamespace(sources=list(srcs), embedding_dim=DIM, db_path=home / "brain.db")


# R1 — INVARIANT (post-fix form): a builder whose heartbeat is MAINTAINED at the
# designed cadence can never be seized, however long its stage runs relative to
# stale_after. (Round 1 proved build() had no in-stage heartbeat; the fix added a
# 10s poll-loop heartbeat inside _run_cli.)
def test_r1_heartbeating_builder_cannot_be_seized(tmp_path):
    import threading
    a = epoch.BuilderLock(tmp_path, stale_after=0.3).acquire()
    stop = threading.Event()

    def hb():
        while not stop.is_set():
            a.heartbeat()
            time.sleep(0.05)

    t = threading.Thread(target=hb, daemon=True)
    t.start()
    try:
        time.sleep(0.6)  # a "stage" runs 2x stale_after
        with pytest.raises(epoch.BuilderBusy):
            epoch.BuilderLock(tmp_path, stale_after=0.3).acquire()
    finally:
        stop.set()
        t.join()
        a.release()


# R1b — a deposed builder must die on LockLost and must NOT stomp the usurper's
# lease (round 1: its heartbeat silently recreated meta in the new holder's dir).
def test_r1b_deposed_builder_raises_locklost_and_never_stomps(tmp_path):
    a = epoch.BuilderLock(tmp_path, stale_after=0.2).acquire()
    time.sleep(0.4)
    b = epoch.BuilderLock(tmp_path, stale_after=0.2).acquire()  # legit stale takeover
    try:
        with pytest.raises(epoch.LockLost):
            a.heartbeat()
        import json
        meta = json.loads(b.meta.read_text(encoding="utf-8"))
        assert meta["nonce"] == b.nonce, "deposed builder overwrote the usurper's lease"
        a.release()  # must be a no-op on a lock it no longer owns
        assert b.meta.exists() and b.dir.exists(), "deposed release() deleted the usurper's lock"
    finally:
        b.release()


# R2 — INVARIANT: publish() must refuse an epoch that never passed the gate.
# A kill -9 during VACUUM INTO leaves epochs/<eid>/brain.db as a PARTIAL file in
# published shape; build-manifest.json is written only after the post-vacuum
# re-check, so its absence marks an unvetted epoch. Manual rollback (the emergency
# path) calls publish() directly.
def test_r2_publish_refuses_an_unvetted_partial_epoch(tmp_path):
    d = epoch.epoch_dir(tmp_path, "PARTIAL")
    d.mkdir(parents=True)
    (d / "brain.db").write_bytes(b"SQLite format 3\x00" + b"\x00" * 512)  # torn VACUUM
    with pytest.raises(epoch.EpochError):
        epoch.publish(tmp_path, "PARTIAL")


# R3 — INVARIANT (panel: "a self-test that can't fail is a heartbeat"): the FTS
# probe must catch a wrong text->doc binding, not just emptiness. Same row count,
# same texts, rel_path bindings scrambled = serving misattribution.
def test_r3_gate_catches_scrambled_fts_binding(tmp_path):
    # disjoint vocabularies per doc — a shared word would put the probed doc among
    # the hits even with scrambled bindings and mask the defect
    home = tmp_path / "home"; home.mkdir()
    src = tmp_path / "src0"; src.mkdir()
    vocab = [("aardwolf", "basilisk"), ("chimera", "dryadic"), ("erlking", "fomorian")]
    for j, (w1, w2) in enumerate(vocab):
        (src / f"doc{j}.md").write_text(f"# T{j}\n\n{w1} {w2} {w1}{w2}\n")
    srcs = [str(src)]
    (home / "config.toml").write_text("\n".join([
        'embedding_provider = "local"',
        f'embedding_model = "{MODEL}"',
        f"embedding_dim = {DIM}",
        f'db_path = "{home / "brain.db"}"',
        "sources = [", f'  "{src}",', "]",
    ]) + "\n")
    (home / "identity.json").write_text('{"who": "cso-red"}\n')
    _seed(home)
    db = home / "brain.db"
    con = sqlite3.connect(str(db))
    try:
        rows = con.execute("SELECT rowid, rel_path, text FROM fts_chunks").fetchall()
        con.execute("DELETE FROM fts_chunks")
        paths = [r[1] for r in rows]
        rotated = paths[1:] + paths[:1]
        for (rowid, _, text), wrong_path in zip(rows, rotated):
            con.execute("INSERT INTO fts_chunks (rowid, rel_path, text) VALUES (?,?,?)",
                        (rowid, wrong_path, text))
        con.commit()
    finally:
        con.close()
    failures = epoch_build.validate_candidate(
        db, embedding_dim=DIM, sources=srcs, prior_inv={}, confirmed_removed=set())
    assert failures, "gate passed a candidate whose FTS serves the WRONG document"


# R4 — INVARIANT: zero/NaN vectors are refused wherever they sit. The sample is
# `LIMIT 32` in rowid order — corruption past row 32 (a partial embed failure,
# the realistic shape) must still refuse.
def test_r4_gate_catches_zero_vectors_beyond_the_first_32(tmp_path):
    home, srcs = _mk_home(tmp_path, n_docs=40)
    _seed(home)
    db = home / "brain.db"
    con = epoch_build._connect_vec(db)
    try:
        rowids = [r[0] for r in con.execute("SELECT rowid FROM vec_chunks ORDER BY rowid")]
        assert len(rowids) > 36, f"need >36 chunks for this red, got {len(rowids)}"
        zero = struct.pack(f"{DIM}f", *([0.0] * DIM))
        for rid in rowids[34:]:
            con.execute("DELETE FROM vec_chunks WHERE rowid = ?", (rid,))
            con.execute("INSERT INTO vec_chunks (rowid, embedding) VALUES (?, ?)", (rid, zero))
        con.commit()
    finally:
        con.close()
    failures = epoch_build.validate_candidate(
        db, embedding_dim=DIM, sources=srcs, prior_inv={}, confirmed_removed=set())
    assert any("zero norm" in f for f in failures), (
        f"gate passed {len(rowids)-34} zero vectors sitting beyond the 32-row sample: {failures}")


# R6 — my own v1.4 amendment, verified as implemented: an unmounted /mnt root is
# "mount ABSENT", never a deletion. /mnt/q is not in /proc/mounts on this box.
def test_r6_unmounted_mnt_root_refuses_as_mount_absent(tmp_path):
    home, srcs = _mk_home(tmp_path)
    _seed(home)
    ghost = "/mnt/q/corpus"
    failures = epoch_build.validate_candidate(
        home / "brain.db", embedding_dim=DIM, sources=[*srcs, ghost],
        prior_inv={ghost: {"a.md": "h1"}}, confirmed_removed=set())
    assert any("ABSENT from" in f and "/proc/mounts" in f for f in failures), failures
    # and NOT the vanished-root message — absence of the mount is a different verdict
    assert not any("VANISHED" in f for f in failures), failures
