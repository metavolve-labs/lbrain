"""Unit tests for W1/W2 write gates (CSO spec 2026-09-01, R1–R7/G1–G3 shapes).

These are the builder's own tests. The CSO's blind fixtures land separately in
test_write_path_gates.py; overlap is intended — an oracle that shares its
implementation with the thing it tests is not an oracle, and the same goes for
authorship.
"""

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from lbrain.write_gates import WriteGateError, check_write_target


@pytest.fixture()
def fake_home(tmp_path, monkeypatch):
    """An isolated named-home whose refusal log lands under a fake $HOME."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "userhome"))
    (tmp_path / "userhome").mkdir()
    home = tmp_path / "namedhome"
    home.mkdir()
    monkeypatch.delenv("LBRAIN_SEAT", raising=False)
    return home


def _cfg(db: Path):
    return SimpleNamespace(db_path=db)


def _refusals(tmp_path):
    log = tmp_path / "userhome" / ".lbrain-refusals.log"
    return log.read_text() if log.exists() else ""


# --- W1: home coherence ------------------------------------------------------

def test_r1_copied_config_absolute_db_path_refused(fake_home, tmp_path):
    """The CSO §7 replay: named home is a copy, db_path points at the original."""
    original = tmp_path / "original"
    original.mkdir()
    db = original / "brain.db"
    with pytest.raises(WriteGateError) as exc:
        check_write_target(_cfg(db), fake_home)
    msg = str(exc.value)
    assert str(fake_home.resolve()) in msg and str(db.resolve()) in msg
    assert "REFUSED write" in _refusals(tmp_path)


def test_r2_symlink_escaping_home_refused(fake_home, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "brain.db").touch()
    link = fake_home / "brain.db"
    link.symlink_to(outside / "brain.db")
    with pytest.raises(WriteGateError):
        check_write_target(_cfg(link), fake_home)


def test_g1_coherent_home_no_claim_proceeds(fake_home):
    check_write_target(_cfg(fake_home / "brain.db"), fake_home)  # no raise


# --- W2: seat identity -------------------------------------------------------

def _write_identity(home: Path, name="metavolvelabs/csuite/cso/touchstone"):
    (home / "identity.json").write_text(json.dumps({"name": name}))


def test_r4_wrong_seat_refused_both_named(fake_home, monkeypatch):
    _write_identity(fake_home, "metavolvelabs/csuite/cco/artiswa")
    monkeypatch.setenv("LBRAIN_SEAT", "cso")
    with pytest.raises(WriteGateError) as exc:
        check_write_target(_cfg(fake_home / "brain.db"), fake_home)
    assert "cso" in str(exc.value) and "artiswa" in str(exc.value)


def test_r5_claim_with_no_identity_refused(fake_home, monkeypatch):
    monkeypatch.setenv("LBRAIN_SEAT", "cso")
    with pytest.raises(WriteGateError) as exc:
        check_write_target(_cfg(fake_home / "brain.db"), fake_home)
    assert "no identity" in str(exc.value)


def test_r6_malformed_identity_names_parse_failure(fake_home, monkeypatch):
    (fake_home / "identity.json").write_text("{not json")
    monkeypatch.setenv("LBRAIN_SEAT", "cso")
    with pytest.raises(WriteGateError) as exc:
        check_write_target(_cfg(fake_home / "brain.db"), fake_home)
    msg = str(exc.value).lower()
    assert "unparseable" in msg or "unreadable" in msg
    assert "another seat" not in msg  # a parse failure is NOT a foreign-seat diagnosis


def test_r7_set_but_empty_claim_refused(fake_home, monkeypatch):
    _write_identity(fake_home)
    monkeypatch.setenv("LBRAIN_SEAT", "")
    with pytest.raises(WriteGateError) as exc:
        check_write_target(_cfg(fake_home / "brain.db"), fake_home)
    assert "EMPTY" in str(exc.value)


def test_g2_matching_claims_proceed(fake_home, monkeypatch):
    _write_identity(fake_home)
    for claim in ("cso", "touchstone", "cso/touchstone", "metavolvelabs/csuite/cso/touchstone"):
        monkeypatch.setenv("LBRAIN_SEAT", claim)
        check_write_target(_cfg(fake_home / "brain.db"), fake_home)  # no raise


def test_w2_substring_of_component_refused(fake_home, monkeypatch):
    """Anchored matching: 'so' appears inside 'cso' but is not a component."""
    _write_identity(fake_home)
    monkeypatch.setenv("LBRAIN_SEAT", "so")
    with pytest.raises(WriteGateError):
        check_write_target(_cfg(fake_home / "brain.db"), fake_home)


def test_w2_noncontiguous_multiseg_refused(fake_home, monkeypatch):
    _write_identity(fake_home)
    monkeypatch.setenv("LBRAIN_SEAT", "csuite/touchstone")  # segments exist, not contiguous
    with pytest.raises(WriteGateError):
        check_write_target(_cfg(fake_home / "brain.db"), fake_home)


# --- rc=2 through the real CLI (G3's engine half) ---------------------------

def test_cli_import_refuses_rc2_on_incoherent_home(tmp_path):
    home = tmp_path / "copyhome"
    home.mkdir()
    (tmp_path / "elsewhere").mkdir()
    (home / "config.toml").write_text(
        f'db_path = "{tmp_path / "elsewhere" / "brain.db"}"\n'
    )
    import os
    import site
    env = dict(os.environ, LBRAIN_HOME=str(home), HOME=str(tmp_path))
    env.pop("LBRAIN_SEAT", None)
    # HOME is overridden so the refusal log stays in the sandbox — but that also
    # hides the real user-site (httpx et al). Re-expose it explicitly.
    env["PYTHONPATH"] = os.pathsep.join(
        p for p in [site.getusersitepackages(), env.get("PYTHONPATH", "")] if p
    )
    proc = subprocess.run(
        [sys.executable, "-m", "lbrain.cli", "import", str(tmp_path)],
        env=env, capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "the home you name" in (proc.stdout + proc.stderr)


# --- RED-W-BYPASS-1 regression: the archive sub-CLI funnels through the gates --

def _cli_env(tmp_path, home):
    import os
    import site
    env = dict(os.environ, LBRAIN_HOME=str(home), HOME=str(tmp_path))
    env.pop("LBRAIN_SEAT", None)
    env["PYTHONPATH"] = os.pathsep.join(
        p for p in [site.getusersitepackages(), env.get("PYTHONPATH", "")] if p
    )
    return env


def test_archive_shred_refuses_foreign_db_and_materializes_nothing(tmp_path):
    """CSO's behavioral proof, replayed: shred against a foreign db_path was
    rc=0 and MATERIALIZED a brain.db at the foreign path. Now: rc=2, refusal
    names both paths, and nothing is created anywhere."""
    pytest.importorskip("cryptography")
    home = tmp_path / "copyhome"
    home.mkdir()
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    (home / "config.toml").write_text(f'db_path = "{foreign / "brain.db"}"\n')
    proc = subprocess.run(
        [sys.executable, "-m", "lbrain.cli", "shred", "--txid", "deadbeef", "--yes", "--soft"],
        env=_cli_env(tmp_path, home), capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "the home you name" in (proc.stdout + proc.stderr)
    assert not (foreign / "brain.db").exists()
    assert not (home / "brain.db").exists()


def test_archive_read_refuses_to_create(tmp_path):
    """A read (archives list) on a legacy home with no db must refuse, not
    materialize — non-immutable Store() creates on open."""
    pytest.importorskip("cryptography")
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.toml").write_text(f'db_path = "{home / "brain.db"}"\n')
    proc = subprocess.run(
        [sys.executable, "-m", "lbrain.cli", "archives"],
        env=_cli_env(tmp_path, home), capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "refusing to create" in (proc.stdout + proc.stderr)
    assert not (home / "brain.db").exists()
