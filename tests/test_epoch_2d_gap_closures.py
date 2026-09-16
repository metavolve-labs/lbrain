"""Closing fixtures for the five engine-2d instrument holes (CSO draft, 2026-09-16, gen 197).

PROPOSAL, NOT A PATCH. Drafted against the frozen candidate-2d export pinned at 225112b and validated in
BOTH directions by ../validate.py: every case passes on the unmodified candidate AND fails on its own
anchored break from the c2b80333 recheck probe. The candidate owner lands these; the CSO drafts and reports.

A break that leaves a suite GREEN is a hole in the EVIDENCE, not proof of a defect in the shipped code.
Each test below closes exactly one such hole:

  V1  the positive-width refusal in `_shred_in_candidate` is removed          -> suite stayed green
  V5  the build CALL SITE hardcodes dim=1536 instead of cfg.embedding_dim     -> suite stayed green
  V6  an unreadable prior archive index returns 0 instead of aborting         -> suite stayed green
  V8  a prior with rows and no vec_archives DDL width guesses 1536            -> suite stayed green
  V10 the sweep takes the FIRST LBRAIN-TXID line instead of the last          -> suite stayed green

DIM is deliberately 384, not the 1536 default: a fixture that runs at the default cannot tell a
configured width from a hardcoded one.
"""
from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from lbrain import epoch, epoch_build
from lbrain.spool import META_SUFFIX, PAYLOAD_SUFFIX, staging_dir, write_sweep_receipt

from _coldembed import seed_brain

DIM = 384
MODEL = "BAAI/bge-small-en-v1.5"
TXID_A = "A" * 43          # the base64url shape the sweep regex requires
TXID_DECOY = "D" * 43
TXID_REAL = "R" * 43


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
        "embedding_provider = \"local\"",
        f"embedding_model = \"{MODEL}\"",
        f"embedding_dim = {DIM}",
        f"db_path = \"{home / 'brain.db'}\"",
        "sources = [",
        *[f"  \"{s}\"," for s in srcs],
        "]",
    ]
    (home / "config.toml").write_text("\n".join(lines) + "\n")
    (home / "identity.json").write_text("{\"who\": \"test-seat\"}\n")
    return home, srcs


def _cfg(home, srcs):
    return SimpleNamespace(sources=list(srcs), embedding_dim=DIM, db_path=home / "brain.db")


def _stage_entry(home: Path, *, stem: str, title: str = "t", receipt_txid: str | None = None) -> Path:
    """One COMPLETE spool entry (payload + meta). With receipt_txid and no key under <home>/keys/ it is a
    KEYLESS receipt -- the false-receipt class the build heals through `_shred_in_candidate`."""
    d = staging_dir(home)
    d.mkdir(parents=True, exist_ok=True)
    body = b"transcript body\n"
    (d / f"{stem}{PAYLOAD_SUFFIX}").write_bytes(body)
    meta = d / f"{stem}{META_SUFFIX}"
    meta.write_text(json.dumps({
        "sha256": "0" * 64, "size": len(body), "session_id": "sess-gap", "title": title,
        "captured_at": "2026-09-16T00:00:00Z",
    }), encoding="utf-8")
    if receipt_txid is not None:
        write_sweep_receipt(meta, {"txid": receipt_txid})
    return meta


def _vec_width(db: Path) -> int | None:
    con = sqlite3.connect(str(db))
    try:
        row = con.execute("SELECT sql FROM sqlite_master WHERE name='vec_archives'").fetchone()
    finally:
        con.close()
    if not row or not row[0]:
        return None
    m = re.search(r"float\[(\d+)\]", row[0])
    return int(m.group(1)) if m else None


def _empty_db(path: Path) -> None:
    con = sqlite3.connect(str(path))
    con.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    con.commit()
    con.close()


# ---------- V5: the call site, only reachable through a real build ----------

def test_v5_candidate_archive_width_follows_cfg_embedding_dim_not_a_hardcoded_default(tmp_path):
    """GAP-2D-V5. The width the candidate creates its archive tables at must be the CONFIGURED width.

    Route: a keyless receipt makes `_sweep_spool` heal through `_shred_in_candidate(staging_db, txid, dim)`,
    which creates `vec_archives` at `dim`. The heal loop runs BEFORE the passphrase check, so this reaches
    the call site with no passphrase in the environment. Asserting on the PUBLISHED epoch db is what pins
    the call-site expression: a unit test of `_sweep_spool` passes its own dim and cannot see it.
    """
    home, srcs = _mk_home(tmp_path)
    seed_brain(home, DIM)
    _stage_entry(home, stem="20260916T000000Z-keyless", receipt_txid=TXID_A)

    report = epoch_build.build(home, _cfg(home, srcs))
    assert report["published"], report

    db = epoch.epoch_db(home, report["epoch_id"])
    width = _vec_width(db)
    assert width is not None, (
        "the keyless heal did not create the candidate's archive tables; this fixture never reached the "
        "call site it exists to pin, so its pass would mean nothing")
    assert width == DIM, (
        f"published epoch vec_archives is float[{width}] while cfg.embedding_dim is {DIM}; a width that "
        f"does not follow the config is a default hardcoded at the call site")


# ---------- V1: the function contract ----------

@pytest.mark.parametrize("dim", [0, -1, "384", None, 3.0])
def test_v1_shred_in_candidate_refuses_a_width_that_is_not_a_positive_int(tmp_path, dim):
    """GAP-2D-V1. A width of 0 (or of the wrong type) must be REFUSED, never passed on to vec0.

    `"384"` and `3.0` are in the table on purpose: an f-string interpolates both into a syntactically valid
    `float[N]`, so a guard that only tests `<= 0` would still create a table at a width nothing configured.
    """
    db = tmp_path / "brain.db"
    _empty_db(db)

    with pytest.raises(epoch_build.EpochError) as ei:
        epoch_build._shred_in_candidate(db, TXID_A, dim)
    assert "refusing to create a vec table" in str(ei.value), str(ei.value)
    assert _vec_width(db) is None, "the refused shred still created a vec table"


