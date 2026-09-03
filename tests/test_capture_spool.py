"""CSO acceptance fixtures — item-6 increment 1: the capture-staging spool.

Contract: CSO design note 2026-09-02T06:20Z (``_COLLAB``, "item 6: capture
staging spool"), increment 1 only — the staging-dir contract and the gated
hook write. Sweep/receipts are increment 2 and have NO fixtures here.

The one-line design: on an epoch-managed home, ``lbrain capture`` no longer
refuses — it stages the payload into ``$LBRAIN_HOME/capture-staging/`` as an
ADDITIVE, CONTENT-ADDRESSED entry, gated by W1/W2 at spool time. Only the
epoch refusal is exempt; that exemption is the entire feature. The spool path
derives from the NAMED home, never from config ``db_path``, so foreign
materialization is impossible by construction, not by gate.

Matrix (design §"RED lines I'll write before the code", inc-1 rows):
  P1 epoch home + foreign db_path      → rc=2, both paths named, NO spool entry,
                                         nothing materialized at the foreign path
  P2 seat claim vs foreign identity    → rc=2, no spool entry
  P3 LBRAIN_SEAT set-but-EMPTY         → rc=2 (empty ≠ absent — W2's decision)
  P4 same content twice                → one spool entry, both invocations succeed
  P5 interrupt between payload+meta    → entry INCOMPLETE (not counted), retry
                                         completes idempotently; meta renames LAST
  G1 epoch home, honest config         → rc=0, payload+meta staged, epoch db
                                         byte-identical, NO passphrase required
  G2 legacy home                       → db write path unchanged, no spool dir
  R1 --remote on epoch home            → refuses (its index needs a store write)
  R2 shred on epoch home               → still refuses (spool never stages a shred)
  V1 doctor --json                     → reports captures_staged over COMPLETE
                                         entries only

Perms note: the spool is created 0700/0600 best-effort, but this checkout can
sit on DrvFs where chmod does not stick, so no fixture asserts modes — the
assertion would test the mount, not the code.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SEAT_NAME = "metavolvelabs/csuite/cso/touchstone"
EPOCH_ID = "epoch-0001"


# --------------------------------------------------------------------------- #
# plumbing
# --------------------------------------------------------------------------- #

def make_epoch_home(home: Path, *, db_bytes: bytes = b"sentinel-epoch-db") -> Path:
    """A minimal epoch-managed layout: CURRENT + the published db file."""
    root = home / "epochs"
    (root / EPOCH_ID).mkdir(parents=True, exist_ok=True)
    (root / EPOCH_ID / "brain.db").write_bytes(db_bytes)
    (root / "CURRENT").write_text(EPOCH_ID, encoding="utf-8")
    return home


def write_config(home: Path, db_path: Path | None = None) -> None:
    db = db_path if db_path is not None else home / "brain.db"
    # A hosted provider with no key on purpose: capture then skips the embedder
    # entirely, keeping the legacy-path fixture offline and fast.
    (home / "config.toml").write_text(
        f'db_path = "{db.as_posix()}"\nembedding_provider = "gemini"\n',
        encoding="utf-8",
    )


def cli_env(tmp_path: Path, home: Path, **extra: str) -> dict:
    userhome = tmp_path / "userhome"
    userhome.mkdir(exist_ok=True)
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(userhome),
        "LBRAIN_HOME": str(home),
        # The fake HOME hides user-site packages; hand the child the parent's
        # import surface wholesale (same reason as the W1/W2 fixtures).
        "PYTHONPATH": os.pathsep.join([str(REPO_ROOT)] + [p for p in sys.path if p]),
    }
    env.update(extra)
    return env


def run_cli(args: list[str], env: dict, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", "from lbrain.cli import main; main()", *args],
        capture_output=True, text=True, env=env, cwd=str(cwd), timeout=120,
    )


@pytest.fixture()
def transcript(tmp_path) -> Path:
    p = tmp_path / "session.md"
    p.write_text("# session transcript\nprobe content, unique enough\n", encoding="utf-8")
    return p


def spool_entries(home: Path) -> tuple[list[Path], list[Path]]:
    d = home / "capture-staging"
    if not d.is_dir():
        return [], []
    payloads = sorted(p for p in d.iterdir() if p.suffix == ".transcript")
    metas = sorted(p for p in d.iterdir() if p.name.endswith(".meta.json"))
    return payloads, metas


# --------------------------------------------------------------------------- #
# P1–P3: the gates apply to the spool exactly as to the db
# --------------------------------------------------------------------------- #

def test_p1_foreign_dbpath_refused_no_spool_no_materialization(tmp_path, transcript):
    home = make_epoch_home(tmp_path / "lbrain-home")
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    write_config(home, db_path=foreign / "brain.db")

    proc = run_cli(["capture", "--from-file", str(transcript)],
                   cli_env(tmp_path, home), tmp_path)
    out = proc.stdout + proc.stderr
    assert proc.returncode == 2, f"expected rc=2, got {proc.returncode}: {out}"
    assert str((foreign / "brain.db").resolve()) in out, "effective target not named"
    assert str(home.resolve()) in out, "named home not named"
    payloads, metas = spool_entries(home)
    assert not payloads and not metas, "a refused write must stage NOTHING"
    assert not (foreign / "brain.db").exists(), "refusal materialized a foreign db"
    assert not (foreign / "capture-staging").exists(), "spool derived from db_path, not the named home"


def test_p2_seat_mismatch_refused_no_spool(tmp_path, transcript):
    home = make_epoch_home(tmp_path / "lbrain-home")
    write_config(home)
    (home / "identity.json").write_text(
        json.dumps({"name": "metavolvelabs/csuite/cco/artiswa"}), encoding="utf-8")

    proc = run_cli(["capture", "--from-file", str(transcript)],
                   cli_env(tmp_path, home, LBRAIN_SEAT="cso"), tmp_path)
    out = proc.stdout + proc.stderr
    assert proc.returncode == 2, f"expected rc=2, got {proc.returncode}: {out}"
    payloads, metas = spool_entries(home)
    assert not payloads and not metas


def test_p3_seat_set_but_empty_refused(tmp_path, transcript):
    home = make_epoch_home(tmp_path / "lbrain-home")
    write_config(home)

    proc = run_cli(["capture", "--from-file", str(transcript)],
                   cli_env(tmp_path, home, LBRAIN_SEAT=""), tmp_path)
    assert proc.returncode == 2, (proc.stdout + proc.stderr)
    payloads, metas = spool_entries(home)
    assert not payloads and not metas


# --------------------------------------------------------------------------- #
# G1 / P4: the staged write, and content-addressed idempotency
# --------------------------------------------------------------------------- #

def test_g1_epoch_home_stages_no_passphrase_needed_db_untouched(tmp_path, transcript):
    db_bytes = b"sentinel-epoch-db"
    home = make_epoch_home(tmp_path / "lbrain-home", db_bytes=db_bytes)
    write_config(home)

    # Deliberately NO LBRAIN_ARCHIVE_PASSPHRASE: the spool stores the raw
    # payload; encryption happens at sweep time when the archiver runs.
    proc = run_cli(["capture", "--from-file", str(transcript), "--session-id", "sess-1",
                    "--title", "probe session"],
                   cli_env(tmp_path, home), tmp_path)
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out
    assert "staged" in out.lower(), f"output must say the capture was STAGED: {out}"

    payloads, metas = spool_entries(home)
    assert len(payloads) == 1 and len(metas) == 1
    digest = hashlib.sha256(transcript.read_bytes()).hexdigest()
    assert payloads[0].name == f"{digest[:16]}.transcript"
    assert payloads[0].read_bytes() == transcript.read_bytes()
    meta = json.loads(metas[0].read_text(encoding="utf-8"))
    assert meta["sha256"] == digest
    assert meta["session_id"] == "sess-1"
    assert meta["title"] == "probe session"
    assert meta["captured_at"]  # present and non-empty
    # The published epoch is READ-ONLY territory: byte-identical after staging.
    assert (home / "epochs" / EPOCH_ID / "brain.db").read_bytes() == db_bytes
    # No -wal/-shm, no legacy brain.db materialized.
    assert not (home / "brain.db").exists()


def test_p4_same_content_twice_one_entry_both_succeed(tmp_path, transcript):
    home = make_epoch_home(tmp_path / "lbrain-home")
    write_config(home)
    env = cli_env(tmp_path, home)

    p1 = run_cli(["capture", "--from-file", str(transcript)], env, tmp_path)
    p2 = run_cli(["capture", "--from-file", str(transcript)], env, tmp_path)
    assert p1.returncode == 0 and p2.returncode == 0
    assert "already staged" in (p2.stdout + p2.stderr).lower()
    payloads, metas = spool_entries(home)
    assert len(payloads) == 1 and len(metas) == 1


# --------------------------------------------------------------------------- #
# P5: crash ordering — meta renames LAST, incomplete entries don't count
# --------------------------------------------------------------------------- #

def test_p5_interrupt_between_payload_and_meta_then_retry(tmp_path, monkeypatch):
    import lbrain.spool as spool_mod
    from lbrain.spool import spool_capture, staged_count

    home = make_epoch_home(tmp_path / "lbrain-home")
    write_config(home)
    cfg = SimpleNamespace(db_path=home / "brain.db")
    payload = b"unique interrupt probe\n"

    calls = {"n": 0}
    real_replace = spool_mod.os.replace

    def exploding_replace(src, dst):
        calls["n"] += 1
        if calls["n"] == 2:  # payload rename survived; meta rename is the crash
            raise OSError("simulated crash between payload and meta")
        return real_replace(src, dst)

    monkeypatch.setattr(spool_mod.os, "replace", exploding_replace)
    with pytest.raises(OSError):
        spool_capture(cfg, home, payload, session_id="s", title="t", namespace=None)
    monkeypatch.setattr(spool_mod.os, "replace", real_replace)

    d = home / "capture-staging"
    digest = hashlib.sha256(payload).hexdigest()
    assert (d / f"{digest[:16]}.transcript").exists(), "payload rename came first"
    assert not (d / f"{digest[:16]}.meta.json").exists(), "meta must rename LAST"
    assert staged_count(home) == 0, "an entry without meta is INCOMPLETE, never counted"

    res = spool_capture(cfg, home, payload, session_id="s", title="t", namespace=None)
    assert not res.skipped, "retry after a torn spool must complete, not skip"
    assert staged_count(home) == 1
    assert not list(d.glob("*.tmp")), "no tmp debris after a completed retry"


# --------------------------------------------------------------------------- #
# G2: legacy homes keep the old path, byte for byte
# --------------------------------------------------------------------------- #

def test_g2_legacy_home_writes_db_no_spool(tmp_path, transcript):
    home = tmp_path / "lbrain-home"
    home.mkdir(exist_ok=True)  # conftest's autouse isolation may have made it
    write_config(home)

    proc = run_cli(["capture", "--from-file", str(transcript)],
                   cli_env(tmp_path, home, LBRAIN_ARCHIVE_PASSPHRASE="test-pass"),
                   tmp_path)
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out
    assert (home / "brain.db").exists(), "legacy capture must still write the db"
    assert not (home / "capture-staging").exists(), "legacy path must not grow a spool"


# --------------------------------------------------------------------------- #
# R1 / R2: what still refuses
# --------------------------------------------------------------------------- #

def test_r1_remote_capture_on_epoch_home_refuses(tmp_path, transcript):
    home = make_epoch_home(tmp_path / "lbrain-home")
    write_config(home)

    proc = run_cli(["capture", "--from-file", str(transcript), "--remote"],
                   cli_env(tmp_path, home, LBRAIN_ARCHIVE_PASSPHRASE="test-pass"),
                   tmp_path)
    out = proc.stdout + proc.stderr
    assert proc.returncode == 2, f"expected rc=2, got {proc.returncode}: {out}"
    assert "capture-staging" in out or "spool" in out.lower(), \
        "the refusal must name the path forward"
    payloads, metas = spool_entries(home)
    assert not payloads and not metas


def test_r2_shred_on_epoch_home_still_refuses(tmp_path):
    home = make_epoch_home(tmp_path / "lbrain-home")
    write_config(home)

    proc = run_cli(["shred", "--txid", "deadbeef", "--yes"],
                   cli_env(tmp_path, home, LBRAIN_ARCHIVE_PASSPHRASE="test-pass"),
                   tmp_path)
    out = proc.stdout + proc.stderr
    assert proc.returncode != 0, "shred must NOT succeed on an epoch home"
    payloads, metas = spool_entries(home)
    assert not payloads and not metas, "a shred must never stage anything (a deferred shred is a lie)"


# --------------------------------------------------------------------------- #
# V1: staleness must be visible
# --------------------------------------------------------------------------- #

def test_v1_doctor_reports_staged_count_complete_entries_only(tmp_path, transcript):
    home = make_epoch_home(tmp_path / "lbrain-home")
    write_config(home)
    env = cli_env(tmp_path, home)

    run_cli(["capture", "--from-file", str(transcript)], env, tmp_path)
    other = tmp_path / "other.md"
    other.write_text("second transcript\n", encoding="utf-8")
    run_cli(["capture", "--from-file", str(other)], env, tmp_path)
    # An orphaned payload (no meta) must NOT count.
    (home / "capture-staging" / ("0" * 16 + ".transcript")).write_bytes(b"torn")

    proc = run_cli(["doctor", "--json"], env, tmp_path)
    payload = json.loads(proc.stdout)
    assert payload.get("captures_staged") == 2, proc.stdout
