"""CSO acceptance fixtures for the W1/W2 write-path gates — the R/G matrix of
spec 2026-09-01T20:00Z ("done-means-dereferenced"), staged as the contract the
implementation is verified against.

Authorship note (two-gate discipline): these fixtures were written by the CSO
from the spec, without reading tests/test_write_gates_unit.py, so the two files
are independent readings of the same contract. Where both exist, keep both — a
divergence between them is signal, not duplication.

Matrix rows (spec §"RED/GREEN acceptance matrix"):
  R1 copied home, db_path → ORIGINAL     → W1 refuse, both paths named, logged
  R2 db_path relative / symlink escape   → W1 refuse (realpath containment)
  R3 LBRAIN_HOME unset, config escapes   → W1 refuse (default home guarded)
  R4 seat claim vs foreign identity      → W2 refuse, both seats named
  R5 claim present, identity.json absent → W2 refuse "no identity to check"
  R6 identity.json malformed             → W2 refuse naming the PARSE failure
  R7 LBRAIN_SEAT set-but-EMPTY           → refuse (decision: empty ≠ absent)
  G1 honest home, no claim               → write proceeds
  G2 honest home, matching claim         → write proceeds
  G3 refusal line is on disk BEFORE the exception escapes (what makes a
     fail-safe rc=0 hook wrapper honest)

The refusal log lands at ``Path.home()/.lbrain-refusals.log`` — every test here
repoints HOME into the sandbox first, so no fixture can touch a real home.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from lbrain.epoch import open_store
from lbrain.write_gates import WriteGateError

REPO_ROOT = Path(__file__).resolve().parent.parent
SEAT_NAME = "metavolvelabs/csuite/cso/touchstone"


# --------------------------------------------------------------------------- #
# plumbing
# --------------------------------------------------------------------------- #

@pytest.fixture()
def sandbox(tmp_path, monkeypatch):
    """HOME → sandbox (refusal log containment) + no ambient seat claim.

    The named home itself comes from conftest's autouse isolation: it is
    whatever lbrain.config.CONFIG_DIR points at during the test.
    """
    userhome = tmp_path / "userhome"
    userhome.mkdir()
    monkeypatch.setenv("HOME", str(userhome))
    monkeypatch.delenv("LBRAIN_SEAT", raising=False)

    import lbrain.config as config

    named_home = Path(config.CONFIG_DIR)
    named_home.mkdir(parents=True, exist_ok=True)
    return SimpleNamespace(
        home=named_home,
        log=userhome / ".lbrain-refusals.log",
        tmp=tmp_path,
    )


def cfg_for(db_path: Path) -> SimpleNamespace:
    return SimpleNamespace(db_path=Path(db_path), embedding_dim=64)


def refuse(cfg) -> WriteGateError:
    with pytest.raises(WriteGateError) as ei:
        open_store(cfg, for_write=True)
    return ei.value


def write_identity(home: Path, name: str = SEAT_NAME) -> None:
    (home / "identity.json").write_text(
        json.dumps({"name": name, "address": "test-only"}), encoding="utf-8"
    )


# --------------------------------------------------------------------------- #
# W1 — home coherence
# --------------------------------------------------------------------------- #

def test_r1_copied_home_writes_refused_with_both_paths_named(sandbox):
    """The §7 incident replay: a copied home whose config still carries the
    ORIGINAL's absolute db_path must refuse, naming what the caller believed
    AND what would actually have been written."""
    original = sandbox.tmp / "original-home"
    original.mkdir()
    err = refuse(cfg_for(original / "brain.db"))

    msg = str(err)
    assert str(original / "brain.db") in msg, "effective (real) target not named"
    assert str(sandbox.home.resolve()) in msg, "named home not named"
    # Refusal logged unconditionally, outside any brain home.
    assert sandbox.log.is_file()
    assert "REFUSED" in sandbox.log.read_text(encoding="utf-8")


def test_r2_symlink_inside_home_escaping_it_refused(sandbox):
    """A db_path that LOOKS inside the home but dereferences outside must
    refuse — containment is decided on realpath, not on spelling."""
    outside = sandbox.tmp / "outside"
    outside.mkdir()
    (sandbox.home / "data").symlink_to(outside, target_is_directory=True)
    refuse(cfg_for(sandbox.home / "data" / "brain.db"))


def test_r2_relative_dbpath_resolving_outside_refused(sandbox, monkeypatch):
    """A relative db_path resolves against CWD, not the home — if that lands
    outside the named home it must refuse."""
    monkeypatch.chdir(sandbox.tmp)
    refuse(cfg_for(Path("elsewhere") / "brain.db"))


def test_r3_default_home_guarded_when_lbrain_home_unset(tmp_path):
    """LBRAIN_HOME unset → named home is ~/.lbrain, and W1 still runs: a config
    pointing OUT of the default home refuses at the CLI with rc=2.

    Subprocess on purpose: this is the one branch in-process isolation cannot
    reach (conftest always sets LBRAIN_HOME), and it doubles as the CLI
    contract check — refusal is exit code 2, not 1, not a traceback.
    """
    userhome = tmp_path / "userhome"
    default_home = userhome / ".lbrain"
    default_home.mkdir(parents=True)
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    (default_home / "config.toml").write_text(
        f'db_path = "{foreign / "brain.db"}"\n', encoding="utf-8"
    )
    doc = tmp_path / "note.md"
    doc.write_text("# probe\n", encoding="utf-8")

    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(userhome),
        # The fake HOME hides user-site packages (where deps may live), so hand
        # the child the parent's import surface wholesale.
        "PYTHONPATH": os.pathsep.join([str(REPO_ROOT)] + [p for p in sys.path if p]),
    }
    proc = subprocess.run(
        [sys.executable, "-c", "from lbrain.cli import main; main()", "import", str(doc)],
        capture_output=True, text=True, env=env, cwd=str(tmp_path), timeout=120,
    )
    out = proc.stdout + proc.stderr
    assert proc.returncode == 2, f"expected rc=2, got {proc.returncode}: {out}"
    assert str(foreign / "brain.db") in out
    log = userhome / ".lbrain-refusals.log"
    assert log.is_file(), "refusal not logged from the CLI path"
    assert (foreign / "brain.db").name in log.read_text(encoding="utf-8")


def test_w1_outranks_epoch_refusal_for_diagnosis(sandbox):
    """On an epoch-managed home a misdirected config must get the W1 refusal
    (which names the actual defect), not the generic epoch write refusal."""
    epochs = sandbox.home / "epochs"
    epochs.mkdir()
    (epochs / "CURRENT").write_text("19700101T000000Z\n", encoding="utf-8")
    foreign = sandbox.tmp / "foreign"
    foreign.mkdir()
    err = refuse(cfg_for(foreign / "brain.db"))
    assert "named home" in str(err)


# --------------------------------------------------------------------------- #
# W2 — seat identity
# --------------------------------------------------------------------------- #

def test_r4_foreign_seat_refused_both_seats_named(sandbox, monkeypatch):
    write_identity(sandbox.home, "metavolvelabs/csuite/cco/axiom")
    monkeypatch.setenv("LBRAIN_SEAT", "cso")
    err = refuse(cfg_for(sandbox.home / "brain.db"))
    msg = str(err)
    assert "cso" in msg and "cco/axiom" in msg, "refusal must name both seats"


def test_r4_substring_within_segment_refused(sandbox, monkeypatch):
    """The A-546 lesson at brain grain: 'cso' appearing INSIDE a segment is not
    a match — component-wise means segment equality, never substring."""
    write_identity(sandbox.home, "metavolvelabs/csuite/csotouchstone")
    monkeypatch.setenv("LBRAIN_SEAT", "cso")
    refuse(cfg_for(sandbox.home / "brain.db"))


def test_r4_noncontiguous_multisegment_claim_refused(sandbox, monkeypatch):
    """'csuite/touchstone' has both segments present in the identity but not
    adjacent — a multi-segment claim must match as a contiguous run."""
    write_identity(sandbox.home)  # metavolvelabs/csuite/cso/touchstone
    monkeypatch.setenv("LBRAIN_SEAT", "csuite/touchstone")
    refuse(cfg_for(sandbox.home / "brain.db"))


def test_r5_claim_without_identity_refused(sandbox, monkeypatch):
    monkeypatch.setenv("LBRAIN_SEAT", "cso")
    err = refuse(cfg_for(sandbox.home / "brain.db"))
    msg = str(err)
    assert "identity" in msg and "cso" in msg
    # and it is the ABSENCE diagnosis, not the foreign-seat one:
    assert "another seat" not in msg


def test_r6_malformed_identity_diagnosed_as_parse_failure(sandbox, monkeypatch):
    """A parse failure and a foreign seat are different diagnoses (V4 note):
    the message must name the parse failure and must NOT claim the home
    belongs to another seat."""
    (sandbox.home / "identity.json").write_text("{not json", encoding="utf-8")
    monkeypatch.setenv("LBRAIN_SEAT", "cso")
    err = refuse(cfg_for(sandbox.home / "brain.db"))
    msg = str(err).lower()
    assert "unparseable" in msg or "parse" in msg or "unreadable" in msg
    assert "another seat" not in msg


def test_r7_seat_set_but_empty_refuses_and_says_why(sandbox, monkeypatch):
    """RED-V2-1's shape: set-but-empty is what a broken launcher expansion
    produces. Decision (recorded in write_gates.py at the test site): it
    REFUSES — it never silently selects the no-claim branch."""
    monkeypatch.setenv("LBRAIN_SEAT", "")
    err = refuse(cfg_for(sandbox.home / "brain.db"))
    msg = str(err).lower()
    assert "empty" in msg, "must distinguish empty from absent, in words"


# --------------------------------------------------------------------------- #
# GREEN rows — the gate must not tax honest writes
# --------------------------------------------------------------------------- #

def test_g1_honest_home_no_claim_proceeds(sandbox):
    store = open_store(cfg_for(sandbox.home / "brain.db"), for_write=True)
    assert store is not None
    assert not sandbox.log.exists(), "no refusal may be logged on a green path"


def test_g2_matching_single_segment_claim_proceeds(sandbox, monkeypatch):
    write_identity(sandbox.home)
    monkeypatch.setenv("LBRAIN_SEAT", "cso")
    assert open_store(cfg_for(sandbox.home / "brain.db"), for_write=True) is not None
    assert not sandbox.log.exists()


def test_g2_matching_contiguous_multisegment_claim_proceeds(sandbox, monkeypatch):
    write_identity(sandbox.home)
    monkeypatch.setenv("LBRAIN_SEAT", "cso/touchstone")
    assert open_store(cfg_for(sandbox.home / "brain.db"), for_write=True) is not None
    assert not sandbox.log.exists()


def test_g3_refusal_is_on_disk_before_the_exception_escapes(sandbox):
    """The property a fail-safe hook wrapper (outward rc=0) depends on: by the
    time ANY catcher sees WriteGateError, the refusal line already exists.
    Asserted inside the except block — not after the fact."""
    foreign = sandbox.tmp / "foreign"
    foreign.mkdir()
    try:
        open_store(cfg_for(foreign / "brain.db"), for_write=True)
    except WriteGateError:
        assert sandbox.log.is_file(), "refusal must be logged BEFORE raising"
        assert "REFUSED" in sandbox.log.read_text(encoding="utf-8")
    else:
        pytest.fail("R-case did not refuse")