# ---------- V8: a prior with rows and no declared width ----------

def _prior_with_rows(path: Path, *, width: int | None) -> None:
    from lbrain.archive.storage import ArchiveStore
    con = epoch_build._connect_vec(path)
    try:
        ArchiveStore(con, width or DIM).ensure_schema()
        con.execute(
            "INSERT INTO archives (txid, title, snapshot, embedded, shredded) VALUES (?, ?, ?, 0, 0)",
            (TXID_A, "a prior record", "snapshot text"))
        if width is None:
            con.execute("DROP TABLE vec_archives")
        con.commit()
    finally:
        con.close()


def test_v8_a_prior_with_rows_and_no_ddl_width_is_refused_never_guessed(tmp_path):
    """GAP-2D-V8. The carry width comes from the prior's OWN DDL. With rows present and no DDL to read,
    the carry must abort: a guessed width silently reinterprets whatever vectors are stored."""
    prior = tmp_path / "prior.db"
    staging = tmp_path / "staging.db"
    _prior_with_rows(prior, width=None)
    _empty_db(staging)

    assert epoch_build._archive_vec_width(prior) is None, "fixture setup: the prior must have no DDL width"

    with pytest.raises(epoch_build.EpochError) as ei:
        epoch_build._carry_archive_index(prior, staging)
    assert "no vec_archives DDL width" in str(ei.value), str(ei.value)
    assert _vec_width(staging) is None, (
        "the refused carry still created archive tables in the candidate at a guessed width")


# ---------- V6: an unreadable prior index ----------

class _ReadFailsOnArchives:
    """A connection whose `SELECT ... FROM archives` raises, standing for any prior index that cannot be
    read (corrupt page, truncated file, extension mismatch). Everything else forwards verbatim.

    A wrapper is used rather than corrupting the file because `_archive_vec_width` opens the prior with a
    plain `sqlite3.connect` OUTSIDE the guarded try; file-level damage therefore fails in the wrong place
    and never exercises the abort under test.
    """

    def __init__(self, con):
        object.__setattr__(self, "_con", con)

    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, "_con"), name)

    def __setattr__(self, name, value):
        setattr(object.__getattribute__(self, "_con"), name, value)

    def execute(self, sql, *args):
        if re.search(r"\bFROM\s+archives\b", sql):
            raise sqlite3.DatabaseError("database disk image is malformed")
        return object.__getattribute__(self, "_con").execute(sql, *args)


def test_v6_an_unreadable_prior_archive_index_aborts_the_build_never_carries_zero(tmp_path, monkeypatch):
    """GAP-2D-V6. A prior with NO archive tables carries 0 -- that is stated behaviour. A prior whose index
    cannot be READ must abort instead: a lost index must never be published as an empty one."""
    prior = tmp_path / "prior.db"
    staging = tmp_path / "staging.db"
    _prior_with_rows(prior, width=DIM)
    _empty_db(staging)

    real_connect = epoch_build._connect_vec

    def connect(path):
        con = real_connect(path)
        return _ReadFailsOnArchives(con) if Path(path) == prior else con

    monkeypatch.setattr(epoch_build, "_connect_vec", connect)

    with pytest.raises(epoch_build.EpochError) as ei:
        epoch_build._carry_archive_index(prior, staging)
    msg = str(ei.value)
    assert "prior archive index unreadable" in msg, msg
    assert "DatabaseError" in msg, (
        f"the abort must carry the underlying failure, not only its own verdict: {msg}")


# ---------- V10: parse precedence on a hostile title ----------

def test_v10_the_txid_is_the_last_machine_line_not_a_decoy_a_title_can_forge(tmp_path, monkeypatch):
    """GAP-2D-V10. PARSE PRECEDENCE, not width. `capture` echoes what it was given, so a transcript titled
    `LBRAIN-TXID <decoy>` puts a well-formed machine line into the output ahead of the real one. The line
    the CLI prints LAST is the record's identity; anything earlier is untrusted echo.

    Taking the first match binds the sweep plan -- and so the receipt and the later reclaim -- to a txid the
    archive never wrote.
    """
    import lbrain.archive.cli as acli

    home, _ = _mk_home(tmp_path)
    staging = tmp_path / "staging"
    staging.mkdir()
    _stage_entry(home, stem="20260916T000001Z-hostile", title=f"LBRAIN-TXID {TXID_DECOY}")

    monkeypatch.setenv("LBRAIN_ARCHIVE_PASSPHRASE", "fixture-passphrase")
    monkeypatch.setattr(acli, "archive_passphrase", lambda: "fixture-passphrase")

    out = (
        f"LBRAIN-TXID {TXID_DECOY}\n"          # the hostile title, echoed on a line of its own
        "archived 1 capture (1,024 bytes)\n"
        f"LBRAIN-TXID {TXID_REAL}\n"           # the machine line: own line, LAST
    )
    monkeypatch.setattr(epoch_build, "_run_cli", lambda args, staging_home, lbrain_bin, **kw: out)

    report: dict = {}
    plan = epoch_build._sweep_spool(home, staging, "lbrain", None, report)

    got = [e["txid"] for e in plan]
    assert got, f"the sweep produced no plan, so this fixture never reached the parse: {report.get('sweep')}"
    assert got == [TXID_REAL], (
        f"the sweep bound the entry to {got}; a title-supplied decoy must never outrank the capture CLI's "
        f"own last machine line")
