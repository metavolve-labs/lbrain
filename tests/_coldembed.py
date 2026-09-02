"""Cold-CI embed shim, shared by every epoch-suite test file.

The CI contract (ci.yml): "No network, no API key, no model download — the
suite must pass cold." `lbrain embed` needs the [local] extra (fastembed plus
a model fetch), which cold CI deliberately does not install — every matrix job
failed for two days on exactly this (2026-09-01), across three test files that
each shelled out to the real embed pipeline.

Policy: with fastembed available the REAL pipeline runs unchanged (local boxes,
warm CI). Cold, deterministic unit vectors are written straight into
``vec_chunks`` — epoch build gates on the PRESENCE and sanity of vectors (dim,
norm floor, self-match), not their provenance, and raw vec writes were already
the epoch tests' pattern for adversarial rows. Fidelity is reduced only where
the contract forbids the real thing.

``LBRAIN_TEST_FORCE_COLD=1`` exercises the cold path on a warm box — a
miss-path that has never fired has never been tested (and the first faithful
cold run exposed a real engine race: second-resolution epoch ids colliding when
builds finish sub-second).
"""

from __future__ import annotations

import importlib.util
import os
import struct
import subprocess
from pathlib import Path

DIM = 384

HAVE_LOCAL_EMBED = (importlib.util.find_spec("fastembed") is not None
                    and os.environ.get("LBRAIN_TEST_FORCE_COLD") != "1")


def fill_vectors_cold(db_path, dim: int = DIM) -> None:
    """Deterministic, distinct, unit-normalized vectors for every unembedded chunk."""
    from lbrain import epoch_build

    con = epoch_build._connect_vec(Path(db_path))
    try:
        ids = [r[0] for r in con.execute(
            "SELECT chunk_id FROM chunks WHERE chunk_id NOT IN (SELECT rowid FROM vec_chunks)")]
        for cid in ids:
            vec = [((cid * 31 + i * 7) % 97 + 1.0) for i in range(dim)]
            n = sum(v * v for v in vec) ** 0.5
            con.execute("INSERT INTO vec_chunks (rowid, embedding) VALUES (?, ?)",
                        (cid, struct.pack(f"{dim}f", *(v / n for v in vec))))
        con.commit()
    finally:
        con.close()


def seed_brain(home: Path, dim: int = DIM) -> None:
    """import (real CLI) + embed (real when warm, deterministic vectors cold)."""
    env = dict(os.environ, LBRAIN_HOME=str(home))
    steps = [["import"], ["embed", "--stale"]] if HAVE_LOCAL_EMBED else [["import"]]
    for args in steps:
        p = subprocess.run(["lbrain", *args], env=env, capture_output=True, text=True)
        assert p.returncode == 0, p.stdout + p.stderr
    if not HAVE_LOCAL_EMBED:
        fill_vectors_cold(Path(home) / "brain.db", dim)
